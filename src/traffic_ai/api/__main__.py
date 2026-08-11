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


def main() -> None:
    host = os.environ.get("TRAFFIC_AI_API_HOST", "0.0.0.0")
    port = int(os.environ.get("TRAFFIC_AI_API_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
