"""Runtime configuration.

Every setting is environment-driven with a working default, so the stack comes up
with no `.env` at all. Anything secret belongs in the environment — never here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TRAFFIC_AI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "production"] = "development"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- state store ------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"
    # State older than this is treated as stale rather than shown as current —
    # a frozen dashboard that looks live is worse than one that says it is stale.
    state_ttl_seconds: int = Field(default=30, ge=5)
    event_history: int = Field(default=200, ge=1, le=5000)

    # --- media ------------------------------------------------------------
    video_dir: Path = Path("data/videos")

    # --- inference --------------------------------------------------------
    # `torchvision` (BSD-3-Clause) is the default so the default deployment
    # carries no AGPL obligation. `ultralytics` (AGPL-3.0) stays fully
    # supported as an explicit opt-in — see the Licensing note in README.md.
    detector: Literal["torchvision", "ultralytics"] = "torchvision"
    model_weights: str = "yolov8n.pt"
    device: str = "cpu"
    confidence_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    iou_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # Detection is the expensive stage; the tracker interpolates between runs.
    # Raise this on a small server, lower it for accuracy.
    detect_every_n_frames: int = Field(default=2, ge=1)
    target_fps: float = Field(default=12.0, gt=0)
    frame_width: int = Field(default=960, ge=160)
    jpeg_quality: int = Field(default=75, ge=1, le=100)

    # --- ANPR -------------------------------------------------------------
    # No plate-detection model ships with this project, so no plate is ever read
    # and none is ever invented. The pipeline keeps the seam (worker/anpr.py);
    # enabling this without providing a model is a startup error, by design.
    anpr_enabled: bool = False
    anpr_model_path: Path | None = None

    # --- service wiring ---------------------------------------------------
    # Server-to-server, inside the compose network.
    api_internal_url: str = "http://worker:8000"
    # Browser-facing. Relative by default: Traefik routes /api on the same host,
    # so there is one DNS record, one certificate, and no CORS.
    api_public_url: str = "/api"
    api_timeout_seconds: float = Field(default=5.0, gt=0)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Cached so config is read once."""
    return Settings()
