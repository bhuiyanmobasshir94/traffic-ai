"""Composition root: wires detection, tracking, counting, and annotation around
one camera's video loop, and publishes results to the Redis-backed `StateStore`.

Video decode is inherently blocking (`cv2.VideoCapture.read()`), so every decode
call runs via `asyncio.to_thread` — this is the fix for the 25-minute frozen UI the
previous `time.sleep()`-driven generators produced (see `CLAUDE.md`). `run()` is a
long-running coroutine that loops the demo video forever and never raises out: a
missing file, a corrupt frame, or a detector failure is caught, published as
`PipelineStatus.ERROR`, and retried with backoff, so one camera's fault cannot take
the whole API process down with it.

Two side channels leave the frame loop and both are fail-open: crossings are
handed to the `CrossingWriter` (Postgres history) and tick state is mirrored into
Prometheus. A database that is down or a metric that raises costs history or a
graph, never a frame — `submit()` is a non-blocking in-memory append, and every
metric update is guarded.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable

import cv2
import numpy as np
import supervision as sv

from traffic_ai import metrics
from traffic_ai.cameras import CAMERAS, CameraConfig
from traffic_ai.config import Settings
from traffic_ai.db.writer import CrossingWriter
from traffic_ai.domain import VEHICLE_CLASSES, CameraState, CrossingEvent, PipelineStatus
from traffic_ai.logging import get_logger
from traffic_ai.store import StateStore, utcnow
from traffic_ai.worker.annotate import FrameAnnotator
from traffic_ai.worker.anpr import build_plate_reader
from traffic_ai.worker.counting import Crossing, LineCounter, ThroughputWindow, derive_congestion
from traffic_ai.worker.detection import Detector, build_detector
from traffic_ai.worker.tracking import ByteTrackTracker, VehicleTracker

log = get_logger(__name__)

_INITIAL_RETRY_BACKOFF_SECONDS = 5.0
_MAX_RETRY_BACKOFF_SECONDS = 30.0


# Ticks averaged by `CameraPipeline._measure_fps`; spans several detection ticks.
_FPS_WINDOW_TICKS = 20


class CameraPipeline:
    """Owns one camera's full inference loop, from video decode to published state.

    `detector`/`tracker` are injectable so tests can substitute `StubDetector` (or
    a hand-built `VehicleTracker`) without loading real weights; production code
    leaves both `None` and gets `build_detector(settings)` / a fresh
    `ByteTrackTracker`.

    `writer` is the durable-history sink. `None` (the default) means live-only:
    persistence disabled, or the database could not be set up at startup.
    """

    def __init__(
        self,
        camera: CameraConfig,
        store: StateStore,
        settings: Settings,
        *,
        detector: Detector | None = None,
        tracker: VehicleTracker | None = None,
        writer: CrossingWriter | None = None,
    ) -> None:
        self._camera = camera
        self._store = store
        self._settings = settings
        self._writer = writer
        self._detector = detector or build_detector(settings)
        self._tracker = tracker or ByteTrackTracker(frame_rate=settings.target_fps)
        self._plate_reader = build_plate_reader(settings)

        # Sized lazily, on the first processed frame, once the real frame
        # dimensions (post-resize) are known.
        self._line_counter: LineCounter | None = None
        self._annotator: FrameAnnotator | None = None
        self._throughput = ThroughputWindow()

        self._frame_index = 0
        self._last_detections: sv.Detections = sv.Detections.empty()
        # Recent tick times for `_measure_fps`. A single-tick reading swings
        # between fast tracker-only ticks and slow detection ticks, reporting
        # e.g. 35 fps while the camera actually advances ~0.5 frames a second.
        self._tick_times: deque[float] = deque(maxlen=_FPS_WINDOW_TICKS)
        self._stop_requested = False
        # A metric that raises would do so on every tick (~12/s); warn once, not per frame.
        self._metrics_failure_logged = False

        self._state = CameraState(
            camera_id=camera.camera_id,
            name=camera.name,
            status=PipelineStatus.STARTING,
            updated_at=utcnow(),
            anpr_enabled=settings.anpr_enabled,
        )
        # Registers the series at startup, so a scrape between process start and the
        # first decoded frame reports `starting` instead of omitting the camera.
        self._publish_status_metric(PipelineStatus.STARTING)

    @property
    def camera_id(self) -> str:
        return self._camera.camera_id

    @property
    def state(self) -> CameraState:
        """Always readable, even before the first tick — `STARTING`, set in `__init__`."""
        return self._state

    def request_stop(self) -> None:
        self._stop_requested = True

    async def run(self) -> None:
        """Loops the demo video forever, publishing state every tick. Never raises
        out: any failure is caught, published as an error state, and retried with
        backoff so one camera's fault cannot take the process down."""
        backoff = _INITIAL_RETRY_BACKOFF_SECONDS
        while not self._stop_requested:
            try:
                await self._run_loop()
            except Exception as exc:
                # A pipeline fault (bad video, corrupt frame, detector error) must
                # not take down the process — publish it and retry with backoff.
                log.error("pipeline_error", camera_id=self.camera_id, error=str(exc))
                await self._publish_error(str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_RETRY_BACKOFF_SECONDS)
                continue
            backoff = _INITIAL_RETRY_BACKOFF_SECONDS
        await self._publish_stopped()

    async def _run_loop(self) -> None:
        video_path = self._camera.video_path(self._settings.video_dir)
        cap = await asyncio.to_thread(cv2.VideoCapture, str(video_path))
        try:
            if not cap.isOpened():
                raise RuntimeError(f"could not open video: {video_path}")

            frame_budget = 1.0 / self._settings.target_fps
            while not self._stop_requested:
                tick_started = time.monotonic()
                ok, frame = await asyncio.to_thread(cap.read)
                if not ok or frame is None:
                    # EOF (or a transient decode hiccup) — the demo video loops
                    # forever. Counts keep accumulating across the loop, but the
                    # tracker is reset: track identity does not survive the jump
                    # back to frame 0.
                    log.info("video_loop_restart", camera_id=self.camera_id)
                    await asyncio.to_thread(cap.set, cv2.CAP_PROP_POS_FRAMES, 0)
                    self._tracker.reset()
                    self._last_detections = sv.Detections.empty()
                    continue

                await self._process_frame(self._resize(frame))

                elapsed = time.monotonic() - tick_started
                await asyncio.sleep(max(0.0, frame_budget - elapsed))
        finally:
            await asyncio.to_thread(cap.release)

    def _resize(self, frame: np.ndarray) -> np.ndarray:
        target_width = self._settings.frame_width
        height, width = frame.shape[:2]
        if width <= 0 or width == target_width:
            return frame
        scale = target_width / width
        target_height = max(1, round(height * scale))
        return cv2.resize(frame, (target_width, target_height), interpolation=cv2.INTER_AREA)

    async def _process_frame(self, frame: np.ndarray) -> None:
        """Runs one tick of the pipeline against an already-decoded frame: detect
        (every `detect_every_n_frames`), track (every frame), count crossings,
        derive congestion, annotate, and publish. Split out from `_run_loop` so it
        is directly testable against a synthetic frame, with no video file needed.
        """
        height, width = frame.shape[:2]
        if self._line_counter is None or self._annotator is None:
            self._line_counter = LineCounter(self._camera.counting_line, width, height)
            self._annotator = FrameAnnotator(
                self._camera.counting_line, jpeg_quality=self._settings.jpeg_quality
            )

        if self._frame_index % self._settings.detect_every_n_frames == 0:
            self._last_detections = await asyncio.to_thread(self._detect_and_filter, frame)

        tracked = self._tracker.update(self._last_detections)
        class_names = self._detector.class_names

        crossings = self._line_counter.update(tracked, class_names)
        if crossings:
            await self._record_crossings(crossings, tracked, frame)

        now = utcnow()
        active_tracks = len(tracked)
        throughput_per_min = self._throughput.per_minute(now=now)
        congestion = derive_congestion(
            throughput_per_min=throughput_per_min,
            capacity_per_min=self._camera.capacity_per_min,
            active_tracks=active_tracks,
        )

        self._frame_index += 1
        self._state = CameraState(
            camera_id=self.camera_id,
            name=self._camera.name,
            status=PipelineStatus.RUNNING,
            updated_at=now,
            counts=self._line_counter.counts,
            active_tracks=active_tracks,
            throughput_per_min=throughput_per_min,
            congestion=congestion,
            pipeline_fps=self._measure_fps(),
            frames_processed=self._frame_index,
            anpr_enabled=self._settings.anpr_enabled,
            error=None,
        )
        # Before the Redis write, not after: metrics are in-memory, so a Redis outage
        # (which raises out of `publish_state`) must not also blind the scrape.
        self._publish_tick_metrics(self._state)
        await self._store.publish_state(self._state)

        jpeg = self._annotator.render(frame, tracked, class_names, self._state)
        await self._store.publish_frame(self.camera_id, jpeg)

    def _detect_and_filter(self, frame: np.ndarray) -> sv.Detections:
        """Runs the detector and drops anything outside `VEHICLE_CLASSES`. This is
        the pipeline-boundary filter: it applies regardless of whether the
        detector implementation already filtered, so an unexpected COCO label
        (or a future detector swap that doesn't filter) can never reach
        `CameraState`."""
        detections = self._detector.detect(frame)
        class_names = self._detector.class_names
        if detections.class_id is None or len(detections) == 0:
            return sv.Detections.empty()
        keep = np.array(
            [class_names.get(int(class_id)) in VEHICLE_CLASSES for class_id in detections.class_id],
            dtype=bool,
        )
        return detections[keep]

    async def _record_crossings(
        self,
        crossings: list[Crossing],
        tracked: sv.Detections,
        frame: np.ndarray,
    ) -> None:
        track_boxes: dict[int, tuple[float, float, float, float]] = {}
        if tracked.tracker_id is not None:
            for i, tracker_id in enumerate(tracked.tracker_id):
                track_boxes[int(tracker_id)] = tuple(float(v) for v in tracked.xyxy[i])

        events: list[CrossingEvent] = []
        for crossing in crossings:
            plate_text: str | None = None
            plate_confidence: float | None = None
            # Never invented: reading is attempted only when ANPR is enabled, and
            # a miss (no box, no model result) leaves both fields None.
            if self._settings.anpr_enabled:
                box = track_boxes.get(crossing.track_id)
                if box is not None:
                    result = self._plate_reader.read(frame, box)
                    if result is not None:
                        plate_text, plate_confidence = result

            events.append(
                CrossingEvent(
                    camera_id=self.camera_id,
                    track_id=crossing.track_id,
                    vehicle_class=crossing.vehicle_class,
                    direction=crossing.direction,
                    crossed_at=utcnow(),
                    confidence=crossing.confidence,
                    plate_text=plate_text,
                    plate_confidence=plate_confidence,
                )
            )
            log.info(
                "crossing",
                camera_id=self.camera_id,
                track_id=crossing.track_id,
                vehicle_class=crossing.vehicle_class,
                direction=crossing.direction.value,
            )

        # History and metrics BEFORE the Redis append: the crossing has already been
        # counted, so a Redis failure raising out of `append_events` must not also
        # cost the durable record of it.
        for event in events:
            self._submit_to_writer(event)
            self._count_crossing_metric(event)

        self._throughput.record(len(events))
        await self._store.append_events(events)

    def _submit_to_writer(self, event: CrossingEvent) -> None:
        if self._writer is None:
            return
        try:
            self._writer.submit(event)
        except Exception as exc:
            # `submit()` is a plain in-memory append and is not expected to fail, but
            # history is a side channel: whatever goes wrong here costs one record,
            # never the frame loop.
            log.warning(
                "history_submit_failed",
                camera_id=self.camera_id,
                track_id=event.track_id,
                error=str(exc),
            )

    def _count_crossing_metric(self, event: CrossingEvent) -> None:
        # `vehicle_class` is bounded by the VEHICLE_CLASSES filter at the pipeline
        # boundary and `camera_id` by the registry, so label cardinality is fixed.
        self._guarded_metric_update(
            lambda: metrics.crossings_total.labels(
                self.camera_id, event.direction.value, event.vehicle_class
            ).inc()
        )

    def _publish_tick_metrics(self, state: CameraState) -> None:
        def update() -> None:
            metrics.pipeline_frames_processed_total.labels(self.camera_id).inc()
            metrics.pipeline_fps.labels(self.camera_id).set(state.pipeline_fps)
            metrics.pipeline_active_tracks.labels(self.camera_id).set(state.active_tracks)
            metrics.pipeline_status.labels(self.camera_id).state(state.status.value)

        self._guarded_metric_update(update)

    def _publish_status_metric(self, status: PipelineStatus) -> None:
        self._guarded_metric_update(
            lambda: metrics.pipeline_status.labels(self.camera_id).state(status.value)
        )

    def _guarded_metric_update(self, update: Callable[[], None]) -> None:
        """Observability fails open: a metrics fault must never reach the frame loop."""
        try:
            update()
        except Exception as exc:
            if not self._metrics_failure_logged:
                self._metrics_failure_logged = True
                log.warning("metrics_update_failed", camera_id=self.camera_id, error=str(exc))

    def _measure_fps(self) -> float:
        """Frames actually processed per second, averaged over recent ticks.

        Averaging across `_FPS_WINDOW_TICKS` spans several detection ticks, so the
        figure is the sustained rate an operator can tune against rather than the
        speed of whichever tick happened to publish last.
        """
        self._tick_times.append(time.monotonic())
        if len(self._tick_times) < 2:
            return self._settings.target_fps
        span = self._tick_times[-1] - self._tick_times[0]
        if span <= 0:
            return self._settings.target_fps
        return (len(self._tick_times) - 1) / span

    async def _publish_error(self, message: str) -> None:
        self._state = self._state.model_copy(
            update={"status": PipelineStatus.ERROR, "updated_at": utcnow(), "error": message}
        )
        self._publish_status_metric(PipelineStatus.ERROR)
        await self._safe_publish_state()

    async def _publish_stopped(self) -> None:
        self._state = self._state.model_copy(
            update={"status": PipelineStatus.STOPPED, "updated_at": utcnow()}
        )
        self._publish_status_metric(PipelineStatus.STOPPED)
        await self._safe_publish_state()

    async def _safe_publish_state(self) -> None:
        try:
            await self._store.publish_state(self._state)
        except Exception as exc:
            # Publishing the error/stopped state must never itself crash the loop.
            log.error("state_publish_failed", camera_id=self.camera_id, error=str(exc))


def build_pipelines(
    settings: Settings, store: StateStore, writer: CrossingWriter | None = None
) -> list[CameraPipeline]:
    """One `CameraPipeline` per registered camera (`traffic_ai.cameras.CAMERAS`).

    All pipelines share one `writer`: batching only pays off if the cameras feed a
    single buffer rather than one INSERT stream each.
    """
    return [CameraPipeline(camera, store, settings, writer=writer) for camera in CAMERAS]
