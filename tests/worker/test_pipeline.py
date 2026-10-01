"""`CameraPipeline` — the composition root, tested against `StubDetector` and a
fakeredis-backed `StateStore`. Never opens a real video file: `_process_frame` is
the internal single-tick entry point that lets these tests drive the pipeline with
a synthetic frame, and the "never raises" retry loop is tested against a
deliberately nonexistent `video_dir`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
import structlog
import supervision as sv

from traffic_ai import metrics
from traffic_ai.domain import CrossingEvent, Direction, PipelineStatus
from traffic_ai.worker import pipeline as pipeline_module
from traffic_ai.worker.detection import StubDetector
from traffic_ai.worker.pipeline import CameraPipeline, build_pipelines
from traffic_ai.worker.tracking import ByteTrackTracker

_JPEG_MAGIC = b"\xff\xd8"


def _frame() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


class _RogueDetector:
    """Always reports one real vehicle box and one bogus, non-vehicle box — used
    to prove the pipeline-boundary filter (not just the detector) is what keeps
    an unexpected COCO label out of `CameraState`."""

    def __init__(self) -> None:
        self._class_names = {0: "car", 99: "airplane"}

    def detect(self, frame: np.ndarray) -> sv.Detections:
        return sv.Detections(
            xyxy=np.array([[10.0, 10.0, 50.0, 50.0], [60.0, 60.0, 90.0, 90.0]], dtype=np.float32),
            confidence=np.array([0.9, 0.9], dtype=np.float32),
            class_id=np.array([0, 99]),
        )

    @property
    def is_ready(self) -> bool:
        return True

    @property
    def class_names(self) -> dict[int, str]:
        return self._class_names


async def test_tick_publishes_a_camera_state_and_a_jpeg_frame(store, settings, camera) -> None:
    pipe = CameraPipeline(
        camera,
        store,
        settings,
        detector=StubDetector(boxes_per_frame=3),
        tracker=ByteTrackTracker(frame_rate=settings.target_fps),
    )

    await pipe._process_frame(_frame())

    assert pipe.state.status is PipelineStatus.RUNNING
    assert pipe.state.camera_id == camera.camera_id
    assert pipe.state.frames_processed == 1
    assert pipe.state.active_tracks == 3

    published = await store.read_state(camera.camera_id)
    assert published is not None
    assert published.status is PipelineStatus.RUNNING
    assert published.camera_id == camera.camera_id

    jpeg = await store.read_frame(camera.camera_id)
    assert jpeg is not None
    assert jpeg[:2] == _JPEG_MAGIC


async def test_state_is_readable_before_any_tick(store, settings, camera) -> None:
    pipe = CameraPipeline(
        camera, store, settings, detector=StubDetector(), tracker=ByteTrackTracker()
    )

    assert pipe.state.status is PipelineStatus.STARTING
    assert pipe.state.camera_id == camera.camera_id
    assert pipe.camera_id == camera.camera_id


async def test_frames_processed_accumulates_across_ticks(store, settings, camera) -> None:
    pipe = CameraPipeline(
        camera, store, settings, detector=StubDetector(), tracker=ByteTrackTracker()
    )

    for _ in range(3):
        await pipe._process_frame(_frame())

    assert pipe.state.frames_processed == 3


async def test_pipeline_boundary_drops_unexpected_classes(store, settings, camera) -> None:
    pipe = CameraPipeline(
        camera, store, settings, detector=_RogueDetector(), tracker=ByteTrackTracker()
    )

    await pipe._process_frame(_frame())

    # Only the "car" box survives — the "airplane" box is dropped at the pipeline
    # boundary even though the detector itself reported it.
    assert pipe.state.active_tracks == 1


async def test_run_never_raises_and_recovers_to_stopped(
    store, settings, camera, monkeypatch
) -> None:
    monkeypatch.setattr(pipeline_module, "_INITIAL_RETRY_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(pipeline_module, "_MAX_RETRY_BACKOFF_SECONDS", 0.02)

    broken_settings = settings.model_copy(update={"video_dir": Path("/nonexistent/does-not-exist")})
    pipe = CameraPipeline(
        camera,
        store,
        broken_settings,
        detector=StubDetector(),
        tracker=ByteTrackTracker(),
    )

    task = asyncio.create_task(pipe.run())

    # Poll for the first failed attempt rather than sleeping a fixed interval.
    # A fixed sleep races the pipeline's first decode attempt and fails
    # intermittently on a loaded machine — the assertion below then reports a
    # timing artifact as a logic error.
    async def _wait_for_error() -> None:
        while pipe.state.status is not PipelineStatus.ERROR:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait_for_error(), timeout=5.0)

    assert pipe.state.status is PipelineStatus.ERROR
    assert pipe.state.error is not None

    pipe.request_stop()
    await asyncio.wait_for(task, timeout=2.0)

    assert pipe.state.status is PipelineStatus.STOPPED
    assert not task.cancelled()
    assert task.exception() is None

    published = await store.read_state(camera.camera_id)
    assert published is not None
    assert published.status is PipelineStatus.STOPPED


async def test_build_pipelines_returns_one_pipeline_per_camera(store, settings) -> None:
    # No ultralytics in this environment, so build_detector falls back to
    # StubDetector for each camera — no weights download, matching the
    # "do not download model weights" constraint on this test suite.
    pipelines = build_pipelines(settings, store)

    assert len(pipelines) >= 1
    assert all(isinstance(p, CameraPipeline) for p in pipelines)
    assert {p.camera_id for p in pipelines} == {c.camera_id for c in pipeline_module.CAMERAS}


# --- history writer and metrics ----------------------------------------------
#
# Both are side channels, and the property under test is the same for each: they
# observe the frame loop and are never allowed to break it. Crossings are produced
# by a scripted tracker rather than a real one: ByteTrack would need a believable
# trajectory to hand out a stable id, and what is under test is what the pipeline
# does with a crossing, not whether the tracker can find one.


class _CrossingTracker:
    """Two tracked vehicles (a car and a motorcycle) that sit above the camera's
    counting line on the first tick and below it on every later one, so the second
    tick counts exactly two INCOMING crossings. The frame is 640x480 and the default
    line is at 55% height, i.e. y=264."""

    def __init__(self) -> None:
        self._calls = 0

    def update(self, detections: sv.Detections) -> sv.Detections:
        bottom = 100.0 if self._calls == 0 else 400.0
        self._calls += 1
        return sv.Detections(
            xyxy=np.array(
                [[100.0, bottom - 40, 140.0, bottom], [300.0, bottom - 40, 340.0, bottom]],
                dtype=np.float32,
            ),
            confidence=np.array([0.9, 0.8], dtype=np.float32),
            class_id=np.array([0, 1]),  # StubDetector: 0 -> car, 1 -> motorcycle
            tracker_id=np.array([1, 2]),
        )

    def reset(self) -> None:
        self._calls = 0


class _RecordingWriter:
    def __init__(self) -> None:
        self.events: list[CrossingEvent] = []

    def submit(self, event: CrossingEvent) -> None:
        self.events.append(event)


class _RaisingWriter:
    def __init__(self) -> None:
        self.attempts = 0

    def submit(self, event: CrossingEvent) -> None:
        self.attempts += 1
        raise RuntimeError("buffer exploded")


class _ExplodingMetric:
    """Every operation a pipeline performs on a metric raises."""

    def labels(self, *args: object, **kwargs: object) -> _ExplodingMetric:
        raise RuntimeError("metric exploded")


def _crossing_pipeline(store, settings, camera, *, writer=None) -> CameraPipeline:
    return CameraPipeline(
        camera,
        store,
        settings,
        detector=StubDetector(),
        tracker=_CrossingTracker(),
        writer=writer,
    )


def _sample(name: str, labels: dict[str, str]) -> float:
    return metrics.registry.get_sample_value(name, labels) or 0.0


def _crossings_metric(camera_id: str, direction: str, vehicle_class: str) -> float:
    return _sample(
        "crossings_total",
        {"camera_id": camera_id, "direction": direction, "vehicle_class": vehicle_class},
    )


def _status_metric(camera_id: str, state: str) -> float:
    return _sample("pipeline_status", {"camera_id": camera_id, "pipeline_status": state})


async def test_every_counted_crossing_is_submitted_to_the_writer(store, settings, camera) -> None:
    writer = _RecordingWriter()
    pipe = _crossing_pipeline(store, settings, camera, writer=writer)

    await pipe._process_frame(_frame())
    assert writer.events == []  # first sighting establishes a side; nothing crossed yet

    await pipe._process_frame(_frame())

    assert [(e.track_id, e.vehicle_class, e.direction) for e in writer.events] == [
        (1, "car", Direction.INCOMING),
        (2, "motorcycle", Direction.INCOMING),
    ]
    assert all(e.camera_id == camera.camera_id for e in writer.events)
    assert [e.confidence for e in writer.events] == pytest.approx([0.9, 0.8])
    # No plate model ships with the project: history carries "not read", not a stand-in.
    assert all(e.plate_text is None and e.plate_confidence is None for e in writer.events)

    # The very same events went to Redis: history and the live feed cannot disagree.
    live = await store.read_events(camera.camera_id, 10)
    assert sorted(e.track_id for e in live) == [1, 2]


async def test_a_pipeline_with_no_writer_still_counts_and_publishes(
    store, settings, camera
) -> None:
    pipe = _crossing_pipeline(store, settings, camera, writer=None)

    await pipe._process_frame(_frame())
    await pipe._process_frame(_frame())

    assert pipe.state.counts[Direction.INCOMING].total == 2
    assert len(await store.read_events(camera.camera_id, 10)) == 2


async def test_pipeline_survives_a_writer_that_raises(store, settings, camera, monkeypatch) -> None:
    writer = _RaisingWriter()
    pipe = _crossing_pipeline(store, settings, camera, writer=writer)

    with structlog.testing.capture_logs() as logs:
        # A fresh logger, so an earlier test that configured structlog (and cached
        # the module's logger) cannot hide the event from the capture.
        monkeypatch.setattr(
            pipeline_module, "log", structlog.get_logger("traffic_ai.worker.pipeline")
        )
        await pipe._process_frame(_frame())
        await pipe._process_frame(_frame())  # does not raise
        await pipe._process_frame(_frame())  # and the loop keeps going afterwards

    assert writer.attempts == 2  # one per crossing; a failure does not stop the next one
    assert pipe.state.status is PipelineStatus.RUNNING
    assert pipe.state.frames_processed == 3
    assert pipe.state.counts[Direction.INCOMING].total == 2
    # Live path unaffected: Redis still got both crossings and the state.
    assert len(await store.read_events(camera.camera_id, 10)) == 2
    published = await store.read_state(camera.camera_id)
    assert published is not None
    assert published.frames_processed == 3

    failed = [entry for entry in logs if entry["event"] == "history_submit_failed"]
    assert len(failed) == 2
    assert failed[0]["error"] == "buffer exploded"


async def test_history_is_handed_over_even_if_the_redis_append_fails(
    store, settings, camera, monkeypatch
) -> None:
    """The crossing is already counted by the time Redis is written. A Redis fault
    escapes `_process_frame` (and is caught by `run()`), but it must not also cost the
    durable record — the writer is fed first."""

    async def broken_append(events) -> None:
        raise ConnectionError("redis down")

    writer = _RecordingWriter()
    pipe = _crossing_pipeline(store, settings, camera, writer=writer)
    await pipe._process_frame(_frame())
    monkeypatch.setattr(store, "append_events", broken_append)

    with pytest.raises(ConnectionError):
        await pipe._process_frame(_frame())

    assert [e.track_id for e in writer.events] == [1, 2]


async def test_counted_crossings_increment_the_crossings_metric(store, settings, camera) -> None:
    pipe = _crossing_pipeline(store, settings, camera)
    cam = camera.camera_id
    car_before = _crossings_metric(cam, "incoming", "car")
    moto_before = _crossings_metric(cam, "incoming", "motorcycle")
    outgoing_before = _crossings_metric(cam, "outgoing", "car")

    await pipe._process_frame(_frame())
    assert _crossings_metric(cam, "incoming", "car") == car_before  # nothing crossed yet
    await pipe._process_frame(_frame())

    assert _crossings_metric(cam, "incoming", "car") == car_before + 1
    assert _crossings_metric(cam, "incoming", "motorcycle") == moto_before + 1
    assert _crossings_metric(cam, "outgoing", "car") == outgoing_before


async def test_each_tick_exports_frames_fps_tracks_and_status(store, settings, camera) -> None:
    pipe = CameraPipeline(
        camera,
        store,
        settings,
        detector=StubDetector(boxes_per_frame=3),
        tracker=ByteTrackTracker(frame_rate=settings.target_fps),
    )
    cam = {"camera_id": camera.camera_id}
    frames_before = _sample("pipeline_frames_processed_total", cam)

    await pipe._process_frame(_frame())
    await pipe._process_frame(_frame())

    assert _sample("pipeline_frames_processed_total", cam) == frames_before + 2
    assert _sample("pipeline_active_tracks", cam) == pipe.state.active_tracks == 3
    assert _sample("pipeline_fps", cam) == pytest.approx(pipe.state.pipeline_fps)
    assert _status_metric(camera.camera_id, "running") == 1.0
    assert _status_metric(camera.camera_id, "starting") == 0.0


async def test_status_metric_tracks_the_lifecycle(store, settings, camera) -> None:
    pipe = CameraPipeline(
        camera, store, settings, detector=StubDetector(), tracker=ByteTrackTracker()
    )
    cam = camera.camera_id

    # Visible to a scrape before the first frame is ever decoded.
    assert _status_metric(cam, "starting") == 1.0

    await pipe._publish_error("could not open video")
    assert _status_metric(cam, "error") == 1.0
    assert _status_metric(cam, "starting") == 0.0

    await pipe._publish_stopped()
    assert _status_metric(cam, "stopped") == 1.0
    assert _status_metric(cam, "error") == 0.0


async def test_pipeline_survives_metrics_that_raise(store, settings, camera, monkeypatch) -> None:
    """Observability fails open: a broken metric costs a graph, never a frame."""
    for name in (
        "crossings_total",
        "pipeline_frames_processed_total",
        "pipeline_fps",
        "pipeline_active_tracks",
        "pipeline_status",
    ):
        monkeypatch.setattr(pipeline_module.metrics, name, _ExplodingMetric())
    writer = _RecordingWriter()

    with structlog.testing.capture_logs() as logs:
        monkeypatch.setattr(
            pipeline_module, "log", structlog.get_logger("traffic_ai.worker.pipeline")
        )
        pipe = _crossing_pipeline(store, settings, camera, writer=writer)  # init also publishes
        await pipe._process_frame(_frame())
        await pipe._process_frame(_frame())  # a crossing tick: crossings_total raises here too
        await pipe._process_frame(_frame())
        await pipe._publish_error("x")
        await pipe._publish_stopped()

    assert pipe.state.status is PipelineStatus.STOPPED
    assert pipe.state.frames_processed == 3
    assert pipe.state.counts[Direction.INCOMING].total == 2
    # The writer and Redis were unaffected by the metric faults.
    assert [e.track_id for e in writer.events] == [1, 2]
    assert len(await store.read_events(camera.camera_id, 10)) == 2
    published = await store.read_state(camera.camera_id)
    assert published is not None
    assert published.status is PipelineStatus.STOPPED

    # Warned once, not once per tick: a metric that raises would do so ~12 times a second.
    warned = [entry for entry in logs if entry["event"] == "metrics_update_failed"]
    assert len(warned) == 1
    assert warned[0]["error"] == "metric exploded"


async def test_build_pipelines_shares_one_writer_across_cameras(store, settings) -> None:
    writer = _RecordingWriter()

    shared = build_pipelines(settings, store, writer)
    default = build_pipelines(settings, store)

    assert all(p._writer is writer for p in shared)
    assert all(p._writer is None for p in default)
