"""Automatic number-plate recognition — a disabled seam, not a feature.

No plate-detection model ships with this project, so no plate is ever read and
none is ever invented. `CrossingEvent.plate_text` is `None` unless a real model
read it — `None` means "not read", never "unreadable" and never a placeholder.
See `docs/decisions/DECISIONS.md` (2026-08-10, "No plate is ever fabricated").
"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from traffic_ai.config import Settings


class PlateReader(Protocol):
    def read(
        self, frame: np.ndarray, xyxy: tuple[float, float, float, float]
    ) -> tuple[str, float] | None: ...


class NullPlateReader:
    """Always returns None. The default, and the only reader that ships with this
    project. Fabricating a plausible-looking plate here would be worse than
    reporting nothing — it would be indistinguishable from a real read."""

    def read(
        self, frame: np.ndarray, xyxy: tuple[float, float, float, float]
    ) -> tuple[str, float] | None:
        return None


def build_plate_reader(settings: Settings) -> PlateReader:
    """Fails closed: `anpr_enabled=True` with no `anpr_model_path` configured is a
    startup error, never a silent fallback to `NullPlateReader`. Enabling ANPR
    without a model is very likely a misconfiguration, and continuing to run with
    it silently disabled would hide that from whoever flipped the flag.
    """
    if settings.anpr_enabled and settings.anpr_model_path is None:
        raise RuntimeError(
            "TRAFFIC_AI_ANPR_ENABLED is set but no TRAFFIC_AI_ANPR_MODEL_PATH was "
            "provided — refusing to start with ANPR half-configured. Set a model "
            "path or disable ANPR."
        )
    return NullPlateReader()
