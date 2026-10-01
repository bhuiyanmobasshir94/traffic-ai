"""Runtime configuration.

Every setting is environment-driven with a working default, so the stack comes up
with no `.env` at all. Anything secret belongs in the environment — never here.

Development defaults are permissive so a laptop needs no setup. Production is
where that stops: `_production_requires_hardening` refuses to construct Settings
for a production environment that is unauthenticated or still carrying the
throwaway database password. Failing at startup is the point — the alternative is
a public deployment that silently serves a live camera feed to anyone.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The password baked into the local-development database URL below. Deploying
# with this still in place is a configuration error, not a style preference, so
# it is named here and checked for explicitly.
DEV_DATABASE_PASSWORD = "traffic"  # noqa: S105 - a known-bad default, not a credential


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

    # --- persistence ------------------------------------------------------
    # Postgres is the system of record for crossing history; Redis stays the
    # live path and keeps its TTLs. The UI never connects here — it reads
    # history over HTTP, preserving the thin-viewer split.
    #
    # The default URL is for local development only. Production must override
    # it; `_production_requires_hardening` enforces that.
    database_url: str = (
        f"postgresql+asyncpg://traffic:{DEV_DATABASE_PASSWORD}@postgres:5432/traffic_ai"
    )
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    db_echo: bool = False
    # A database outage must not take the live dashboard down with it. When this
    # is off, or when writes fail, the pipeline keeps counting and only loses
    # history — degraded, not dead.
    persistence_enabled: bool = True
    # Crossings are buffered and flushed in batches: one INSERT per vehicle
    # would put a network round-trip inside the frame loop.
    db_flush_interval_seconds: float = Field(default=2.0, gt=0)
    db_flush_max_batch: int = Field(default=100, ge=1)

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

    # --- API authentication -----------------------------------------------
    # Bearer token for programmatic `/api` access. The browser reaches the
    # dashboard through Traefik's BasicAuth instead; this guards the API for
    # scripts and for anything bypassing the edge.
    #
    # Unset means auth is OFF, which is fine for local development and refused
    # in production unless `allow_unauthenticated` is set deliberately.
    api_token: SecretStr | None = None
    allow_unauthenticated: bool = False
    # Liveness and readiness stay open whatever the token says: a load balancer
    # cannot present credentials, and a probe that 401s looks like an outage.
    auth_exempt_paths: tuple[str, ...] = ("/api/healthz", "/api/readyz")

    # --- observability ----------------------------------------------------
    metrics_enabled: bool = True
    # Served inside the /api prefix so the existing Traefik router reaches it
    # with no new rule, and so the API token covers it.
    metrics_path: str = "/api/metrics"

    # --- edge hardening ---------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=120, ge=1)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0)
    # The MJPEG stream is one long-lived request per viewer, so it is counted
    # separately and far more tightly than ordinary JSON calls.
    rate_limit_stream_requests: int = Field(default=10, ge=1)
    security_headers_enabled: bool = True
    hsts_max_age_seconds: int = Field(default=31_536_000, ge=0)  # one year

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

    @property
    def auth_enabled(self) -> bool:
        """True when a token is configured and must be presented."""
        return self.api_token is not None

    @model_validator(mode="after")
    def _production_requires_hardening(self) -> Settings:
        """Refuse to build an unsafe production configuration.

        Fails closed: a misconfigured production deployment stops at startup
        with a named reason rather than coming up wide open. Development is
        untouched, so this never gets in the way of running locally.
        """
        if self.environment != "production":
            return self

        problems: list[str] = []

        if self.api_token is None and not self.allow_unauthenticated:
            problems.append(
                "TRAFFIC_AI_API_TOKEN is unset. Set it to a long random value, or set "
                "TRAFFIC_AI_ALLOW_UNAUTHENTICATED=true to run the API open on purpose."
            )

        if self.persistence_enabled and f":{DEV_DATABASE_PASSWORD}@" in self.database_url:
            problems.append(
                "TRAFFIC_AI_DATABASE_URL still carries the development password. Point it "
                "at the real database with real credentials."
            )

        if problems:
            raise ValueError(
                "Refusing to start in production with an unsafe configuration:\n  - "
                + "\n  - ".join(problems)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Cached so config is read once."""
    return Settings()
