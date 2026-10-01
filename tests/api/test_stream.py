"""The MJPEG stream endpoint.

404-on-unknown-camera is covered by the parametrized sweep in
`test_cameras.py::test_unknown_camera_is_404`. The frame-polling behavior
itself (skip-unchanged, ttl cap, disconnect handling) is exercised directly
against `_mjpeg_frames` rather than through a real HTTP round trip — driving
it end to end would mean waiting out `Settings.state_ttl_seconds` (floor of 5
real seconds) for every case, for no extra coverage.
"""

from __future__ import annotations

import asyncio

from traffic_ai.api.routes import _mjpeg_frames


async def _read_until(stream, marker: bytes, *, timeout: float) -> bytes:
    async def _read() -> bytes:
        chunk = b""
        async for part in stream:
            chunk += part
            if marker in chunk:
                return chunk
        return chunk

    return await asyncio.wait_for(_read(), timeout=timeout)


async def test_stream_starts_and_carries_the_latest_frame(
    app_factory, running_app, store, camera, settings
) -> None:
    jpeg = b"\xff\xd8fake-jpeg-bytes"
    await store.publish_frame(camera.camera_id, jpeg)

    # Floor of `Settings.state_ttl_seconds` (5s) bounds the worst case if the
    # ASGI test transport doesn't propagate an early client close promptly.
    fast_settings = settings.model_copy(update={"state_ttl_seconds": 5})
    app = app_factory(settings_override=fast_settings)
    async with (
        running_app(app) as client,
        client.stream("GET", f"/api/cameras/{camera.camera_id}/stream.mjpg") as resp,
    ):
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
        chunk = await _read_until(resp.aiter_bytes(), jpeg, timeout=5.0)

    assert chunk.startswith(b"--frame\r\n")
    assert b"Content-Type: image/jpeg\r\n" in chunk
    assert jpeg in chunk


async def test_mjpeg_frames_skips_unchanged_frames(store, camera) -> None:
    """An unchanged frame is polled but not re-sent, so a stalled pipeline
    does not saturate the connection; the stream ends once `ttl_seconds`
    passes with no *new* frame."""
    jpeg = b"\xff\xd8only-frame"
    await store.publish_frame(camera.camera_id, jpeg)

    chunks = [
        chunk async for chunk in _mjpeg_frames(store, camera.camera_id, fps=200.0, ttl_seconds=0.05)
    ]

    assert len(chunks) == 1
    assert jpeg in chunks[0]


async def test_mjpeg_frames_sends_each_new_frame(store, camera) -> None:
    async def publish_frames() -> None:
        for payload in (b"\xff\xd8one", b"\xff\xd8two", b"\xff\xd8three"):
            await store.publish_frame(camera.camera_id, payload)
            await asyncio.sleep(0.01)

    frames = _mjpeg_frames(store, camera.camera_id, fps=200.0, ttl_seconds=0.2)

    async def collect() -> list[bytes]:
        return [chunk async for chunk in frames]

    publisher = asyncio.create_task(publish_frames())
    chunks = await collect()
    await publisher

    assert b"\xff\xd8one" in b"".join(chunks)
    assert b"\xff\xd8two" in b"".join(chunks)
    assert b"\xff\xd8three" in b"".join(chunks)


async def test_mjpeg_frames_absorbs_cancellation(store, camera) -> None:
    """Client disconnect surfaces as `asyncio.CancelledError` inside the
    generator; it must be handled there, not propagate as a 500."""
    await store.publish_frame(camera.camera_id, b"\xff\xd8x")
    frames = _mjpeg_frames(store, camera.camera_id, fps=200.0, ttl_seconds=30.0)

    async def consume() -> None:
        async for _ in frames:
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    task.cancel()
    await task  # must not raise — the generator returns cleanly instead


# --- shutdown: an open stream must not hold the server up ---------------------------
#
# A viewer on a healthy pipeline never hits the stall exit, so without the shutdown event a
# single open tab keeps the stream (and so the graceful shutdown) alive until the
# orchestrator kills the container.


async def test_mjpeg_frames_ends_when_the_stop_event_is_set(store, camera) -> None:
    await store.publish_frame(camera.camera_id, b"\xff\xd8frame")
    stop = asyncio.Event()
    # A `ttl_seconds` far beyond the test: nothing but the event can end this stream.
    frames = _mjpeg_frames(store, camera.camera_id, fps=200.0, ttl_seconds=3600.0, stop=stop)
    received: list[bytes] = []

    async def consume() -> None:
        async for chunk in frames:
            received.append(chunk)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    assert received, "the stream should be live before it is told to stop"
    assert not consumer.done()

    stop.set()
    # `asyncio.wait`, not `wait_for`: `_mjpeg_frames` swallows `CancelledError` (a client
    # disconnect is not an error), so when `wait_for` times out and cancels the consumer, the
    # consumer "finishes" normally and `wait_for` returns without ever raising -- a stream that
    # ignores `stop` would pass. `asyncio.wait` does not cancel; `done` says what happened.
    done, _ = await asyncio.wait({consumer}, timeout=2.0)
    try:
        assert consumer in done, "the stream kept running after the stop event was set"
        assert consumer.exception() is None
    finally:
        consumer.cancel()  # a regressed stream must not outlive the test


async def test_mjpeg_frames_sends_nothing_when_already_stopped(store, camera) -> None:
    await store.publish_frame(camera.camera_id, b"\xff\xd8frame")
    stop = asyncio.Event()
    stop.set()

    frames = _mjpeg_frames(store, camera.camera_id, fps=200.0, ttl_seconds=3600.0, stop=stop)

    async def collect() -> list[bytes]:
        return [chunk async for chunk in frames]

    collector = asyncio.create_task(collect())
    done, _ = await asyncio.wait({collector}, timeout=2.0)  # see the note above on `wait_for`
    try:
        assert collector in done, "the stream kept running although the stop event was already set"
        assert collector.result() == []
    finally:
        collector.cancel()


async def test_the_stream_route_ends_when_the_app_is_shutting_down(
    app_factory, running_app, store, camera
) -> None:
    """The route hands the app's own shutdown event to the generator."""
    await store.publish_frame(camera.camera_id, b"\xff\xd8frame")
    app = app_factory()
    async with running_app(app) as client:
        app.state.shutdown_event.set()
        request = asyncio.create_task(client.get(f"/api/cameras/{camera.camera_id}/stream.mjpg"))
        done, _ = await asyncio.wait({request}, timeout=2.0)  # not `wait_for`: see above
        try:
            assert request in done, "the stream stayed open although the app is shutting down"
            resp = request.result()
        finally:
            request.cancel()
    assert resp.status_code == 200
    assert resp.content == b""


async def test_an_open_stream_is_ended_by_the_lifespan_teardown(
    app_factory, running_app, store, camera
) -> None:
    """End to end: a stream that is mid-flight when the app begins shutting down ends on its
    own once teardown starts -- the app is not left waiting on it."""
    await store.publish_frame(camera.camera_id, b"\xff\xd8frame")
    app = app_factory()
    async with running_app(app) as client:
        open_stream = asyncio.create_task(
            client.get(f"/api/cameras/{camera.camera_id}/stream.mjpg")
        )
        await asyncio.sleep(0.1)
        assert not open_stream.done(), "the stream should still be open while the app is serving"
    # Leaving `running_app` ran the lifespan teardown, which sets the shutdown event.

    done, _ = await asyncio.wait({open_stream}, timeout=2.0)  # not `wait_for`: see above
    try:
        assert open_stream in done, "the open stream was not ended by the lifespan teardown"
        resp = open_stream.result()
    finally:
        open_stream.cancel()
    assert resp.status_code == 200
    assert b"\xff\xd8frame" in resp.content
