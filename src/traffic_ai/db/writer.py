"""Background batching writer: crossings go to Postgres without touching the frame loop.

One INSERT per vehicle would put a network round-trip inside the pipeline tick, so
`submit()` only appends to memory and a separate `run()` task flushes in batches.

The governing rule is that a database failure must never reach the pipeline. A
flush that raises is logged and its batch is abandoned — history is lost, the live
dashboard is not. The batch is deliberately not re-queued: retrying through a long
outage would only grow the buffer toward its cap and then evict newer events in
favour of older ones.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Sequence
from typing import Protocol

from traffic_ai.config import Settings
from traffic_ai.domain import CrossingEvent
from traffic_ai.logging import get_logger

log = get_logger(__name__)

# A fixed ceiling rather than a Settings field: it is a safety bound against an
# unbounded buffer during an outage, not an operational knob. At the default flush
# settings this is minutes of traffic across both cameras.
DEFAULT_BUFFER_LIMIT = 10_000


class _EventSink(Protocol):
    """The slice of `CrossingRepository` the writer depends on.

    Structural, so the writer is testable against a stub with no database and no
    SQLAlchemy session in sight.
    """

    async def add_many(self, events: Sequence[CrossingEvent]) -> int: ...


class CrossingWriter:
    """Buffers `CrossingEvent`s and flushes them in batches from a background task."""

    def __init__(
        self,
        sink: _EventSink,
        *,
        flush_interval_seconds: float,
        flush_max_batch: int,
        buffer_limit: int = DEFAULT_BUFFER_LIMIT,
    ) -> None:
        self._sink = sink
        self._flush_interval = flush_interval_seconds
        self._flush_max_batch = flush_max_batch
        self._buffer_limit = buffer_limit
        self._buffer: deque[CrossingEvent] = deque()
        self._dropped = 0
        self._stop_requested = False
        # Wakes the run loop early when a batch fills, so a burst is not held
        # back until the next interval tick.
        self._wake = asyncio.Event()

    @classmethod
    def from_settings(cls, sink: _EventSink, settings: Settings) -> CrossingWriter:
        return cls(
            sink,
            flush_interval_seconds=settings.db_flush_interval_seconds,
            flush_max_batch=settings.db_flush_max_batch,
        )

    @property
    def dropped_count(self) -> int:
        """Events evicted because the buffer was full — i.e. history lost to an outage."""
        return self._dropped

    @property
    def pending_count(self) -> int:
        return len(self._buffer)

    def submit(self, event: CrossingEvent) -> None:
        """Non-blocking and synchronous, so the pipeline's hot loop can call it freely.

        When the buffer is full the oldest event is evicted: during an outage the
        newest crossings are the ones worth keeping.
        """
        if len(self._buffer) >= self._buffer_limit:
            self._buffer.popleft()
            self._dropped += 1
            log.warning(
                "db.buffer_full_dropped_oldest",
                dropped_total=self._dropped,
                buffer_limit=self._buffer_limit,
            )
        self._buffer.append(event)
        if len(self._buffer) >= self._flush_max_batch:
            self._wake.set()

    def request_stop(self) -> None:
        self._stop_requested = True
        # Without this the loop would sleep out the rest of its interval before
        # noticing the stop, delaying shutdown by up to `flush_interval_seconds`.
        self._wake.set()

    async def run(self) -> None:
        """Flushes until `request_stop()`, then drains what is left. Never raises out."""
        while not self._stop_requested:
            # A timeout is the normal interval tick, not an error.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._flush_interval)
            self._wake.clear()
            await self._flush_one_batch()
            # A burst can leave more than one batch behind; re-arm so the rest is
            # not held until the next interval tick.
            if len(self._buffer) >= self._flush_max_batch:
                self._wake.set()

        # Drain in batch-sized chunks: a stop must not discard buffered crossings,
        # and one oversized INSERT after a long outage is exactly what batching avoids.
        while self._buffer:
            await self._flush_one_batch()

    async def _flush_one_batch(self) -> None:
        if not self._buffer:
            return
        batch = [
            self._buffer.popleft() for _ in range(min(len(self._buffer), self._flush_max_batch))
        ]
        try:
            await self._sink.add_many(batch)
        except Exception as exc:
            # Losing history is acceptable; taking the live dashboard down is not.
            # This is the one place a broad catch is correct — any driver, pool, or
            # network error must stop at this boundary.
            log.warning("db.flush_failed", error=str(exc), batch_size=len(batch))
