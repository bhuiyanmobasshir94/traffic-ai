"""Redis-backed handoff between the inference worker and the UI.

Everything written here carries a TTL. If the worker dies, its state expires
rather than lingering as a dashboard that looks live but is frozen — the UI can
tell the difference between "no data" and "stale data" and says so.
"""

from __future__ import annotations

from datetime import UTC, datetime

import redis.asyncio as aioredis

from traffic_ai.domain import CameraState, CrossingEvent

_NAMESPACE = "traffic-ai"


def _state_key(camera_id: str) -> str:
    return f"{_NAMESPACE}:camera:{camera_id}:state"


def _frame_key(camera_id: str) -> str:
    return f"{_NAMESPACE}:camera:{camera_id}:frame"


def _events_key(camera_id: str) -> str:
    return f"{_NAMESPACE}:camera:{camera_id}:events"


class StateStore:
    """Async accessor for pipeline state, latest frames, and crossing events."""

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        ttl_seconds: int = 30,
        event_history: int = 200,
    ) -> None:
        self._redis = client
        self._ttl = ttl_seconds
        self._event_history = event_history

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        ttl_seconds: int = 30,
        event_history: int = 200,
    ) -> StateStore:
        client = aioredis.from_url(url, decode_responses=False)
        return cls(client, ttl_seconds=ttl_seconds, event_history=event_history)

    async def ping(self) -> bool:
        """Liveness probe for the readiness endpoint. Never raises."""
        try:
            return bool(await self._redis.ping())
        except Exception:
            # A probe reports False; it does not crash the caller. This is the one
            # place a broad catch is correct — readiness must survive Redis being down.
            return False

    # --- state ------------------------------------------------------------

    async def publish_state(self, state: CameraState) -> None:
        await self._redis.set(
            _state_key(state.camera_id),
            state.model_dump_json().encode(),
            ex=self._ttl,
        )

    async def read_state(self, camera_id: str) -> CameraState | None:
        raw = await self._redis.get(_state_key(camera_id))
        if raw is None:
            return None
        return CameraState.model_validate_json(raw)

    async def read_states(self, camera_ids: list[str]) -> dict[str, CameraState]:
        """Batch read. Missing cameras are simply absent from the result."""
        if not camera_ids:
            return {}
        raws = await self._redis.mget([_state_key(cid) for cid in camera_ids])
        out: dict[str, CameraState] = {}
        for cid, raw in zip(camera_ids, raws, strict=True):
            if raw is not None:
                out[cid] = CameraState.model_validate_json(raw)
        return out

    # --- frames -----------------------------------------------------------

    async def publish_frame(self, camera_id: str, jpeg: bytes) -> None:
        await self._redis.set(_frame_key(camera_id), jpeg, ex=self._ttl)

    async def read_frame(self, camera_id: str) -> bytes | None:
        return await self._redis.get(_frame_key(camera_id))

    # --- events -----------------------------------------------------------

    async def append_events(self, events: list[CrossingEvent]) -> None:
        """Append crossings. Trimmed to `event_history`; history is never rewritten."""
        if not events:
            return
        key = _events_key(events[0].camera_id)
        payloads = [e.model_dump_json().encode() for e in events]
        pipe = self._redis.pipeline()
        pipe.lpush(key, *payloads)
        pipe.ltrim(key, 0, self._event_history - 1)
        pipe.expire(key, self._ttl * 20)
        await pipe.execute()

    async def read_events(self, camera_id: str, limit: int = 50) -> list[CrossingEvent]:
        raws = await self._redis.lrange(_events_key(camera_id), 0, max(0, limit - 1))
        return [CrossingEvent.model_validate_json(r) for r in raws]

    async def read_events_multi(
        self, camera_ids: list[str], limit: int = 50
    ) -> list[CrossingEvent]:
        """Merged, newest-first feed across cameras."""
        merged: list[CrossingEvent] = []
        for cid in camera_ids:
            merged.extend(await self.read_events(cid, limit))
        merged.sort(key=lambda e: e.crossed_at, reverse=True)
        return merged[:limit]

    async def close(self) -> None:
        await self._redis.aclose()


def utcnow() -> datetime:
    """Single source of 'now'. Timezone-aware everywhere, so state ages correctly."""
    return datetime.now(UTC)
