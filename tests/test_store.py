"""`StateStore.ping` -- the readiness probe must report, never raise, and never hang."""

from __future__ import annotations

import asyncio

import pytest

from traffic_ai import store as store_module
from traffic_ai.store import StateStore


class _Redis:
    """Just enough of a Redis client for `ping`, with a behaviour chosen per test."""

    def __init__(self, behaviour: str) -> None:
        self._behaviour = behaviour

    async def ping(self) -> bool:
        if self._behaviour == "hang":
            await asyncio.Event().wait()  # never set: a black-holed host
        if self._behaviour == "raise":
            raise ConnectionError("connection refused")
        return True


async def test_ping_is_true_against_a_live_client(store: StateStore) -> None:
    assert await store.ping() is True


async def test_ping_is_false_when_the_client_raises() -> None:
    assert await StateStore(_Redis("raise")).ping() is False  # type: ignore[arg-type]


async def test_ping_is_false_when_the_client_hangs_instead_of_waiting_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis host that drops packets would otherwise hold the readiness probe until the socket
    gives up -- longer than the orchestrator's own probe timeout, failing the probe for the
    wrong reason."""
    monkeypatch.setattr(store_module, "_PING_TIMEOUT_SECONDS", 0.05)

    result = await asyncio.wait_for(StateStore(_Redis("hang")).ping(), timeout=2.0)  # type: ignore[arg-type]

    assert result is False


def test_the_ping_bound_is_short_enough_for_an_orchestrator_probe() -> None:
    assert 0 < store_module._PING_TIMEOUT_SECONDS <= 5.0
