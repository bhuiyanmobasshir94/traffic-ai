"""Entrypoint: `python -m traffic_ai.api` runs uvicorn inside the container.

Host and port are read from the environment rather than `traffic_ai.config.
Settings` — they describe how the process binds to the network, not the
domain configuration `Settings` owns, and adding them there would be a change
to a file this task does not own.
"""

from __future__ import annotations

import os

import uvicorn

from traffic_ai.api.app import create_app

app = create_app()

# How long uvicorn waits, after SIGTERM, for open requests to finish before it cancels
# them. Without a bound it waits forever, and an MJPEG stream on a healthy pipeline
# never finishes: one open browser tab would hold the container until the orchestrator
# SIGKILLs it, skipping the lifespan teardown that drains the history writer. Kept short
# so that teardown still fits inside the orchestrator's stop grace period (compose
# defaults to 10s; Kubernetes to 30s) -- uvicorn runs it only AFTER this wait ends.
GRACEFUL_SHUTDOWN_SECONDS = 5


def main() -> None:
    host = os.environ.get("TRAFFIC_AI_API_HOST", "0.0.0.0")
    port = int(os.environ.get("TRAFFIC_AI_API_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS)


if __name__ == "__main__":
    main()
