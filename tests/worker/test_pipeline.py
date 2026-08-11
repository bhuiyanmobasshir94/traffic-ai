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
import supervision as sv

from traffic_ai.domain import PipelineStatus
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
