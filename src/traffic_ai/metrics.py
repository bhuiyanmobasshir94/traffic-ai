"""Prometheus metrics: HTTP instrumentation and pipeline gauges.

Everything here uses its OWN `CollectorRegistry` (`registry`, below) rather
than `prometheus_client`'s process-global default registry. Registering
against the default registry makes the test suite order-dependent — two
tests that each build an app via `create_app()` would try to register the
same metric name twice against one shared global and raise
`ValueError: Duplicated timeseries` on the second import-adjacent app build,
or on test re-collection. A private registry makes this module import-safe
and re-import-safe, at the cost of one extra argument (`registry=registry`)
on every metric constructor below.

This module only *declares* the metrics and how to render them. Nothing here
scrapes or increments on its own:

- HTTP instrumentation is wired in `traffic_ai.api.app` (a middleware reads
  `http_requests_total` / `http_request_duration_seconds` on every request).
- The pipeline gauges and counters below are declared for the worker to set
  as it runs (`worker/pipeline.py` is out of scope for this change — see
  that module's owner for the wiring). They are safe to import and leave at
  their zero value until something sets them.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Enum,
    Gauge,
    Histogram,
    generate_latest,
)

from traffic_ai.domain import PipelineStatus

# A private registry — see module docstring for why this is not
# `prometheus_client.REGISTRY` (the global default).
registry = CollectorRegistry()

# --- HTTP ---------------------------------------------------------------
# `path` is always the matched ROUTE TEMPLATE (e.g.
# "/api/cameras/{camera_id}/frame.jpg"), never the raw request path. The
# camera id, a query string, or an attacker-supplied 404 path are all
# unbounded in cardinality; a raw-path label would mint a new Prometheus
# timeseries per distinct value ever seen, which is the textbook way to
# exhaust a Prometheus server's memory. See
# `traffic_ai.api.app._metrics_middleware` for where this label is computed.
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled, by method, route template, and status code.",
    ["method", "path", "status"],
    registry=registry,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds, by method and route template.",
    ["method", "path"],
    registry=registry,
)

# --- pipeline -------------------------------------------------------------
# Settable by the worker process as each camera pipeline runs. All of these
# default to zero/unknown until something calls `.labels(...).set(...)` or
# `.inc()` — importing this module never implies a pipeline is running.
pipeline_frames_processed_total = Counter(
    "pipeline_frames_processed_total",
    "Frames processed by a camera pipeline, cumulative since process start.",
    ["camera_id"],
    registry=registry,
)

pipeline_fps = Gauge(
    "pipeline_fps",
    "Most recently measured processing rate of a camera pipeline, in frames per second.",
    ["camera_id"],
    registry=registry,
)

pipeline_active_tracks = Gauge(
    "pipeline_active_tracks",
    "Number of tracked objects currently active in a camera pipeline.",
    ["camera_id"],
    registry=registry,
)

# An `Enum` metric rather than a numeric `Gauge`: pipeline status is
# categorical (see `traffic_ai.domain.PipelineStatus`), and `Enum` renders
# that as one series per possible state with value 1 for the current one and
# 0 for the rest, which is the standard Prometheus idiom for "which of these
# named states am I in" — a numeric encoding would make every PromQL query
# against it carry an undocumented lookup table.
pipeline_status = Enum(
    "pipeline_status",
    "Current lifecycle status of a camera pipeline.",
    ["camera_id"],
    states=[status.value for status in PipelineStatus],
    registry=registry,
)

crossings_total = Counter(
    "crossings_total",
    "Vehicle crossings counted, by camera, direction, and vehicle class.",
    ["camera_id", "direction", "vehicle_class"],
    registry=registry,
)


def render() -> tuple[bytes, str]:
    """Render the current state of `registry` in Prometheus text exposition
    format, paired with the content type that must accompany it."""
    return generate_latest(registry), CONTENT_TYPE_LATEST
