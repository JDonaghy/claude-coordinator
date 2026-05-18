"""SSE event source for the coordinator and live-log streaming for agents.

The dashboard (#9) mounts these on its HTTP server. Two things live here:

1. `EventSource` — fan-out hub. Producers call `publish(event_type, data)`;
   subscribers each get their own `asyncio.Queue`, so a slow client never
   blocks a fast one. Events carry a monotonic `id`, and a small ring buffer
   lets reconnecting clients resume via `Last-Event-ID`.

2. `sse_response(source, last_event_id=...)` — wraps the source as a Starlette
   `StreamingResponse` with the right headers (`text/event-stream`, no-cache,
   keepalive).

3. `stream_assignment_log(agent, assignment_id)` — async generator that tails
   the assignment log file and yields SSE-formatted chunks. The event `id` is
   the byte offset, which doubles as a `since` cursor for resume.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route


# ── Event types (the wire vocabulary) ───────────────────────────────────────

ASSIGNMENT_STARTED = "assignment_started"
ASSIGNMENT_COMPLETED = "assignment_completed"
ASSIGNMENT_FAILED = "assignment_failed"
MACHINE_CONNECTED = "machine_connected"
MACHINE_DISCONNECTED = "machine_disconnected"
BOARD_UPDATED = "board_updated"

KNOWN_EVENT_TYPES = frozenset({
    ASSIGNMENT_STARTED,
    ASSIGNMENT_COMPLETED,
    ASSIGNMENT_FAILED,
    MACHINE_CONNECTED,
    MACHINE_DISCONNECTED,
    BOARD_UPDATED,
})


# ── Tunables ────────────────────────────────────────────────────────────────

DEFAULT_HISTORY_SIZE = 256
DEFAULT_QUEUE_MAXSIZE = 128
DEFAULT_KEEPALIVE_SECONDS = 15.0
LOG_POLL_INTERVAL = 0.25
LOG_CHUNK_SIZE = 4096


@dataclass
class Event:
    """One thing that happened, ready to serialize as SSE."""

    id: int
    type: str
    data: Any
    created_at: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        # SSE requires data: prefix on every line. JSON-encode the payload so
        # multi-line strings and structured data both round-trip cleanly.
        payload = json.dumps(self.data, default=str)
        lines = [f"id: {self.id}", f"event: {self.type}"]
        for chunk in payload.splitlines() or [""]:
            lines.append(f"data: {chunk}")
        return "\n".join(lines) + "\n\n"


class EventSource:
    """In-memory pub/sub. One instance per coordinator process.

    Subscribers each get an independent `asyncio.Queue`. If a queue fills, the
    oldest event for that subscriber is dropped — bounded memory beats global
    backpressure here. A short ring buffer of recent events lets reconnecting
    clients resume from `Last-Event-ID`.
    """

    def __init__(
        self,
        *,
        history_size: int = DEFAULT_HISTORY_SIZE,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self._next_id = 1
        self._history: deque[Event] = deque(maxlen=history_size)
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_maxsize = queue_maxsize
        self._lock = asyncio.Lock()

    # ── Producer side ──────────────────────────────────────────────────────

    def publish(self, event_type: str, data: Any) -> Event:
        """Record an event and fan it out to current subscribers.

        Called from the same event loop that owns the queues. Synchronous —
        producers don't await — so this is cheap to call from board mutation
        sites. If a subscriber's queue is full we drop its oldest event so we
        can still enqueue the new one.
        """
        event = Event(id=self._next_id, type=event_type, data=data)
        self._next_id += 1
        self._history.append(event)
        for queue in list(self._subscribers):
            self._offer(queue, event)
        return event

    @staticmethod
    def _offer(queue: asyncio.Queue[Event], event: Event) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Concurrent subscriber pop won the race; give up silently.
                return

    # ── Consumer side ──────────────────────────────────────────────────────

    async def subscribe(
        self,
        *,
        last_event_id: int | None = None,
    ) -> AsyncIterator[Event]:
        """Yield events as they're published. Catches up via `last_event_id`.

        The returned async generator must be closed (via `aclose()` or by
        exiting `async for`) to release the subscriber slot. Reconnecting
        clients pass their last seen id; we replay from history if we still
        have it, otherwise they get only new events from now on.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_maxsize)
        async with self._lock:
            self._subscribers.add(queue)
            backfill = self._backfill(last_event_id)
        try:
            for event in backfill:
                yield event
            while True:
                event = await queue.get()
                yield event
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    def _backfill(self, last_event_id: int | None) -> list[Event]:
        if last_event_id is None:
            return []
        return [e for e in self._history if e.id > last_event_id]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def next_id(self) -> int:
        return self._next_id


# ── SSE response helpers ────────────────────────────────────────────────────

def _parse_last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def sse_response(
    source: EventSource,
    request: Request,
    *,
    keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS,
) -> StreamingResponse:
    """Wrap an EventSource as a Starlette streaming SSE response."""

    last_event_id = _parse_last_event_id(request)

    async def gen() -> AsyncIterator[bytes]:
        # Initial retry hint and a comment so the client knows we're open even
        # before any event is published.
        yield b"retry: 2000\n\n"
        async for chunk in _multiplex(
            source.subscribe(last_event_id=last_event_id),
            keepalive_seconds=keepalive_seconds,
            request=request,
        ):
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def build_events_route(
    source: EventSource,
    *,
    path: str = "/events",
    keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS,
) -> Route:
    """Convenience: a Starlette Route mounting `source` at `path` as SSE.

    The dashboard server (#9) mounts this on its app. Kept here so the wire
    format (headers, keepalive cadence, `Last-Event-ID` handling) lives in one
    place — callers shouldn't have to reimplement it.
    """

    async def handler(request: Request) -> StreamingResponse:
        return sse_response(source, request, keepalive_seconds=keepalive_seconds)

    return Route(path, handler, methods=["GET"])


# ── Board-state event helpers (called by board mutation sites) ──────────────

def publish_assignment_started(source: EventSource, assignment: Any) -> Event:
    return source.publish(ASSIGNMENT_STARTED, _assignment_payload(assignment))


def publish_assignment_completed(source: EventSource, assignment: Any) -> Event:
    return source.publish(ASSIGNMENT_COMPLETED, _assignment_payload(assignment))


def publish_assignment_failed(source: EventSource, assignment: Any) -> Event:
    return source.publish(ASSIGNMENT_FAILED, _assignment_payload(assignment))


def publish_machine_connected(source: EventSource, machine_name: str) -> Event:
    return source.publish(MACHINE_CONNECTED, {"machine": machine_name})


def publish_machine_disconnected(source: EventSource, machine_name: str) -> Event:
    return source.publish(MACHINE_DISCONNECTED, {"machine": machine_name})


def publish_board_updated(source: EventSource, summary: dict) -> Event:
    return source.publish(BOARD_UPDATED, summary)


def _assignment_payload(assignment: Any) -> dict:
    """Coerce an Assignment-like object to a JSON-safe dict for the wire."""
    if hasattr(assignment, "to_dict"):
        return assignment.to_dict()
    if hasattr(assignment, "__dict__"):
        return {k: v for k, v in vars(assignment).items() if not k.startswith("_")}
    if isinstance(assignment, dict):
        return assignment
    return {"value": str(assignment)}


async def _multiplex(
    events: AsyncIterator[Event],
    *,
    keepalive_seconds: float,
    request: Request,
) -> AsyncIterator[bytes]:
    """Yield SSE bytes from `events`, sending a keepalive comment when idle.

    Also bails out if the client disconnects, so we don't leak subscribers.
    """
    aiter = events.__aiter__()
    pending: asyncio.Task | None = None
    try:
        while True:
            if await request.is_disconnected():
                return
            if pending is None:
                pending = asyncio.create_task(aiter.__anext__())
            try:
                event = await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=keepalive_seconds,
                )
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            except StopAsyncIteration:
                pending = None
                return
            pending = None
            yield event.to_sse().encode("utf-8")
    finally:
        if pending is not None:
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, StopAsyncIteration, Exception):
                pass
        await aiter.aclose()


# ── Live log streaming for agent /stream/<id> ───────────────────────────────

async def stream_assignment_log(
    log_path: Path,
    *,
    is_active: "callable",
    request: Request,
    start_offset: int = 0,
    poll_interval: float = LOG_POLL_INTERVAL,
    keepalive_seconds: float = DEFAULT_KEEPALIVE_SECONDS,
) -> AsyncIterator[bytes]:
    """Tail `log_path` and yield SSE-formatted bytes.

    `is_active()` returns True while the assignment is still RUNNING — we keep
    polling for new bytes; once it returns False we drain anything that landed
    after the last read and then send a terminal `event: end`. The event `id`
    on each chunk is the byte offset after that chunk, so reconnecting clients
    can pass `Last-Event-ID` to resume mid-stream.
    """
    offset = max(0, int(start_offset))
    last_yield = time.monotonic()
    yield b"retry: 2000\n\n"
    while True:
        if await request.is_disconnected():
            return
        chunk, offset = _read_log_chunk(log_path, offset)
        if chunk:
            yield _format_log_event(offset, chunk)
            last_yield = time.monotonic()
            continue
        if not is_active():
            # One final drain after the worker exits — bytes can arrive
            # between our last read and the status flip.
            chunk, offset = _read_log_chunk(log_path, offset)
            if chunk:
                yield _format_log_event(offset, chunk)
            yield f"id: {offset}\nevent: end\ndata: {{}}\n\n".encode("utf-8")
            return
        if time.monotonic() - last_yield >= keepalive_seconds:
            yield b": keepalive\n\n"
            last_yield = time.monotonic()
        await asyncio.sleep(poll_interval)


def _read_log_chunk(log_path: Path, offset: int) -> tuple[str, int]:
    if not log_path.exists():
        return "", offset
    try:
        with open(log_path, "rb") as f:
            f.seek(offset)
            data = f.read(LOG_CHUNK_SIZE)
    except OSError:
        return "", offset
    if not data:
        return "", offset
    return data.decode("utf-8", errors="replace"), offset + len(data)


def _format_log_event(offset: int, text: str) -> bytes:
    lines = [f"id: {offset}", "event: log"]
    for line in text.splitlines() or [""]:
        lines.append(f"data: {line}")
    if text.endswith("\n"):
        lines.append("data: ")
    return ("\n".join(lines) + "\n\n").encode("utf-8")
