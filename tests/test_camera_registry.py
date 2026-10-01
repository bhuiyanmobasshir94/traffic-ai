"""Invariants of the camera registry that other layers rely on.

Named `test_camera_registry` rather than `test_cameras`: `tests/api/test_cameras.py`
already owns that basename and `tests/api` has no `__init__.py`, so pytest would refuse
to collect both.
"""

from __future__ import annotations

from traffic_ai.cameras import CAMERAS, CameraConfig, get_camera
from traffic_ai.domain import CameraState


def test_counting_is_on_by_default() -> None:
    camera = CameraConfig(
        camera_id="x", name="X", video_filename="x.mp4", latitude=0.0, longitude=0.0
    )

    assert camera.counting_enabled is True
    assert camera.counting_disabled_reason == ""


def test_camera_state_defaults_to_counting_enabled() -> None:
    """Additive contract field: a state from a worker that predates it still parses, and
    reads as a calibrated camera."""
    payload = {
        "camera_id": "toll-plaza-a",
        "name": "Toll Plaza A",
        "status": "running",
        "updated_at": "2026-10-01T12:00:00+00:00",
    }

    assert CameraState.model_validate(payload).counting_enabled is True


def test_every_camera_with_counting_off_says_why() -> None:
    """The UI shows the reason verbatim; a switched-off camera without one would be
    switched off with no explanation on screen."""
    for camera in CAMERAS:
        if not camera.counting_enabled:
            assert camera.counting_disabled_reason.strip(), camera.camera_id


def test_toll_plaza_b_is_uncalibrated_and_toll_plaza_a_counts() -> None:
    camera_a = get_camera("toll-plaza-a")
    camera_b = get_camera("toll-plaza-b")

    assert camera_a is not None
    assert camera_a.counting_enabled is True
    assert camera_b is not None
    assert camera_b.counting_enabled is False
    assert "not calibrated" in camera_b.counting_disabled_reason
