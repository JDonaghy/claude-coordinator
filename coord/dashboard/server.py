"""Web dashboard HTTP server — lightweight UI for phone-accessible coordination."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from coord import __version__
from coord.config import Config
from coord.dashboard.fixture import FixtureServer
from coord.dashboard.terminal import (
    SessionAttacher,
    TmuxSessionAttacher,
    resolve_session_target,
)
from coord.dispatch import AGENT_PORT
from coord.events import (
    ASSIGNMENT_COMPLETED,
    ASSIGNMENT_FAILED,
    BOARD_UPDATED,
    EventSource,
    build_events_route,
)
from coord.board_service import read_board, write_board
from coord.models import Assignment
from coord.network import check_all, fetch_status
from coord.openapi import build_spec, dataclass_schema, openapi_and_docs_routes
from coord.pipeline import PipelineView
from coord.state import load_proposals

DASHBOARD_DIR = Path(__file__).parent
# Built React webapp lives here after `npm run build` inside coord/dashboard/webapp/.
# When this directory is absent the server falls back to the legacy index.html so
# that existing behaviour (and the test suite) is completely unaffected.
WEBAPP_DIST = DASHBOARD_DIR / "webapp" / "dist"

# How often (seconds) the background poller queries agent servers.
_POLL_INTERVAL = 30.0
# How long (seconds) an assignment must be running with no agent record before
# it is flagged as possibly stuck.
_STUCK_THRESHOLD = 300.0  # 5 minutes

# #1217 fix iteration 1: api_sessions' per-machine tmux sweep timing knobs.
# A sweep taking at least this long looks like it hit the SSH ConnectTimeout
# (i.e. the machine is unreachable) rather than a normal fast tmux query.
_SESSIONS_SLOW_THRESHOLD = 3.0  # seconds; a healthy sweep is normally <1s
# Once a machine looks unreachable, skip re-probing it for this long — caps
# how often we pay the full SSH ConnectTimeout for a chronically down machine
# on every ~4s dashboard poll.
_SESSIONS_COOLDOWN = 20.0  # seconds before a down machine is re-probed

# Bug 1 fix: distinct event type for cancelled assignments so they are not
# bucketed as FAILED on the client.  Not yet in coord.events — defined here
# until a shared constants refactor can move it.
ASSIGNMENT_CANCELLED = "assignment_cancelled"
# #448: advisory (0-commit clean exit) is neither a green completion nor a
# red failure — it's a "needs attention" state.  Route it to a distinct
# event so the dashboard can style it appropriately (warning, not failure).
ASSIGNMENT_ADVISORY = "assignment_advisory"
# #846: an assignment running past its wall-clock threshold, or thrashing
# through fix/review rounds without converging (coord.notify.attention_signal
# — same detection core as the coordinator's GitHub-comment backstop).
# Detection + surfacing only.
ASSIGNMENT_NEEDS_ATTENTION = "assignment_needs_attention"


def _fetch_agent_status(host: str, port: int = AGENT_PORT, timeout: float = 5.0) -> dict | None:
    """Synchronous agent /status fetch — safe to call from a thread executor."""
    try:
        resp = httpx.get(f"http://{host}:{port}/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


async def _poll_once(
    config: Config,
    event_source: EventSource,
    seen_terminal: set[str],
    orphaned_since: dict[str, float],
    *,
    board=None,
    now: float | None = None,
    stuck_threshold: float = _STUCK_THRESHOLD,
    needs_attention_seen: set[str] | None = None,
) -> list[dict]:
    """One iteration of the background agent poller.

    Queries each machine's agent server, publishes ``assignment_completed`` /
    ``assignment_failed`` / ``assignment_cancelled`` SSE events on transitions,
    and returns a list of possibly-stuck assignment info dicts.

    Also publishes ``ASSIGNMENT_NEEDS_ATTENTION`` (#846) — a live counterpart
    to the coordinator's GitHub-comment backstop — the first time a running
    assignment trips the shared ``coord.notify.attention_signal`` wall-clock
    / non-convergence check. *needs_attention_seen* is caller-owned dedupe
    state (mirrors *seen_terminal*) so the toast fires once per assignment,
    not every poll interval; pass ``None`` to skip this check entirely (e.g.
    from callers that don't track dedupe state, such as older tests).

    Extracted to module level so unit tests can drive it directly without
    standing up a full HTTP server.
    """
    if board is None:
        board = read_board()
    if now is None:
        now = time.time()

    running = {
        a.assignment_id: a
        for a in board.active
        if a.status == "running"
        and a.assignment_id
        and a.assignment_id not in seen_terminal
    }
    if not running:
        return []

    if needs_attention_seen is not None:
        from coord.notify import attention_signal  # noqa: PLC0415

        for aid, assignment in running.items():
            if aid in needs_attention_seen:
                continue
            reason, detail = attention_signal(
                assignment_type=assignment.type,
                status=assignment.status,
                dispatched_at=assignment.dispatched_at,
                review_iteration=assignment.review_iteration,
                config=config,
                now=now,
                provider_name=assignment.provider_name,
                review_of_assignment_id=assignment.review_of_assignment_id,
            )
            if reason is None:
                continue
            needs_attention_seen.add(aid)
            event_source.publish(ASSIGNMENT_NEEDS_ATTENTION, {
                "assignment_id": aid,
                "repo_name": assignment.repo_name,
                "issue_number": assignment.issue_number,
                "issue_title": assignment.issue_title,
                "machine_name": assignment.machine_name,
                "reason": reason,
                "detail": detail,
            })

    machines_by_name = {m.name: m for m in config.machines}
    needed_machines = {a.machine_name for a in running.values()}

    loop = asyncio.get_running_loop()
    agent_data: dict[str, dict] = {}
    for mname in needed_machines:
        machine = machines_by_name.get(mname)
        if machine:
            data = await loop.run_in_executor(
                None, _fetch_agent_status, machine.host
            )
            if data:
                agent_data[mname] = data

    possibly_stuck: list[dict] = []

    for aid, assignment in running.items():
        mname = assignment.machine_name
        data = agent_data.get(mname)
        if data is None:
            # Agent unreachable — don't flag as stuck yet.
            orphaned_since.pop(aid, None)
            continue

        active_ids = {e.get("id") for e in data.get("active", []) if e.get("id")}
        completed_by_id = {
            e.get("id"): e
            for e in data.get("completed", [])
            if e.get("id")
        }

        if aid in active_ids:
            # Still running — clear any orphaned flag.
            orphaned_since.pop(aid, None)
        elif aid in completed_by_id:
            # Terminal transition detected.
            seen_terminal.add(aid)
            orphaned_since.pop(aid, None)
            entry = completed_by_id[aid]
            stats: dict = {}
            for k in ("num_turns", "total_cost_usd", "exit_code", "last_tool", "stop_reason"):
                v = entry.get(k)
                if v is not None:
                    stats[k] = v
            payload = {
                "assignment_id": aid,
                "repo_name": assignment.repo_name,
                "issue_number": assignment.issue_number,
                "issue_title": assignment.issue_title,
                "machine_name": mname,
                "stats": stats,
                "status": entry.get("status"),  # attached so client can inspect
            }
            status = entry.get("status")
            # Bug 1 fix: three-way branch — cancelled must not fire FAILED.
            # #448: advisory routes to a distinct event so the dashboard does
            # not paint a 0-commit clean exit as a failure.
            if status == "done":
                event_source.publish(ASSIGNMENT_COMPLETED, payload)
            elif status == "cancelled":
                event_source.publish(ASSIGNMENT_CANCELLED, payload)
            elif status == "advisory":
                payload["zero_commit_reason"] = entry.get("zero_commit_reason")
                event_source.publish(ASSIGNMENT_ADVISORY, payload)
            else:  # "failed" and any other unexpected terminal status
                payload["exit_code"] = entry.get("exit_code")
                event_source.publish(ASSIGNMENT_FAILED, payload)
        else:
            # Not in active OR completed on the agent.
            dispatched_ago = now - (assignment.dispatched_at or 0)
            if dispatched_ago > stuck_threshold:
                if aid not in orphaned_since:
                    orphaned_since[aid] = now
                possibly_stuck.append({
                    "assignment_id": aid,
                    "repo_name": assignment.repo_name,
                    "issue_number": assignment.issue_number,
                    "machine_name": mname,
                    "dispatched_ago_seconds": int(dispatched_ago),
                })

    # Prune orphaned_since entries that are no longer in the running set.
    for aid in list(orphaned_since):
        if aid not in running:
            del orphaned_since[aid]

    if needs_attention_seen is not None:
        for aid in list(needs_attention_seen):
            if aid not in running:
                needs_attention_seen.discard(aid)

    return possibly_stuck


def openapi_spec() -> dict:
    """#757: the dashboard's OpenAPI 3 document.

    ``GET /api/board`` and ``GET /api/pipeline`` are fully specified via
    :func:`coord.openapi.dataclass_schema` over ``coord.models.Assignment`` /
    ``coord.pipeline.PipelineView``. #1550's TS codegen (``scripts/codegen.py``)
    reads its ``components/schemas`` straight from *this* spec — not from the
    dataclasses directly — so the generated TS types describe exactly what the
    server declares it serves, and the existing ``declared_routes(app.routes)
    == spec_routes(spec)`` test (``tests/test_openapi.py``) transitively
    guarantees they can't drift from the real route table either. The
    action-style ``POST /api/pipeline/action`` endpoint documents its
    ``action`` enum but leaves the response loosely typed since each action
    returns a distinct ad-hoc shape.

    Public (no leading underscore) because ``scripts/codegen.py`` imports it
    as its source of truth; ``coord/agent_app.py`` and ``coord/serve_app.py``
    keep their own ``_openapi_spec()`` private since nothing outside those
    modules consumes them (yet).
    """
    components: dict = {}
    assignment_ref = dataclass_schema(Assignment, components)
    pipeline_view_ref = dataclass_schema(PipelineView, components)
    board_response = {
        "type": "object",
        "properties": {
            "round_number": {"type": "integer"},
            "active": {"type": "array", "items": assignment_ref},
            "completed": {"type": "array", "items": assignment_ref},
        },
    }
    ok_response = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    session_response = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "assignment_id (== the /ws/terminal/{session_id} path param)"},
            "session_name": {"type": "string", "description": "the tmux session name, coord-<session_id>"},
            "machine": {"type": ["string", "null"], "description": "machine name from coordinator.yml"},
            "host": {"type": ["string", "null"], "description": "the machine's Tailscale host"},
            "repo": {"type": ["string", "null"]},
            "issue": {"type": ["integer", "null"]},
            "issue_title": {"type": ["string", "null"]},
            "stage": {"type": ["string", "null"], "description": "assignment type — work/review/smoke/fix/plan/merge/..."},
            "status": {"type": ["string", "null"], "description": "assignment status — running/done/failed/advisory/..."},
            "attached": {"type": "boolean", "description": "is a client currently attached to the tmux session"},
            "pane_dead": {"type": "boolean", "description": "claude has exited but the tmux session is still up"},
        },
    }
    paths = {
        "/": {
            "get": {
                "summary": "Dashboard SPA (or legacy single-file UI) index page",
                "responses": {"200": {"description": "text/html"}},
            }
        },
        "/api/board": {
            "get": {
                "summary": "Recent board state: active assignments + last 20 completed",
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": board_response}},
                    }
                },
            }
        },
        "/api/machines": {
            "get": {
                "summary": "Machine reachability + live agent /status per machine",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/api/proposals": {
            "get": {
                "summary": "Pending brain proposals awaiting approve/reject",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/api/approve": {
            "post": {
                "summary": "Dispatch one or more proposals by id",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "ids": {"type": "array", "items": {"type": "integer"}},
                                    "briefings": {"type": "object"},
                                },
                                "required": ["ids"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "ids must be a non-empty list"},
                    "404": {"description": "No matching proposals"},
                },
            }
        },
        "/api/reject": {
            "post": {
                "summary": "Discard one or more proposals by id",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "ids": {"type": "array", "items": {"type": "integer"}},
                                },
                                "required": ["ids"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "ids must be a non-empty list"},
                },
            }
        },
        "/api/diff/{id}": {
            "get": {
                "summary": "PR/branch diff for an assignment (gh pr diff, falls back to compare)",
                "parameters": [_dashboard_path_param("id", "assignment id")],
                "responses": {
                    "200": {"description": "OK"},
                    "404": {"description": "Assignment/branch/repo not found"},
                    "500": {"description": "gh lookup failed"},
                },
            }
        },
        "/api/chat": {
            "post": {
                "summary": "Stream a chat reply about current board state (SSE)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                                "required": ["message"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "text/event-stream"},
                    "400": {"description": "message required / unsupported provider"},
                },
            }
        },
        "/api/sessions": {
            "get": {
                "summary": (
                    "Live coord-* interactive tmux sessions the phone can attach "
                    "to via GET /ws/terminal/{session_id} (#1066)"
                ),
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": session_response}
                            }
                        },
                    }
                },
            }
        },
        "/api/pipeline": {
            "get": {
                "summary": "PipelineView for every type='work' assignment",
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": pipeline_view_ref}
                            }
                        },
                    }
                },
            }
        },
        "/api/pipeline/action": {
            "post": {
                "summary": "Advance an assignment through a pipeline gate",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "action": {
                                        "type": "string",
                                        "enum": [
                                            "dispatch_review", "dispatch_smoke", "enqueue",
                                            "merge", "post_findings", "unstick",
                                            "test-verdict", "record-review-verdict",
                                            "retry", "dispatch_fix",
                                        ],
                                    },
                                },
                                "required": ["assignment_id", "action"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing/unknown field"},
                    "404": {"description": "Assignment not found"},
                    "501": {"description": "Action not yet implemented"},
                },
            }
        },
        "/events": {
            "get": {
                "summary": "Server-sent-event stream of board/assignment events",
                "responses": {"200": {"description": "text/event-stream"}},
            }
        },
    }
    return build_spec(
        title="coord dashboard",
        version=__version__,
        description="Phone-accessible coordination dashboard (React webapp + legacy single-file UI).",
        paths=paths,
        components=components,
    )


def _dashboard_path_param(name: str, description: str = "") -> dict:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": description,
    }


def build_app(
    config: Config,
    *,
    token: str | None = None,
    session_attacher: SessionAttacher | None = None,
    fixture: FixtureServer | None = None,
    dist_path: Path | None = None,
) -> Starlette:
    """Build the dashboard Starlette app bound to a Config.

    ``token``: the ``/ws/terminal/{session_id}`` bridge's bearer token
    (see :func:`coord.dashboard.terminal.resolve_web_token`). ``None`` means
    the endpoint runs open (dev default) -- fine on a tailnet-only box, but
    the production dashboard should set one, same convention as `coord
    serve`'s ``resolve_serve_token``.

    ``session_attacher``: injectable seam for the ssh/tmux PTY spawn behind
    the terminal bridge (#1065 acceptance) -- defaults to the real
    :class:`~coord.dashboard.terminal.TmuxSessionAttacher`; tests pass a fake.

    ``fixture`` (#1538): a :class:`~coord.dashboard.fixture.FixtureServer`
    puts the app in **seeded-board mode** — every read is answered from the
    fixture instead of ``~/.coord/coord.db`` / the fleet, and every write is
    recorded rather than executed.  The routes, handlers and serialization are
    unchanged; only the data source is swapped, so an acceptance suite built
    on this is still testing the real contract.  ``None`` (the default) is the
    ordinary live dashboard, byte-for-byte as before.

    ``dist_path`` (#1543): override where the built React webapp is read
    from — ``coord web --dist PATH`` / ``$COORD_WEB_DIST``. Lets the bundle
    be served from outside the installed package (e.g. a checkout a build
    hook keeps in sync with merged ``main``) so a webapp change goes live
    without upgrading ``~/.coord-venv`` — see docs/PHONE_WEBAPP.md. ``None``
    (the default) keeps the historical behaviour of serving
    ``coord/dashboard/webapp/dist`` from inside the installed package.
    """
    attacher: SessionAttacher = session_attacher or TmuxSessionAttacher()
    _fixture = fixture
    # Resolved once per app build. WEBAPP_DIST is read as a module global
    # (not captured as a default arg) so tests that
    # `patch("coord.dashboard.server.WEBAPP_DIST", ...)` keep working when
    # dist_path is left unset (the CLI default).
    webapp_dist = Path(dist_path) if dist_path is not None else WEBAPP_DIST

    def _read_board():
        """The board for this request — seeded fixture or the live DB/daemon.

        Fixture mode rebuilds the Board from the raw payload on every call, so
        a handler that mutates what it is handed (``unstick`` →
        ``mark_failed_by_id``) can't leak that into the next request.
        """
        if _fixture is not None:
            return _fixture.board()
        return read_board()

    def _write_board(board) -> None:  # noqa: ANN001
        """Persist *board* — a no-op in fixture mode (writes never execute)."""
        if _fixture is not None:
            return
        write_board(board)

    # ── Real-time event bus ────────────────────────────────────────────────
    event_source = EventSource()

    # Assignments whose terminal transition has already been published via SSE
    # so that repeated polls don't re-fire the same toast.
    _seen_terminal: set[str] = set()
    # assignment_id → timestamp when we first noticed it orphaned.
    _orphaned_since: dict[str, float] = {}
    # #846: assignment_ids that already fired an ASSIGNMENT_NEEDS_ATTENTION
    # toast, so the live poller doesn't re-publish every _POLL_INTERVAL.
    _needs_attention_seen: set[str] = set()

    # #1217 fix iteration 1: api_sessions' fleet tmux sweep gets its OWN bounded
    # executor rather than sharing the asyncio loop's default executor (which
    # `loop.run_in_executor(None, ...)` submits to). The Home.tsx phone client
    # polls /api/sessions every 4s starting the instant the page loads; each
    # poll fans out one blocking subprocess call per configured machine
    # (bounded at 5s inside `list_coord_tmux_sessions`, batch-mode SSH
    # ConnectTimeout=4). A machine that's down takes the full ~4-5s on EVERY
    # poll, and 4s < 5s means a new sweep task for that machine can be
    # submitted before the previous one finishes — so tasks for a chronically
    # unreachable machine back up faster than they drain. On the shared
    # default executor that backlog eventually starves every other consumer
    # of `run_in_executor(None, ...)` in the process (the background agent
    # poller, the terminal WS PTY-read loop, ...), which is exactly the
    # dashboard-wide hang the operator hit. A dedicated executor contains the
    # backlog to this endpoint; the offline-cooldown cache below stops the
    # backlog from growing in the first place.
    _sessions_executor = ThreadPoolExecutor(
        max_workers=max(4, len(config.machines) * 4),
        thread_name_prefix="coord-sessions-sweep",
    )
    # machine name -> monotonic timestamp of the last sweep that looked like a
    # timeout/unreachable host (took close to the 5s subprocess cap). While a
    # machine is within cooldown, skip spawning a new sweep thread for it
    # entirely and report "no sessions" immediately, instead of re-paying the
    # full SSH ConnectTimeout on every ~4s dashboard poll.
    _sessions_offline_since: dict[str, float] = {}

    async def _background_poller() -> None:
        """Runs forever; polls agents every _POLL_INTERVAL seconds."""
        await asyncio.sleep(10)  # Short initial delay so the server is ready
        while True:
            try:
                possibly_stuck = await _poll_once(
                    config, event_source, _seen_terminal, _orphaned_since,
                    needs_attention_seen=_needs_attention_seen,
                )
                event_source.publish(BOARD_UPDATED, {
                    "possibly_stuck": possibly_stuck,
                    "timestamp": time.time(),
                })
            except Exception:
                pass
            await asyncio.sleep(_POLL_INTERVAL)

    async def _play_event_script() -> None:
        """Publish the fixture's scripted SSE sequence once (#1538).

        Each entry's ``after`` is a delay relative to the previous one, so the
        fixture reads as a timeline.  This is what makes live-update behaviour
        testable: the acceptance suite subscribes to ``/events`` and then hits
        ``POST /api/fixture/events/replay`` to run the script deterministically
        instead of racing the server's startup — which is why the script does
        NOT play at startup unless the fixture opts in with
        ``autoplay_events``.
        """
        for scripted in _fixture.events:
            if scripted.after:
                await asyncio.sleep(scripted.after)
            event_source.publish(scripted.type, scripted.data)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app):  # noqa: ANN001
        # Fixture mode never polls the fleet (no network, no money); the
        # scripted event sequence takes the background poller's place, and
        # only auto-plays when the fixture explicitly asks for it.
        if _fixture is None:
            asyncio.create_task(_background_poller())
        elif _fixture.events and _fixture.autoplay_events:
            asyncio.create_task(_play_event_script())
        yield
        _sessions_executor.shutdown(wait=False, cancel_futures=True)

    async def index(request: Request) -> HTMLResponse:
        # Serve the built React webapp when available; fall back to the legacy
        # single-file dashboard so existing behaviour is entirely unchanged.
        spa_index = webapp_dist / "index.html"
        html = spa_index.read_text() if spa_index.exists() else (DASHBOARD_DIR / "index.html").read_text()
        return HTMLResponse(html)

    async def api_board(request: Request) -> JSONResponse:
        board = _read_board()
        from dataclasses import asdict
        return JSONResponse({
            "round_number": board.round_number,
            "active": [asdict(a) for a in board.active],
            "completed": [asdict(a) for a in board.completed[-20:]],
        })

    async def api_machines(request: Request) -> JSONResponse:
        if _fixture is not None:
            # Seeded reachability — never probe the fleet in fixture mode.
            return JSONResponse(_fixture.machines())
        statuses = check_all(config.machines, timeout=3.0)
        result = []
        for s in statuses:
            machine_data = {
                "name": s.machine.name,
                "host": s.machine.host,
                "repos": s.machine.repos,
                "state": s.state,
                "reason": s.reason,
                "latency_ms": s.latency_ms,
            }
            if s.is_online:
                status_result = fetch_status(s.machine, timeout=3.0)
                if status_result.ok:
                    machine_data["assignments"] = status_result.data
                else:
                    machine_data["assignments"] = None
                    machine_data["status_error"] = status_result.error
            result.append(machine_data)
        return JSONResponse(result)

    async def api_sessions(request: Request) -> JSONResponse:
        """GET /api/sessions — live coord-* interactive sessions the phone can
        attach to via GET /ws/terminal/{session_id} (#1066).

        Sources the roster from the same fleet session substrate `coord
        sessions` itself reads — :func:`coord.interactive.list_coord_tmux_sessions`
        (milestone #32 / substrate #28) — rather than inventing a parallel tmux
        discovery path, then enriches each session against the board, the same
        source :func:`~coord.dashboard.terminal.resolve_session_target` (#1065)
        uses to route the actual WS attach, so the two paths can't drift.

        Fleet-wide (#1217): sweeps *every* configured machine, not just the
        local host `coord web` happens to run on — reusing the exact pattern
        `coord sessions --remote` already proves
        (``list_coord_tmux_sessions(host=TmuxHost(ssh_target=machine.host,
        batch=True))`` per machine; ``batch=True`` so a down/unreachable host
        fails fast instead of hanging on an ssh passphrase prompt). The
        dashboard host itself is probed with ``TmuxHost(None)`` (a plain local
        tmux call, no ssh round-trip to itself). Per-host sweeps run
        concurrently via ``asyncio.gather`` over ``run_in_executor`` calls, so
        wall-clock is bounded by the slowest single host's timeout, not the
        sum across the fleet — important since each host sweep already has
        its own 5s cap inside ``list_coord_tmux_sessions`` and this endpoint
        is polled every ~4s from the phone. On a session-name collision across
        hosts (shouldn't happen in practice — session names embed the
        assignment id) the local host wins, mirroring `coord sessions
        --remote`'s "local always wins" rule.

        Each session is tagged with the machine it was actually discovered
        on: the board assignment's `machine_name` when the session matches
        one (the common case), falling back to the sweep's source machine for
        orphaned/unmatched sessions (`coord terminal new` panes, stale
        sessions with no board row) — previously these reported `machine:
        null` even though the sweep knew exactly which host they came from.

        tmux discovery shells out (bounded by a 5s timeout inside
        ``list_coord_tmux_sessions``) so each host's sweep runs off the event
        loop thread via ``run_in_executor`` — but on a **dedicated** executor
        (``_sessions_executor``, sized to the fleet) rather than the shared
        default one, and a machine that recently looked unreachable is
        skipped for a cooldown window instead of being re-probed every ~4s
        (see the comment above ``_sessions_executor``'s definition for why:
        #1217 iteration 1 fixed a dashboard-wide hang caused by exactly this
        fan-out saturating the process's shared default executor).
        """
        if _fixture is not None:
            # Seeded roster — no tmux, no ssh fan-out in fixture mode.
            return JSONResponse(_fixture.sessions())

        from coord.interactive import (
            TMUX_SESSION_PREFIX,
            TmuxHost,
            _get_local_short_hostname,
            list_coord_tmux_sessions,
        )

        loop = asyncio.get_running_loop()
        local_hn = _get_local_short_hostname()

        def _is_local_machine(machine) -> bool:
            return (
                machine.name.lower() == local_hn
                or machine.host.split(".")[0].lower() == local_hn
            )

        def _sweep_one(machine):
            is_local = _is_local_machine(machine)
            host = (
                TmuxHost(None)
                if is_local
                else TmuxHost(ssh_target=machine.host, batch=True)
            )
            start = time.monotonic()
            try:
                found = list_coord_tmux_sessions(host=host)
            except Exception:  # noqa: BLE001 — a down/unreachable machine just contributes nothing
                found = []
            elapsed = time.monotonic() - start
            # Only track cooldown for remote machines — the local sweep never
            # goes over SSH and a slow local tmux call shouldn't suppress it.
            if not is_local:
                if elapsed >= _SESSIONS_SLOW_THRESHOLD:
                    _sessions_offline_since[machine.name] = time.monotonic()
                else:
                    _sessions_offline_since.pop(machine.name, None)
            return machine, found, is_local

        async def _cached_empty(machine, is_local):
            return machine, [], is_local

        tasks = []
        for m in config.machines:
            is_local = _is_local_machine(m)
            since = _sessions_offline_since.get(m.name)
            if (
                not is_local
                and since is not None
                and (time.monotonic() - since) < _SESSIONS_COOLDOWN
            ):
                tasks.append(_cached_empty(m, is_local))
            else:
                tasks.append(loop.run_in_executor(_sessions_executor, _sweep_one, m))

        sweeps = await asyncio.gather(*tasks)
        # Local host(s) first so they win any session-name collision, matching
        # `coord sessions --remote`'s "local always wins" rule. Stable sort
        # preserves config.machines order within each group.
        sweeps = sorted(sweeps, key=lambda t: not t[2])

        board = _read_board()
        assignments_by_id = {
            a.assignment_id: a
            for a in (*board.active, *board.completed)
            if a.assignment_id
        }
        machines_by_name = {m.name: m for m in config.machines}

        sessions = []
        seen_names: set[str] = set()
        for source_machine, raw_sessions, _is_local in sweeps:
            for s in raw_sessions:
                session_name = s.get("session_name", "")
                if session_name in seen_names:
                    continue
                seen_names.add(session_name)
                session_id = session_name[len(TMUX_SESSION_PREFIX):]
                assignment = assignments_by_id.get(session_id)
                machine_name = (
                    assignment.machine_name if assignment else source_machine.name
                )
                machine_cfg = machines_by_name.get(machine_name)
                sessions.append({
                    "session_id": session_id,
                    "session_name": session_name,
                    "machine": machine_name,
                    "host": machine_cfg.host if machine_cfg else source_machine.host,
                    "repo": assignment.repo_name if assignment else None,
                    "issue": assignment.issue_number if assignment else None,
                    "issue_title": assignment.issue_title if assignment else None,
                    "stage": assignment.type if assignment else None,
                    "status": assignment.status if assignment else None,
                    "attached": bool(s.get("attached", False)),
                    "pane_dead": s.get("pane_dead") == "1",
                })
        return JSONResponse(sessions)

    async def api_proposals(request: Request) -> JSONResponse:
        proposals = _fixture.proposals() if _fixture is not None else load_proposals()
        from dataclasses import asdict
        return JSONResponse([asdict(p) for p in proposals])

    async def api_approve(request: Request) -> JSONResponse:
        from coord.dispatch import dispatch, post_briefing, compute_do_not_touch
        from coord.state import (
            clear_proposals, load_dispatched, load_proposals as load_p,
            record_dispatched,
        )

        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        ids = body.get("ids", [])
        if not ids or not isinstance(ids, list):
            return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)

        briefing_overrides = body.get("briefings", {})

        if _fixture is not None:
            # Recorded, not executed — the shape below matches the live path's
            # per-proposal results array so the client can't tell the
            # difference, but nothing is dispatched and no money is spent.
            selected = [p for p in _fixture.proposals() if p.id in ids]
            if not selected:
                return JSONResponse({"error": "no matching proposals"}, status_code=404)
            _fixture.record("/api/approve", body, action="approve")
            return JSONResponse({
                "results": [
                    {"id": p.id, "assignment_id": f"fixture-{p.id}", "ok": True}
                    for p in selected
                ]
            })

        proposals = load_p()
        selected = [p for p in proposals if p.id in ids]
        if not selected:
            return JSONResponse({"error": "no matching proposals"}, status_code=404)

        for p in selected:
            override = briefing_overrides.get(str(p.id))
            if override is not None:
                p.briefing = override

        from coord.claim import claim_message, find_work_claim

        in_flight = load_dispatched()
        board_for_claim = _read_board()
        results = []
        for p in selected:
            repo = config.repo(p.repo_name)
            if repo is not None:
                claim = find_work_claim(
                    p.issue_number, p.repo_name, repo.github, board_for_claim
                )
                if claim is not None:
                    results.append({
                        "id": p.id, "ok": False,
                        "error": claim_message(claim),
                        "claimed": True,
                    })
                    continue
            try:
                response = dispatch(p, config)
                assignment_id = response.get("id", "pending")
                if repo:
                    record_dispatched(
                        assignment_id=assignment_id,
                        proposal=p,
                        repo_github=repo.github,
                        provider_name=response.get("_provider_name"),
                    )
                do_not_touch = compute_do_not_touch(p, peers=selected, in_flight=in_flight)
                try:
                    post_briefing(p, config, assignment_id=assignment_id, do_not_touch=do_not_touch)
                except Exception:
                    pass
                results.append({"id": p.id, "assignment_id": assignment_id, "ok": True})
            except Exception as e:
                results.append({"id": p.id, "ok": False, "error": str(e)})

        clear_proposals()
        board = _read_board()
        board.round_number += 1
        _write_board(board)
        return JSONResponse({"results": results})

    async def api_chat(request: Request) -> StreamingResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        message = body.get("message", "").strip()
        if not message:
            return JSONResponse({"error": "message required"}, status_code=400)

        if _fixture is not None:
            # Recorded, not executed — never spawn a provider subprocess in
            # fixture mode.  Same SSE envelope the live path streams.
            _fixture.record("/api/chat", body, action="chat")
            reply = _fixture.chat_reply

            async def canned():
                yield f"data: {json.dumps({'text': reply})}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(canned(), media_type="text/event-stream")

        board = _read_board()
        from dataclasses import asdict
        board_context = json.dumps({
            "round_number": board.round_number,
            "active": [asdict(a) for a in board.active],
            "completed": [asdict(a) for a in board.completed[-10:]],
        }, indent=2)

        system = (
            "You are the coordinator assistant for a multi-machine Claude Code system. "
            "Answer questions about the current board state, assignments, and machines. "
            "Be concise.\n\n"
            f"Current board state:\n{board_context}"
        )

        # Resolve the coordinator's default provider so the dashboard chat
        # honours the configured backend rather than hard-coding "claude".
        # Uses resolve_default_provider (shared with brain.py) which also
        # enforces the human_attended_only guard — raises ValueError if the
        # configured default is a human-attended-only backend such as
        # ClaudePtyProvider, preventing unattended use of those providers.
        from coord.providers import resolve_default_provider  # noqa: PLC0415

        try:
            _provider = resolve_default_provider(config.providers, config.models)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        # output_format=None → no --output-format flag; dashboard streams
        # plain-text lines, not a JSON envelope.
        _chat_cmd = _provider.oneshot_command(system_prompt=system, output_format=None)

        async def stream():
            proc = await asyncio.create_subprocess_exec(
                *_chat_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            proc.stdin.write(message.encode())
            proc.stdin.close()

            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace")
                yield f"data: {json.dumps({'text': text})}\n\n"

            await proc.wait()
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def api_reject(request: Request) -> JSONResponse:
        from coord.state import load_proposals as load_p, save_proposals as save_p

        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        ids = body.get("ids", [])
        if not ids or not isinstance(ids, list):
            return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)

        if _fixture is not None:
            seeded = _fixture.proposals()
            remaining = [p for p in seeded if p.id not in ids]
            _fixture.record("/api/reject", body, action="reject")
            return JSONResponse(
                {"removed": len(seeded) - len(remaining), "remaining": len(remaining)}
            )

        proposals = load_p()
        remaining = [p for p in proposals if p.id not in ids]
        removed = len(proposals) - len(remaining)
        if remaining:
            save_p(remaining)
        else:
            from coord.state import clear_proposals
            clear_proposals()
        return JSONResponse({"removed": removed, "remaining": len(remaining)})

    async def api_diff(request: Request) -> JSONResponse:
        assignment_id = request.path_params["id"]
        board = _read_board()
        assignment = board.find_by_id(assignment_id)
        if assignment is None:
            return JSONResponse({"error": "assignment not found"}, status_code=404)
        if not assignment.branch:
            return JSONResponse({"error": "no branch recorded"}, status_code=404)

        if _fixture is not None:
            # Seeded diff text — never shell out to `gh` in fixture mode.
            return JSONResponse(
                {"diff": _fixture.diff(assignment_id), "source": "fixture"}
            )

        repo = config.repo(assignment.repo_name)
        if repo is None:
            return JSONResponse({"error": "unknown repo"}, status_code=404)

        try:
            from coord.github_ops import _gh
            raw = _gh(
                "pr", "diff", "--repo", repo.github,
                assignment.branch,
            )
            return JSONResponse({"diff": raw, "source": "pr"})
        except RuntimeError:
            pass

        try:
            from coord.github_ops import _gh
            raw = _gh(
                "api", f"repos/{repo.github}/compare/{repo.default_branch}...{assignment.branch}",
                "--jq", ".files[].patch // empty",
            )
            return JSONResponse({"diff": raw, "source": "compare"})
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_pipeline(request: Request) -> JSONResponse:
        """GET /api/pipeline — return PipelineView for every type='work' assignment.

        Fixture mode (#1538) swaps the three data sources below — board, merge
        queue, cached review findings — and pins ``now``; the computation and
        serialization underneath are the same ``compute_pipeline`` + ``asdict``
        the live dashboard runs, which is what keeps a fixture-backed
        acceptance suite honest.
        """
        from dataclasses import asdict

        from coord.pipeline import compute_pipeline
        from coord.merge_queue import load_queue
        from coord.state import load_assignment_review_findings

        board = _read_board()
        mq_items = _fixture.merge_queue() if _fixture is not None else load_queue()
        pipeline_now = _fixture.now if _fixture is not None else None

        # Build a lookup of review assignment id per work assignment_id so we can
        # fetch the review findings body with one pass instead of N nested loops.
        all_assignments = list(board.active) + list(board.completed)
        review_by_work: dict[str, str] = {}   # work_aid → review aid
        for a in all_assignments:
            if a.type == "review" and a.review_of_assignment_id and a.assignment_id:
                review_by_work[a.review_of_assignment_id] = a.assignment_id

        pipelines = []
        for a in all_assignments:
            if a.type not in ("work", None, ""):
                continue
            # Exclude assignments with no id (shouldn't normally happen).
            if not a.assignment_id:
                continue
            # Pre-load review findings body (DB call; pure-computation path kept
            # clean by passing it as a parameter rather than inside compute_pipeline).
            findings_body: str | None = None
            rev_aid = review_by_work.get(a.assignment_id)
            if rev_aid:
                found = (
                    _fixture.review_findings(rev_aid)
                    if _fixture is not None
                    else load_assignment_review_findings(rev_aid)
                )
                if found:
                    _, findings_body = found
            pv = compute_pipeline(
                a, board, mq_items, config,
                review_findings_body=findings_body,
                now=pipeline_now,
            )
            pipelines.append(asdict(pv))

        return JSONResponse(pipelines)

    # Actions whose live handler returns a fixed-shape success envelope. The
    # fixture branch below reproduces that envelope exactly (`ok: true` plus
    # whatever fields the client reads) so a seeded acceptance run exercises
    # the same client code as production — while the side effect that would
    # have cost money is only written to the recorded-action log.
    def _fixture_action(action, body, assignment, board) -> JSONResponse:  # noqa: ANN001
        """POST /api/pipeline/action in fixture mode — record, never execute.

        Validation that can be answered from the fixture alone (unknown
        action, verdict enums, missing review row, merge-queue membership) is
        replicated so the error contract holds. Checks that depend on live
        config or the fleet (reviewer-machine availability, merge gates) are
        deliberately not — a fixture asserts the *client* contract, and a
        seeded board has no fleet to be unavailable.
        """
        aid = assignment.assignment_id or ""
        all_assignments = list(board.active) + list(board.completed)
        review_a = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == aid and a.type == "review"
            ),
            None,
        )

        # Reject before recording: an invalid request never happened.
        if action == "retry":
            return JSONResponse(
                {"ok": False, "error": "'retry' is not yet implemented in the dashboard"},
                status_code=501,
            )
        if action == "test-verdict":
            verdict = body.get("verdict")
            if verdict not in ("pass", "fail", "skip"):
                return JSONResponse(
                    {"error": "verdict must be one of ['fail', 'pass', 'skip']"},
                    status_code=400,
                )
        elif action == "record-review-verdict":
            if body.get("verdict") not in ("approve", "request-changes"):
                return JSONResponse(
                    {"error": "verdict must be one of ['approve', 'request-changes']"},
                    status_code=400,
                )
            if not body.get("body"):
                return JSONResponse(
                    {"error": "body is required for record-review-verdict"},
                    status_code=400,
                )
            if review_a is None:
                return JSONResponse(
                    {"error": "no review assignment found for this work assignment"},
                    status_code=404,
                )
        elif action == "post_findings":
            if review_a is None:
                return JSONResponse({"error": "no review assignment found"}, status_code=404)
        elif action == "merge":
            if not any(m.assignment_id == aid for m in _fixture.merge_queue()):
                return JSONResponse({"error": "not in merge queue"}, status_code=404)
        elif action == "dispatch_fix":
            if body.get("parent_type", "work") not in ("work", "review"):
                return JSONResponse(
                    {
                        "error": "parent_type must be 'work' or 'review', got "
                        f"{body.get('parent_type')!r}"
                    },
                    status_code=400,
                )
            if not assignment.branch:
                return JSONResponse(
                    {"ok": False, "error": "work assignment has no branch to fix"},
                    status_code=400,
                )
        elif action not in (
            "dispatch_review", "dispatch_smoke", "enqueue", "unstick",
        ):
            return JSONResponse({"error": f"unknown action: {action!r}"}, status_code=400)

        _fixture.record("/api/pipeline/action", body, action=action)

        if action in ("dispatch_review", "dispatch_smoke"):
            kind = "review" if action == "dispatch_review" else "smoke"
            return JSONResponse({
                "ok": True,
                "machine_name": assignment.machine_name,
                "assignment_id": f"fixture-{kind}-{aid}",
            })
        if action == "dispatch_fix":
            return JSONResponse({
                "ok": True,
                "machine_name": assignment.machine_name,
                "assignment_id": f"fixture-fix-{aid}",
                "branch": assignment.branch,
            })
        if action == "merge":
            return JSONResponse({
                "ok": True,
                "events": [
                    {"kind": "merged", "message": f"fixture: would merge {aid}"}
                ],
            })
        if action == "post_findings":
            return JSONResponse({"ok": True, "detail": "posted"})
        if action == "unstick":
            return JSONResponse({"ok": True, "cancelled_on_agent": False})
        if action == "test-verdict":
            state = {"pass": "passed", "fail": "failed", "skip": "skipped"}[
                body["verdict"]
            ]
            return JSONResponse({"ok": True, "test_state": state})
        # enqueue, record-review-verdict
        return JSONResponse({"ok": True})

    async def api_pipeline_action(request: Request) -> JSONResponse:
        """POST /api/pipeline/action — advance an assignment through a gate.

        Body: {"assignment_id": "...", "action": "..."}

        Supported actions: dispatch_review, dispatch_smoke, enqueue, merge,
        retry (501), dispatch_fix (501).

        In fixture mode (#1538) every one of these is **recorded, not
        executed** — see :func:`_fixture_action`.
        """
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        assignment_id = body.get("assignment_id")
        action = body.get("action")
        if not assignment_id or not action:
            return JSONResponse(
                {"error": "assignment_id and action are required"}, status_code=400
            )

        board = _read_board()
        assignment = board.find_by_id(assignment_id)
        if assignment is None:
            return JSONResponse({"error": "assignment not found"}, status_code=404)

        if _fixture is not None:
            return _fixture_action(action, body, assignment, board)

        if action == "dispatch_review":
            from coord.review import dispatch_review

            try:
                result = dispatch_review(assignment, board, config)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            if result:
                _write_board(board)
                return JSONResponse({
                    "ok": True,
                    "machine_name": result.machine_name,
                    "assignment_id": result.assignment_id,
                })
            # #1627: report the specific guard dispatch_review hit instead of
            # a generic guess — it's recorded on the assignment itself.
            return JSONResponse({
                "ok": False,
                "error": assignment.review_dispatch_reason
                or "could not find a suitable reviewer machine (check reviews config and machine availability)",
            })

        elif action == "dispatch_smoke":
            from coord.smoke import dispatch_smoke

            try:
                result = dispatch_smoke(assignment, board, config)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            if result:
                _write_board(board)
                return JSONResponse({
                    "ok": True,
                    "machine_name": result.machine_name,
                    "assignment_id": result.assignment_id,
                })
            return JSONResponse({
                "ok": False,
                "error": "no smoke test needed or no capable machine matched the diff",
            })

        elif action == "enqueue":
            repo = config.repo(assignment.repo_name)
            if repo is None:
                return JSONResponse({"error": "unknown repo"}, status_code=404)
            from coord import merge_queue as mq

            # #946: this was the third (dashboard-only) enqueue path left
            # ungated after the daemon (`enqueue_approved_work`) and `coord
            # merge`'s auto-enqueue loop were fixed to use the shared
            # `passes_merge_gates` predicate. Gate here too — untested /
            # unreviewed work must never enter the merge queue through any
            # path. `force: true` in the request body is the explicit
            # escape hatch, mirroring `--force-merge` at merge time.
            force = bool(body.get("force"))
            if not force and not mq.passes_merge_gates(assignment, config, board):
                return JSONResponse({
                    "ok": False,
                    "error": (
                        "assignment has not passed the required review/smoke "
                        "gates — pass force: true to enqueue anyway"
                    ),
                })

            try:
                if force:
                    # Bypass the gate entirely — don't pass config/board, or
                    # enqueue()'s own (unconditional) gate check would still
                    # reject it despite the explicit override above.
                    entry = mq.enqueue(assignment, repo.github, repo.default_branch)
                else:
                    entry = mq.enqueue(
                        assignment, repo.github, repo.default_branch,
                        config=config, board=board,
                    )
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            if entry is None:
                return JSONResponse({"ok": False, "error": "could not enqueue (already in queue?)"})
            return JSONResponse({"ok": True})

        elif action == "merge":
            from coord import github_ops as _gh_ops
            from coord.merge_queue import PENDING, load_queue, process, save_queue

            items = load_queue()
            target = next(
                (x for x in items if x.assignment_id == assignment_id), None
            )
            if target is None:
                return JSONResponse({"error": "not in merge queue"}, status_code=404)
            if target.state != PENDING:
                return JSONResponse(
                    {"error": f"queue entry state is {target.state!r}, expected 'pending'"},
                    status_code=400,
                )
            # Process only the single entry (target is in `items` by reference;
            # process() mutates it in place, then we save the full queue).
            events = process([target], _gh_ops)
            save_queue(items)
            return JSONResponse(
                {
                    "ok": True,
                    "events": [
                        {"kind": e.kind, "message": e.message} for e in events
                    ],
                }
            )

        elif action == "post_findings":
            # Find the review assignment linked to this work assignment and
            # attempt to post its findings.
            all_assignments = list(board.active) + list(board.completed)
            review_assignment = next(
                (
                    a for a in all_assignments
                    if a.review_of_assignment_id == assignment_id and a.type == "review"
                ),
                None,
            )
            if review_assignment is None:
                return JSONResponse({"error": "no review assignment found"}, status_code=404)
            if review_assignment.review_posted_at is not None:
                return JSONResponse({"ok": True, "detail": "already posted"})
            from coord.notify import post_orphaned_review_findings  # noqa: PLC0415

            posted = post_orphaned_review_findings(config)
            ok = review_assignment.assignment_id in posted
            return JSONResponse(
                {"ok": ok, "detail": "posted" if ok else "not posted (agent offline or no structured findings)"}
            )

        elif action == "unstick":
            # Cancel on the agent server (best-effort) then mark failed on the
            # board.  Used for assignments that are running in the DB but have
            # silently disappeared from the agent's active list.
            machine = next(
                (m for m in config.machines if m.name == assignment.machine_name),
                None,
            )
            cancelled_on_agent = False
            if machine is not None:
                try:
                    resp = httpx.post(
                        f"http://{machine.host}:{AGENT_PORT}/cancel/{assignment_id}",
                        timeout=10.0,
                    )
                    cancelled_on_agent = resp.status_code in (200, 202)
                except Exception:
                    pass
            # Mark failed in the board regardless of agent response.
            board.mark_failed_by_id(assignment_id, finished_at=time.time())
            _write_board(board)
            return JSONResponse({"ok": True, "cancelled_on_agent": cancelled_on_agent})

        elif action == "test-verdict":
            # Record a human Test-gate verdict for a work assignment.
            # Body: {assignment_id, verdict: "pass"|"fail"|"skip", reason?}
            verdict = body.get("verdict")
            reason = body.get("reason") or None
            _VALID_VERDICTS = {"pass", "fail", "skip"}
            if verdict not in _VALID_VERDICTS:
                return JSONResponse(
                    {"error": f"verdict must be one of {sorted(_VALID_VERDICTS)!r}"},
                    status_code=400,
                )
            # Map short form to the canonical test_state values used by the TUI
            # and reconcile gating logic.
            test_state_map = {"pass": "passed", "fail": "failed", "skip": "skipped"}
            test_state = test_state_map[verdict]
            test_reason = reason if verdict == "fail" else None
            # Mirror to legacy smoke_test column for the smoke-stage scoring in
            # pipeline.py (predates the human Test gate — same mirror as cli.py).
            smoke_test: str | None = verdict if verdict in ("pass", "fail") else None
            smoke_test_reason: str | None = reason if verdict == "fail" else None
            from coord.state import record_test_verdict as _record_test_verdict

            try:
                _record_test_verdict(
                    assignment_id=assignment_id,
                    test_state=test_state,
                    test_reason=test_reason,
                    smoke_test=smoke_test,
                    smoke_test_reason=smoke_test_reason,
                )
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            return JSONResponse({"ok": True, "test_state": test_state})

        elif action == "record-review-verdict":
            # Persist a parsed review verdict + findings body to the DB cache so
            # the phone can record results from a manual review session without
            # going through the full notify/auto_loop path.
            # Body: {assignment_id, verdict: "approve"|"request-changes", body}
            # NOTE: assignment_id here is the WORK assignment id (as exposed by
            # GET /api/pipeline).  We must look up the linked review assignment
            # before writing, since _persist_review_findings writes to the review
            # row and compute_pipeline reads findings back from the review row.
            verdict = body.get("verdict")
            findings_body = body.get("body")
            _VALID_REVIEW_VERDICTS = {"approve", "request-changes"}
            if verdict not in _VALID_REVIEW_VERDICTS:
                return JSONResponse(
                    {"error": f"verdict must be one of {sorted(_VALID_REVIEW_VERDICTS)!r}"},
                    status_code=400,
                )
            if not findings_body:
                return JSONResponse(
                    {"error": "body is required for record-review-verdict"},
                    status_code=400,
                )
            # Look up the review assignment linked to this work assignment.
            all_assignments = list(board.active) + list(board.completed)
            review_a = next(
                (
                    a for a in all_assignments
                    if a.review_of_assignment_id == assignment_id and a.type == "review"
                ),
                None,
            )
            if review_a is None:
                return JSONResponse(
                    {"error": "no review assignment found for this work assignment"},
                    status_code=404,
                )
            from coord.notify import _persist_review_findings  # noqa: PLC0415

            try:
                _persist_review_findings(review_a.assignment_id, verdict, findings_body)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            return JSONResponse({"ok": True})

        elif action == "retry":
            return JSONResponse(
                {"ok": False, "error": "'retry' is not yet implemented in the dashboard"},
                status_code=501,
            )

        elif action == "dispatch_fix":
            parent_type = body.get("parent_type", "work")
            if parent_type not in ("work", "review"):
                return JSONResponse(
                    {"error": f"parent_type must be 'work' or 'review', got {parent_type!r}"},
                    status_code=400,
                )
            if not assignment.branch:
                return JSONResponse(
                    {"ok": False, "error": "work assignment has no branch to fix"},
                    status_code=400,
                )
            from coord.review import dispatch_headless_fix  # noqa: PLC0415

            try:
                result = dispatch_headless_fix(
                    assignment, board, config, parent_type=parent_type
                )
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            if result:
                _write_board(board)
                return JSONResponse({
                    "ok": True,
                    "machine_name": result.machine_name,
                    "assignment_id": result.assignment_id,
                    "branch": result.branch,
                })
            return JSONResponse({
                "ok": False,
                "error": (
                    "could not dispatch fix — no capable machine, branch missing, "
                    "findings unresolvable, or max review iterations reached"
                ),
            })

        else:
            return JSONResponse(
                {"error": f"unknown action: {action!r}"}, status_code=400
            )

    async def terminal_ws(websocket: WebSocket) -> None:
        """Human-attended PTY<->WebSocket bridge for a live tmux session (#1065).

        ToS §3.7 / #437: relays a live human only -- browser keystrokes (binary
        frames) to the PTY's stdin, PTY stdout back as binary frames, plus
        JSON text control messages:

        * ``{"type": "resize", "cols": .., "rows": ..}`` -- propagate a
          terminal resize to the PTY (``TIOCSWINSZ``).
        * ``{"type": "copy-mode", "action": "enter"|"exit"|"page-up"|\
          "page-down"|"top"|"bottom"}`` -- drive tmux copy-mode via
          ``tmux send-keys -X`` / ``tmux copy-mode`` so the phone's Scroll
          button can reach pane history without knowing the user's prefix
          key or mode-keys setting (#1299).

        No autonomous injection or scraping happens here.

        Auth: requires ``?token=`` to match the dashboard's configured bearer
        token (browsers can't set custom headers on a WS upgrade, so it can't
        travel as an ``Authorization`` header like the REST API's). No token
        configured on the server => open, matching `coord serve`'s
        ``resolve_serve_token`` convention. A token is configured but missing
        / wrong on the request => the connection is accepted and then closed
        immediately with 4401 (see the accept-then-close note below); no PTY
        is ever attached, so nothing is relayed to an unauthenticated client.

        Accept-then-close (#1071 live-smoke fix): both rejection paths below
        MUST ``accept()`` the handshake before ``close(code=...)``. Per the
        ASGI/WebSocket spec an application close code can only be delivered
        over an *accepted* connection -- closing pre-accept aborts the HTTP
        upgrade instead, which reaches the browser as a plain ``403`` with no
        code attached. The client (`webapp/src/components/Terminal.tsx`)
        tells "this session is gone for good" (4404, a terminal state) apart
        from "transient drop, reconnect with backoff" purely by the close
        code, so a pre-accept close made every unknown session look like a
        transient drop and the client retried it forever. Accepting first
        costs one extra round trip on an already-failing request and makes
        the close code actually arrive.
        """
        # Consume the ASGI "websocket.connect" event before we can accept()
        # or close() the handshake.
        await websocket.receive()
        await websocket.accept()

        if token and websocket.query_params.get("token") != token:
            await websocket.close(code=4401)
            return

        session_id = websocket.path_params["session_id"]
        board = _read_board()
        target = resolve_session_target(session_id, board, config)
        if target is None:
            await websocket.close(code=4404)
            return
        host, session_name = target

        try:
            attached = await attacher.attach(host, session_name)
        except Exception:
            await websocket.close(code=1011)
            return

        async def _pump_output() -> None:
            try:
                while True:
                    chunk = await attached.read()
                    if not chunk:
                        break
                    await websocket.send_bytes(chunk)
            except Exception:
                pass

        reader_task = asyncio.create_task(_pump_output())
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is not None:
                    attached.write(data)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    continue
                if payload.get("type") == "resize":
                    try:
                        cols = int(payload.get("cols", 0))
                        rows = int(payload.get("rows", 0))
                    except (TypeError, ValueError):
                        continue
                    if cols > 0 and rows > 0:
                        attached.resize(cols, rows)
                elif payload.get("type") == "copy-mode":
                    action = payload.get("action")
                    if isinstance(action, str):
                        await attached.copy_mode(action)
        except WebSocketDisconnect:
            pass
        finally:
            reader_task.cancel()
            # Detach only -- NEVER kill the underlying tmux session (#1065).
            attached.detach()

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/api/board", api_board, methods=["GET"]),
        Route("/api/machines", api_machines, methods=["GET"]),
        Route("/api/sessions", api_sessions, methods=["GET"]),
        Route("/api/proposals", api_proposals, methods=["GET"]),
        Route("/api/approve", api_approve, methods=["POST"]),
        Route("/api/reject", api_reject, methods=["POST"]),
        Route("/api/diff/{id}", api_diff, methods=["GET"]),
        Route("/api/chat", api_chat, methods=["POST"]),
        Route("/api/pipeline", api_pipeline, methods=["GET"]),
        Route("/api/pipeline/action", api_pipeline_action, methods=["POST"]),
        build_events_route(event_source),
        WebSocketRoute("/ws/terminal/{session_id}", terminal_ws),
    ]
    # #757: served OpenAPI 3 spec + Swagger UI docs page.
    routes.extend(openapi_and_docs_routes(openapi_spec()))

    # ── Fixture-mode introspection (#1538) ─────────────────────────────────
    # Registered ONLY under `coord web --fixture`, so the live dashboard's
    # route table and OpenAPI inventory are byte-for-byte unchanged. These are
    # the assertion surface: what would have been dispatched, and a
    # deterministic trigger for the fixture's scripted SSE sequence.
    if _fixture is not None:

        async def api_fixture_actions(request: Request) -> JSONResponse:
            """GET/DELETE /api/fixture/actions — the recorded-write log."""
            if request.method == "DELETE":
                return JSONResponse({"cleared": _fixture.clear_actions()})
            return JSONResponse(
                {"actions": [a.to_dict() for a in _fixture.actions]}
            )

        async def api_fixture_replay(request: Request) -> JSONResponse:
            """POST /api/fixture/events/replay — run the scripted SSE sequence."""
            asyncio.create_task(_play_event_script())
            return JSONResponse({"ok": True, "count": len(_fixture.events)})

        async def api_fixture_publish(request: Request) -> JSONResponse:
            """POST /api/fixture/events — publish one ad-hoc SSE event now."""
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "invalid JSON"}, status_code=400)
            etype = body.get("type")
            if not etype or not isinstance(etype, str):
                return JSONResponse({"error": "type is required"}, status_code=400)
            event = event_source.publish(etype, body.get("data"))
            return JSONResponse({"ok": True, "id": event.id})

        routes.extend([
            Route(
                "/api/fixture/actions",
                api_fixture_actions,
                methods=["GET", "DELETE"],
                include_in_schema=False,
            ),
            Route(
                "/api/fixture/events/replay",
                api_fixture_replay,
                methods=["POST"],
                include_in_schema=False,
            ),
            Route(
                "/api/fixture/events",
                api_fixture_publish,
                methods=["POST"],
                include_in_schema=False,
            ),
        ])

    # ── Static file serving for the built React webapp ─────────────────────
    # Only activated when the resolved dist dir exists (i.e. after
    # `npm run build`, into either coord/dashboard/webapp/dist/ or the
    # --dist/COORD_WEB_DIST override — #1543). When absent the routes list
    # is unchanged and the legacy dashboard serves normally — no
    # test-suite impact.
    if webapp_dist.exists():
        # /assets/ — Vite hashed JS/CSS bundles (immutable; safe to cache).
        _assets = webapp_dist / "assets"
        if _assets.exists():
            routes.append(
                Mount("/assets", StaticFiles(directory=str(_assets)), name="assets")
            )

        async def _spa_catch_all(request: Request) -> FileResponse | HTMLResponse:
            """Serve exact static files from dist/ or SPA index.html fallback.

            Handles three cases:
            - Known static roots (sw.js, manifest.webmanifest, icons/, …)
              → served as the actual file so the browser gets correct MIME types.
            - SPA client-side routes (/issues/42, /pipeline, …)
              → serve index.html; the React router takes over.
            """
            path = request.path_params.get("path", "")
            candidate = webapp_dist / path
            if candidate.is_file():
                return FileResponse(str(candidate))
            # SPA fallback — let the React router handle the path.
            return HTMLResponse((webapp_dist / "index.html").read_text())

        # Not part of the JSON API contract (client-side-router fallback) —
        # excluded from the OpenAPI route inventory via include_in_schema.
        routes.append(
            Route("/{path:path}", _spa_catch_all, methods=["GET"], include_in_schema=False)
        )

    return Starlette(routes=routes, lifespan=_lifespan)
