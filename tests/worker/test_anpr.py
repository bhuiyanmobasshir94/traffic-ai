"""`NullPlateReader` and `build_plate_reader` — no plate is ever fabricated."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from traffic_ai.worker.anpr import NullPlateReader, build_plate_reader


def test_null_plate_reader_always_returns_none() -> None:
    reader = NullPlateReader()
    frame = np.zeros((100, 100, 3), dtype=np.uint8)

    assert reader.read(frame, (0.0, 0.0, 10.0, 10.0)) is None
    assert reader.read(frame, (5.0, 5.0, 95.0, 95.0)) is None


def test_build_plate_reader_returns_null_reader_when_anpr_disabled(settings) -> None:
    disabled = settings.model_copy(update={"anpr_enabled": False, "anpr_model_path": None})

    reader = build_plate_reader(disabled)

    assert isinstance(reader, NullPlateReader)


def test_build_plate_reader_raises_when_enabled_without_a_model(settings) -> None:
    misconfigured = settings.model_copy(update={"anpr_enabled": True, "anpr_model_path": None})

    with pytest.raises(RuntimeError):
        build_plate_reader(misconfigured)


def test_build_plate_reader_does_not_raise_when_enabled_with_a_model_path(settings) -> None:
    # No real plate-reading implementation ships with this project yet — the
    # seam exists, but the only concrete reader is still `NullPlateReader`.
    configured = settings.model_copy(
        update={"anpr_enabled": True, "anpr_model_path": Path("models/plate.onnx")}
    )

    reader = build_plate_reader(configured)

    assert isinstance(reader, NullPlateReader)
