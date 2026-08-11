"""The API service: fronts the inference pipeline over HTTP.

Owns the process lifespan (`app.py`), the route table (`routes.py`), and the
FastAPI dependencies that bind requests to settings, the store, and the camera
allowlist (`dependencies.py`). This process doubles as the worker host: the
lifespan starts each camera pipeline as a background task.
"""

from __future__ import annotations
