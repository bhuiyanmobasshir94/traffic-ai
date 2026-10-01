"""`CrossingWriter` — the batching, buffer-capping, and failure-isolation behaviour,
driven against a stub sink. No database, no SQLAlchemy session: the writer depends
only on an object with `add_many`, so everything here runs anywhere.

The property that matters most is the first one tested for failure: a sink that
raises must never propagate out of `run()`, because the writer runs next to the
live pipeline and a database outage is not allowed to take the dashboard down.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import pytest
import structlog
from sqlalchemy.exc import DBAPIError

from traffic_ai import metrics
from traffic_ai.db import writer as writer_module
from traffic_ai.db.writer import CrossingWriter
from traffic_ai.domain import CrossingEvent, Direction

# Generous: these bound a hang, they are not a performance assertion.
_TIMEOUT = 2.0


def _event(n: int) -> CrossingEvent:
    return CrossingEvent(
        camera_id="toll-plaza-a",
        track_id=n,
        vehicle_class="car",
        direction=Direction.INCOMING,
        crossed_at=datetime.now(UTC),
        confidence=0.9,
    )


class _StubSink:
    """Records every batch it receives. `fail_first` makes the first N calls raise."""

    def __init__(self, *, fail_first: int = 0, error: Exception | None = None) -> None:
        self.batches: list[list[int]] = []
        self.calls = 0
        self._fail_remaining = fail_first
        self._error = error or RuntimeError("database unavailable")

    async def add_many(self, events: Sequence[CrossingEvent]) -> int:
        self.calls += 1
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise self._error
        self.batches.append([e.track_id for e in events])
        return len(events)

    @property
    def delivered(self) -> list[int]:
        return [track_id for batch in self.batches for track_id in batch]


class _AlwaysFailingSink(_StubSink):
    async def add_many(self, events: Sequence[CrossingEvent]) -> int:
        self.calls += 1
        raise self._error


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = _TIMEOUT) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def _stop(writer: CrossingWriter, task: asyncio.Task[None]) -> None:
    writer.request_stop()
    await asyncio.wait_for(task, timeout=_TIMEOUT)


# --- batching ---------------------------------------------------------------


async def test_flushes_when_the_batch_fills_without_waiting_for_the_interval() -> None:
    sink = _StubSink()
    # An interval far longer than the test: only the size trigger can flush.
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=3)
    task = asyncio.create_task(writer.run())

    writer.submit(_event(0))
    writer.submit(_event(1))
    await asyncio.sleep(0.05)
    assert sink.calls == 0  # below the batch size and well inside the interval

    writer.submit(_event(2))
    await _wait_until(lambda: sink.calls == 1)

    assert sink.batches == [[0, 1, 2]]
    await _stop(writer, task)


async def test_flushes_a_partial_batch_when_the_interval_elapses() -> None:
    sink = _StubSink()
    writer = CrossingWriter(sink, flush_interval_seconds=0.05, flush_max_batch=100)
    task = asyncio.create_task(writer.run())

    writer.submit(_event(0))
    writer.submit(_event(1))
    await _wait_until(lambda: sink.delivered == [0, 1])

    await _stop(writer, task)


async def test_a_burst_larger_than_one_batch_is_split_into_batch_sized_writes() -> None:
    sink = _StubSink()
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=2)
    for n in range(5):
        writer.submit(_event(n))
    task = asyncio.create_task(writer.run())

    # Two full batches go out on the size trigger; the leftover single event is
    # below the threshold and, with a 60s interval, waits for the stop-drain.
    await _wait_until(lambda: sink.delivered == [0, 1, 2, 3])
    assert sink.batches == [[0, 1], [2, 3]]

    await _stop(writer, task)
    assert sink.batches == [[0, 1], [2, 3], [4]]


async def test_events_are_delivered_oldest_first() -> None:
    sink = _StubSink()
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=100)
    for n in (5, 3, 9):
        writer.submit(_event(n))
    task = asyncio.create_task(writer.run())

    await _stop(writer, task)

    assert sink.delivered == [5, 3, 9]


# --- failure isolation --------------------------------------------------------


async def test_a_failing_sink_does_not_propagate_and_the_writer_keeps_going() -> None:
    sink = _StubSink(fail_first=1)
    writer = CrossingWriter(sink, flush_interval_seconds=0.02, flush_max_batch=100)
    task = asyncio.create_task(writer.run())

    writer.submit(_event(0))
    await _wait_until(lambda: sink.calls >= 1)  # this flush raised
    assert not task.done()

    # The outage ends; a later event goes through on the same running writer.
    writer.submit(_event(1))
    await _wait_until(lambda: sink.delivered == [1])
    assert not task.done()

    await _stop(writer, task)
    assert task.exception() is None


async def test_a_failed_batch_is_abandoned_not_retried() -> None:
    sink = _StubSink(fail_first=1)
    writer = CrossingWriter(sink, flush_interval_seconds=0.02, flush_max_batch=100)
    task = asyncio.create_task(writer.run())

    writer.submit(_event(0))
    await _wait_until(lambda: sink.calls >= 1)
    writer.submit(_event(1))
    await _wait_until(lambda: 1 in sink.delivered)
    await _stop(writer, task)

    # Event 0 is lost by design: requeueing it through a long outage would only
    # grow the buffer toward its cap and evict newer events.
    assert sink.delivered == [1]
    assert writer.pending_count == 0


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), OSError("connection reset"), ValueError("bad row"), TimeoutError()],
)
async def test_any_exception_type_from_the_sink_is_contained(error: Exception) -> None:
    sink = _AlwaysFailingSink(error=error)
    writer = CrossingWriter(sink, flush_interval_seconds=0.02, flush_max_batch=100)
    task = asyncio.create_task(writer.run())

    writer.submit(_event(0))
    await _wait_until(lambda: sink.calls >= 1)
    assert not task.done()

    await _stop(writer, task)
    assert task.exception() is None


async def test_stopping_during_an_outage_still_terminates() -> None:
    # The drain must not loop forever on a sink that never recovers.
    sink = _AlwaysFailingSink()
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=2)
    for n in range(5):
        writer.submit(_event(n))
    task = asyncio.create_task(writer.run())

    await _stop(writer, task)

    assert writer.pending_count == 0
    assert sink.delivered == []


async def test_a_failed_flush_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _AlwaysFailingSink(error=RuntimeError("database unavailable"))
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=100)
    writer.submit(_event(0))

    with structlog.testing.capture_logs() as logs:
        # A fresh logger, so an earlier test that configured structlog (and cached
        # the module's logger) cannot hide the event from the capture.
        monkeypatch.setattr(writer_module, "log", structlog.get_logger("traffic_ai.db.writer"))
        task = asyncio.create_task(writer.run())
        await _stop(writer, task)

    failed = [entry for entry in logs if entry["event"] == "db.flush_failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "database unavailable"
    assert failed[0]["error_type"] == "RuntimeError"
    assert failed[0]["batch_size"] == 1
    assert failed[0]["lost"] == 1
    assert failed[0]["log_level"] == "warning"


async def test_a_failed_flush_logs_no_statement_parameters_or_driver_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`str()` of a SQLAlchemy error is the SQL, its bound parameters (crossing rows, and
    plate text once a model exists) and the driver message, which Postgres fills with
    `DETAIL: Key (...)=(...)`. None of it may reach a log line."""
    error = DBAPIError(
        "INSERT INTO crossing_events (plate_text) VALUES (%s)",
        ("SECRET-PLATE-0042",),
        Exception("duplicate key. DETAIL: Key (plate_text)=(SECRET-PLATE-0042) already exists."),
    )
    sink = _AlwaysFailingSink(error=error)
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=100)
    writer.submit(_event(0))

    with structlog.testing.capture_logs() as logs:
        monkeypatch.setattr(writer_module, "log", structlog.get_logger("traffic_ai.db.writer"))
        task = asyncio.create_task(writer.run())
        await _stop(writer, task)

    (failed,) = [entry for entry in logs if entry["event"] == "db.flush_failed"]
    assert failed["error_type"] == "DBAPIError"
    assert failed["error"] == "Exception"  # the driver exception's class, not its message
    assert "SECRET-PLATE-0042" not in repr(logs)
    assert "crossing_events" not in repr(logs)


# --- lost events are counted, and a poison row does not take a whole batch ----------


def _lost(reason: str) -> float:
    """Current value of `history_events_lost_total{reason}`. The registry is process-wide,
    so every assertion is a delta against a reading taken first."""
    return metrics.registry.get_sample_value("history_events_lost_total", {"reason": reason}) or 0.0


class _PoisonSink(_StubSink):
    """Rejects any batch containing a poison track id, as one bad row fails an INSERT."""

    def __init__(self, poison: set[int]) -> None:
        super().__init__()
        self._poison = poison

    async def add_many(self, events: Sequence[CrossingEvent]) -> int:
        if any(e.track_id in self._poison for e in events):
            self.calls += 1
            raise ValueError("a row the database refuses")
        return await super().add_many(events)


def test_both_loss_series_exist_before_anything_is_lost() -> None:
    """So `increase()` sees the first loss rather than a series appearing already at 1."""
    for reason in ("buffer_full", "flush_failed"):
        assert (
            metrics.registry.get_sample_value("history_events_lost_total", {"reason": reason})
            is not None
        )


async def test_a_poison_row_loses_half_a_batch_not_all_of_it() -> None:
    sink = _PoisonSink({2})
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=8)
    for n in range(8):
        writer.submit(_event(n))
    before = _lost("flush_failed")

    task = asyncio.create_task(writer.run())
    await _stop(writer, task)

    # The whole batch failed; the half without the poison row was retried and saved.
    assert sink.batches == [[4, 5, 6, 7]]
    assert writer.lost_count == 4  # the half that held the poison row
    assert _lost("flush_failed") - before == 4
    assert task.exception() is None


async def test_a_transient_failure_that_clears_on_the_split_loses_nothing() -> None:
    sink = _StubSink(fail_first=1)
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=4)
    for n in range(4):
        writer.submit(_event(n))
    before = _lost("flush_failed")

    task = asyncio.create_task(writer.run())
    await _stop(writer, task)

    assert sink.batches == [[0, 1], [2, 3]]
    assert writer.lost_count == 0
    assert _lost("flush_failed") - before == 0


async def test_the_retry_is_one_split_not_a_bisection() -> None:
    """During an outage every attempt is a failing round trip. A batch costs three (the
    whole, then each half) however large it is, not one per row."""
    sink = _AlwaysFailingSink()
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=64)
    for n in range(64):
        writer.submit(_event(n))
    before = _lost("flush_failed")

    task = asyncio.create_task(writer.run())
    await _stop(writer, task)

    assert sink.calls == 3
    assert writer.lost_count == 64
    assert _lost("flush_failed") - before == 64


async def test_a_failed_single_event_batch_is_counted_not_retried() -> None:
    sink = _AlwaysFailingSink()
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=100)
    writer.submit(_event(0))
    before = _lost("flush_failed")

    task = asyncio.create_task(writer.run())
    await _stop(writer, task)

    assert sink.calls == 1  # nothing to split
    assert writer.lost_count == 1
    assert _lost("flush_failed") - before == 1


async def test_buffer_evictions_and_failed_batches_both_count_toward_lost() -> None:
    sink = _AlwaysFailingSink()
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=100, buffer_limit=3)
    full_before, failed_before = _lost("buffer_full"), _lost("flush_failed")

    for n in range(5):
        writer.submit(_event(n))
    assert writer.lost_count == 2  # 0 and 1 were evicted, before any flush
    assert _lost("buffer_full") - full_before == 2

    task = asyncio.create_task(writer.run())
    await _stop(writer, task)

    assert writer.lost_count == 5  # ...and the 3 that survived were refused by the database
    assert _lost("flush_failed") - failed_before == 3
    assert writer.dropped_count == 2  # the existing counter still means evictions only


async def test_nothing_lost_means_a_zero_count() -> None:
    sink = _StubSink()
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=100)
    for n in range(3):
        writer.submit(_event(n))

    task = asyncio.create_task(writer.run())
    await _stop(writer, task)

    assert sink.delivered == [0, 1, 2]
    assert writer.lost_count == 0


# --- bounded buffer -----------------------------------------------------------


async def test_a_full_buffer_drops_the_oldest_event_and_counts_it() -> None:
    sink = _StubSink()
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=100, buffer_limit=3)

    for n in range(5):
        writer.submit(_event(n))

    assert writer.dropped_count == 2
    assert writer.pending_count == 3

    task = asyncio.create_task(writer.run())
    await _stop(writer, task)
    # 0 and 1 were evicted; the three newest survive, in order.
    assert sink.delivered == [2, 3, 4]


async def test_nothing_is_counted_as_dropped_below_the_limit() -> None:
    writer = CrossingWriter(
        _StubSink(), flush_interval_seconds=60.0, flush_max_batch=100, buffer_limit=3
    )

    for n in range(3):
        writer.submit(_event(n))

    assert writer.dropped_count == 0
    assert writer.pending_count == 3


async def test_the_buffer_stays_bounded_through_a_long_outage() -> None:
    sink = _AlwaysFailingSink()
    writer = CrossingWriter(sink, flush_interval_seconds=0.01, flush_max_batch=5, buffer_limit=20)
    task = asyncio.create_task(writer.run())

    for n in range(500):
        writer.submit(_event(n))
        assert writer.pending_count <= 20
        if n % 50 == 0:
            await asyncio.sleep(0.02)

    await _stop(writer, task)
    assert task.exception() is None


async def test_dropping_an_event_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = CrossingWriter(
        _StubSink(), flush_interval_seconds=60.0, flush_max_batch=100, buffer_limit=1
    )

    with structlog.testing.capture_logs() as logs:
        monkeypatch.setattr(writer_module, "log", structlog.get_logger("traffic_ai.db.writer"))
        writer.submit(_event(0))
        writer.submit(_event(1))

    dropped = [entry for entry in logs if entry["event"] == "db.buffer_full_dropped_oldest"]
    assert len(dropped) == 1
    assert dropped[0]["dropped_total"] == 1
    assert dropped[0]["log_level"] == "warning"


# --- stop / drain -------------------------------------------------------------


async def test_request_stop_drains_pending_events_before_exiting() -> None:
    sink = _StubSink()
    # Neither trigger can fire on its own: the batch is not full and the interval
    # is far away. Only the stop path can deliver these.
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=100)
    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0)  # let run() reach its wait

    for n in range(4):
        writer.submit(_event(n))
    assert sink.calls == 0

    await _stop(writer, task)

    assert sink.delivered == [0, 1, 2, 3]
    assert writer.pending_count == 0


async def test_request_stop_interrupts_the_interval_wait() -> None:
    writer = CrossingWriter(_StubSink(), flush_interval_seconds=60.0, flush_max_batch=100)
    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0)

    # If stop did not wake the loop this would sit out the 60s interval and hit
    # the timeout instead.
    await _stop(writer, task)


async def test_stop_requested_before_run_starts_still_drains() -> None:
    sink = _StubSink()
    writer = CrossingWriter(sink, flush_interval_seconds=60.0, flush_max_batch=100)
    writer.submit(_event(0))
    writer.request_stop()

    await asyncio.wait_for(writer.run(), timeout=_TIMEOUT)

    assert sink.delivered == [0]


async def test_stopping_an_idle_writer_writes_nothing() -> None:
    sink = _StubSink()
    writer = CrossingWriter(sink, flush_interval_seconds=0.02, flush_max_batch=100)
    task = asyncio.create_task(writer.run())
    await asyncio.sleep(0.05)

    await _stop(writer, task)

    assert sink.calls == 0
