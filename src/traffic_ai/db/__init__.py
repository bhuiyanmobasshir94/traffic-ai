"""Postgres persistence: the system of record for crossing history.

Redis (`traffic_ai.store`) stays the hot live path and keeps its TTLs; this package
is the durable, queryable history behind it. Nothing here is on the frame loop's
critical path — `writer.py` buffers crossings in memory and flushes them off the
loop, and a database failure degrades history, never the live dashboard.
"""

from __future__ import annotations
