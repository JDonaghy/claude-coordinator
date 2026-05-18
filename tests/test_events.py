"""Tests for SSE event source, subscriber fan-out, and live-log streaming."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from coord.agent import AgentServer
from coord.agent_app import build_app
from coord.events import (
    ASSIGNMENT_COMPLETED,
    ASSIGNMENT_STARTED,
    BOARD_UPDATED,
    EventSource,
    build_events_route,
    publish_assignment_completed,
    publish_assignment_started,
    publish_board_updated,
    sse_response,
    stream_assignment_log,
)


# ── EventSource: in-memory fan-out ──────────────────────────────────────────


def test_publish_increments_event_ids() -> None:
    source = EventSource()
    e1 = source.publish(BOARD_UPDATED, {"x": 1})
    e2 = source.publish(BOARD_UPDATED, {"x": 2})
    assert e1.id == 1
    assert e2.id == 2
    assert e2.id > e1.id


def test_event_to_sse_format() -> None:
    source = EventSource()
    event = source.publish(ASSIGNMENT_STARTED, {"id": "abc", "msg": "hi"})
    sse = event.to_sse()
    assert sse.startswith("id: 1\n")
    assert "event: assignment_started\n" in sse
    assert "data: " in sse
    assert sse.endswith("\n\n")


def test_event_to_sse_handles_multiline_data() -> None:
    source = EventSource()
    event = source.publish(BOARD_UPDATED, "line1\nline2\nline3")
    sse = event.to_sse()
    # JSON-encoded so newlines become escapes — but verify SSE framing still
    # ends with the blank-line terminator.
    assert sse.endswith("\n\n")
    assert "id: 1" in sse


@pytest.mark.asyncio
async def test_subscriber_receives_new_events() -> None:
    source = EventSource()
    received: list = []
    gen = source.subscribe()

    async def consume() -> None:
        async for event in gen:
            received.append(event)
            if len(received) == 2:
                return

    task = asyncio.create_task(consume())
    # Yield so the subscriber actually registers before we publish.
    await asyncio.sleep(0.01)
    source.publish(ASSIGNMENT_STARTED, {"n": 1})
    source.publish(ASSIGNMENT_COMPLETED, {"n": 2})
    await asyncio.wait_for(task, timeout=2.0)
    assert [e.type for e in received] == [ASSIGNMENT_STARTED, ASSIGNMENT_COMPLETED]
    await gen.aclose()
    assert source.subscriber_count == 0


@pytest.mark.asyncio
async def test_multiple_subscribers_each_receive_all_events() -> None:
    source = EventSource()
    a_received: list = []
    b_received: list = []
    gen_a = source.subscribe()
    gen_b = source.subscribe()

    async def consume(gen, out: list) -> None:
        async for event in gen:
            out.append(event)
            if len(out) == 3:
                return

    task_a = asyncio.create_task(consume(gen_a, a_received))
    task_b = asyncio.create_task(consume(gen_b, b_received))
    await asyncio.sleep(0.01)
    for i in range(3):
        source.publish(BOARD_UPDATED, {"i": i})
    await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)
    assert [e.data["i"] for e in a_received] == [0, 1, 2]
    assert [e.data["i"] for e in b_received] == [0, 1, 2]
    await gen_a.aclose()
    await gen_b.aclose()


@pytest.mark.asyncio
async def test_subscribe_with_last_event_id_backfills() -> None:
    source = EventSource()
    source.publish(BOARD_UPDATED, {"n": 1})
    source.publish(BOARD_UPDATED, {"n": 2})
    source.publish(BOARD_UPDATED, {"n": 3})

    received: list = []
    gen = source.subscribe(last_event_id=1)

    async def consume() -> None:
        async for event in gen:
            received.append(event)
            if len(received) == 2:
                return

    await asyncio.wait_for(asyncio.create_task(consume()), timeout=2.0)
    await gen.aclose()
    assert [e.id for e in received] == [2, 3]


@pytest.mark.asyncio
async def test_subscribe_without_last_event_id_skips_history() -> None:
    source = EventSource()
    source.publish(BOARD_UPDATED, {"n": 1})
    received: list = []
    gen = source.subscribe()

    async def consume() -> None:
        async for event in gen:
            received.append(event)
            return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    # Subscriber with no Last-Event-ID gets only events from now on.
    assert source.subscriber_count == 1
    source.publish(BOARD_UPDATED, {"n": 2})
    await asyncio.wait_for(task, timeout=2.0)
    await gen.aclose()
    assert len(received) == 1
    assert received[0].data == {"n": 2}


@pytest.mark.asyncio
async def test_subscribe_cleanup_on_aclose() -> None:
    source = EventSource()

    async def open_and_close() -> None:
        gen = source.subscribe()
        # Step into the generator far enough to register the subscriber.
        task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0.01)
        assert source.subscriber_count == 1
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        await gen.aclose()

    await open_and_close()
    assert source.subscriber_count == 0


@pytest.mark.asyncio
async def test_slow_subscriber_does_not_block_publish() -> None:
    """A full queue on one subscriber must not raise from `publish` — it drops
    oldest on that subscriber so the producer keeps going."""
    source = EventSource(queue_maxsize=2)

    # Register a slow subscriber that never consumes.
    slow_gen = source.subscribe()
    slow_task = asyncio.create_task(slow_gen.__anext__())
    await asyncio.sleep(0.01)
    assert source.subscriber_count == 1

    # Publishing many more than queue_maxsize must not raise.
    for i in range(20):
        source.publish(BOARD_UPDATED, {"i": i})

    # The slow subscriber received whichever event happened to win the race
    # when the queue was unblocked — we just need publish to have survived.
    first = await asyncio.wait_for(slow_task, timeout=1.0)
    assert first.type == BOARD_UPDATED

    await slow_gen.aclose()


# ── Helper publishers ───────────────────────────────────────────────────────


def test_publish_helpers_use_correct_event_types() -> None:
    source = EventSource()

    class StubAssignment:
        def to_dict(self) -> dict:
            return {"id": "x", "status": "running"}

    e1 = publish_assignment_started(source, StubAssignment())
    e2 = publish_assignment_completed(source, StubAssignment())
    e3 = publish_board_updated(source, {"active": 2})

    assert e1.type == "assignment_started"
    assert e1.data == {"id": "x", "status": "running"}
    assert e2.type == "assignment_completed"
    assert e3.type == "board_updated"
    assert e3.data == {"active": 2}


# ── /events SSE response (tested via direct ASGI invocation) ────────────────
#
# Starlette's sync TestClient buffers streaming responses oddly, so we drive
# the ASGI app directly and collect the streamed body chunks. This is closer
# to how a real SSE client (httpx async, EventSource in the browser) sees it.


def test_build_events_route_mounts_under_starlette() -> None:
    """Smoke test: the route builder produces a mountable Starlette route."""
    source = EventSource()
    app = Starlette(routes=[build_events_route(source)])
    # A real HEAD/CONNECT isn't going to work via TestClient against a
    # never-ending GET, but we can at least verify the app accepts the route.
    assert any(r.path == "/events" for r in app.router.routes)


async def _collect_sse(
    asgi_app: Starlette,
    path: str,
    *,
    headers: dict | None = None,
    until: str,
    timeout: float = 2.0,
) -> str:
    """Drive an ASGI app, collect chunks until `until` appears in the body."""
    received: list[bytes] = []
    done = asyncio.Event()

    async def receive() -> dict:
        if done.is_set():
            return {"type": "http.disconnect"}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.body":
            received.append(message.get("body", b""))
            joined = b"".join(received).decode("utf-8", errors="replace")
            if until in joined:
                done.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    }

    task = asyncio.create_task(asgi_app(scope, receive, send))
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    return b"".join(received).decode("utf-8", errors="replace")


@pytest.mark.asyncio
async def test_events_endpoint_streams_published_events() -> None:
    source = EventSource()
    # Pre-publish so the backfill (via Last-Event-ID: 0) gives a deterministic
    # event without needing to coordinate a background publisher.
    source.publish(ASSIGNMENT_STARTED, {"id": "abc"})

    app = Starlette(routes=[build_events_route(source, keepalive_seconds=0.1)])
    body = await _collect_sse(
        app,
        "/events",
        headers={"last-event-id": "0"},
        until="event: assignment_started",
    )
    assert "event: assignment_started" in body
    assert re.search(r"id: \d+", body)
    assert '"id": "abc"' in body


@pytest.mark.asyncio
async def test_events_endpoint_respects_last_event_id_header() -> None:
    source = EventSource()
    source.publish(BOARD_UPDATED, {"n": 1})
    source.publish(BOARD_UPDATED, {"n": 2})
    source.publish(BOARD_UPDATED, {"n": 3})

    app = Starlette(routes=[build_events_route(source, keepalive_seconds=0.1)])
    body = await _collect_sse(
        app,
        "/events",
        headers={"last-event-id": "2"},
        until='"n": 3',
    )
    # We resumed past id 2 — earlier events must not appear in the backfill.
    assert '"n": 3' in body
    assert '"n": 1' not in body
    assert '"n": 2' not in body


# ── /stream/{id} live-log endpoint ──────────────────────────────────────────


def _agent_app(tmp_path: Path, *, argv: list[str]) -> tuple[Starlette, AgentServer]:
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv,
    )
    return build_app(server), server


def _payload(tmp_path: Path) -> dict:
    return {
        "repo_name": "api",
        "repo_path": str(tmp_path),
        "issue_number": 1,
        "issue_title": "stream test",
        "briefing": "stream",
        "files_allowed": [],
        "files_forbidden": [],
        "branch": "main",
    }


@pytest.mark.asyncio
async def test_stream_endpoint_yields_log_lines(tmp_path: Path) -> None:
    # Run the assignment via the sync HTTP layer (the worker subprocess uses
    # threads); then exercise /stream via direct ASGI invocation.
    app, server = _agent_app(
        tmp_path,
        argv=["/bin/sh", "-c", "echo MARKER-FROM-WORKER"],
    )
    try:
        client = TestClient(app)
        r = client.post("/assign", json=_payload(tmp_path))
        aid = r.json()["id"]
        server.wait_for(aid)

        body = await _collect_sse(
            app,
            f"/stream/{aid}",
            until="event: end",
        )
        assert "MARKER-FROM-WORKER" in body
        assert "event: log" in body
        assert "event: end" in body
        assert re.search(r"id: \d+", body)
    finally:
        server.shutdown()


def test_stream_endpoint_404_for_unknown_id(tmp_path: Path) -> None:
    app, server = _agent_app(tmp_path, argv=["/bin/sh", "-c", "true"])
    try:
        client = TestClient(app)
        r = client.get("/stream/missing")
        assert r.status_code == 404
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_stream_endpoint_supports_resume_via_last_event_id(tmp_path: Path) -> None:
    """Last-Event-ID is a byte offset — start from there, skip earlier output."""
    app, server = _agent_app(
        tmp_path,
        argv=["/bin/sh", "-c", "echo AAA && echo BBB && echo CCC"],
    )
    try:
        client = TestClient(app)
        r = client.post("/assign", json=_payload(tmp_path))
        aid = r.json()["id"]
        server.wait_for(aid)

        log_path = Path(server.get(aid).log_path)
        raw = log_path.read_bytes()
        # The argv header line in the log also mentions CCC; we want the
        # offset of the actual stdout `CCC\n`, which is the last occurrence.
        ccc_offset = raw.rfind(b"CCC")
        assert ccc_offset > 0

        body = await _collect_sse(
            app,
            f"/stream/{aid}",
            headers={"last-event-id": str(ccc_offset)},
            until="event: end",
        )
        assert "CCC" in body
        # AAA lives before the resume offset; it must not reappear.
        assert "AAA" not in body
    finally:
        server.shutdown()
