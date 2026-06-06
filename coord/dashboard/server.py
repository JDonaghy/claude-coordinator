"""Web dashboard HTTP server — lightweight UI for phone-accessible coordination."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.events import (
    ASSIGNMENT_COMPLETED,
    ASSIGNMENT_FAILED,
    BOARD_UPDATED,
    EventSource,
    build_events_route,
)
from coord.network import check_all, fetch_status
from coord.state import build_board, load_board, load_proposals, save_board

DASHBOARD_DIR = Path(__file__).parent

# How often (seconds) the background poller queries agent servers.
_POLL_INTERVAL = 30.0
# How long (seconds) an assignment must be running with no agent record before
# it is flagged as possibly stuck.
_STUCK_THRESHOLD = 300.0  # 5 minutes

# Bug 1 fix: distinct event type for cancelled assignments so they are not
# bucketed as FAILED on the client.  Not yet in coord.events — defined here
# until a shared constants refactor can move it.
ASSIGNMENT_CANCELLED = "assignment_cancelled"


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
) -> list[dict]:
    """One iteration of the background agent poller.

    Queries each machine's agent server, publishes ``assignment_completed`` /
    ``assignment_failed`` / ``assignment_cancelled`` SSE events on transitions,
    and returns a list of possibly-stuck assignment info dicts.

    Extracted to module level so unit tests can drive it directly without
    standing up a full HTTP server.
    """
    if board is None:
        board = load_board() or build_board()
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
            if status == "done":
                event_source.publish(ASSIGNMENT_COMPLETED, payload)
            elif status == "cancelled":
                event_source.publish(ASSIGNMENT_CANCELLED, payload)
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

    return possibly_stuck


def build_app(config: Config) -> Starlette:
    """Build the dashboard Starlette app bound to a Config."""

    # ── Real-time event bus ────────────────────────────────────────────────
    event_source = EventSource()

    # Assignments whose terminal transition has already been published via SSE
    # so that repeated polls don't re-fire the same toast.
    _seen_terminal: set[str] = set()
    # assignment_id → timestamp when we first noticed it orphaned.
    _orphaned_since: dict[str, float] = {}

    async def _background_poller() -> None:
        """Runs forever; polls agents every _POLL_INTERVAL seconds."""
        await asyncio.sleep(10)  # Short initial delay so the server is ready
        while True:
            try:
                possibly_stuck = await _poll_once(
                    config, event_source, _seen_terminal, _orphaned_since
                )
                event_source.publish(BOARD_UPDATED, {
                    "possibly_stuck": possibly_stuck,
                    "timestamp": time.time(),
                })
            except Exception:
                pass
            await asyncio.sleep(_POLL_INTERVAL)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app):  # noqa: ANN001
        asyncio.create_task(_background_poller())
        yield

    async def index(request: Request) -> HTMLResponse:
        html = (DASHBOARD_DIR / "index.html").read_text()
        return HTMLResponse(html)

    async def api_board(request: Request) -> JSONResponse:
        board = load_board() or build_board()
        from dataclasses import asdict
        return JSONResponse({
            "round_number": board.round_number,
            "active": [asdict(a) for a in board.active],
            "completed": [asdict(a) for a in board.completed[-20:]],
        })

    async def api_machines(request: Request) -> JSONResponse:
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

    async def api_proposals(request: Request) -> JSONResponse:
        proposals = load_proposals()
        from dataclasses import asdict
        return JSONResponse([asdict(p) for p in proposals])

    async def api_approve(request: Request) -> JSONResponse:
        from coord.dispatch import dispatch, post_briefing, compute_do_not_touch
        from coord.state import (
            clear_proposals, load_dispatched, load_proposals as load_p,
            record_dispatched, save_board as save_b, build_board as build_b,
        )

        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        ids = body.get("ids", [])
        if not ids or not isinstance(ids, list):
            return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)

        briefing_overrides = body.get("briefings", {})

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
        board_for_claim = build_b()
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
        board = build_b()
        board.round_number += 1
        save_b(board)
        return JSONResponse({"results": results})

    async def api_chat(request: Request) -> StreamingResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        message = body.get("message", "").strip()
        if not message:
            return JSONResponse({"error": "message required"}, status_code=400)

        board = load_board() or build_board()
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

        async def stream():
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", "--system-prompt", system,
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
        board = load_board() or build_board()
        assignment = board.find_by_id(assignment_id)
        if assignment is None:
            return JSONResponse({"error": "assignment not found"}, status_code=404)
        if not assignment.branch:
            return JSONResponse({"error": "no branch recorded"}, status_code=404)

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
        """GET /api/pipeline — return PipelineView for every type='work' assignment."""
        from dataclasses import asdict

        from coord.pipeline import compute_pipeline
        from coord.merge_queue import load_queue

        board = load_board() or build_board()
        mq_items = load_queue()

        pipelines = []
        for a in list(board.active) + list(board.completed):
            if a.type not in ("work", None, ""):
                continue
            # Exclude assignments with no id (shouldn't normally happen).
            if not a.assignment_id:
                continue
            pv = compute_pipeline(a, board, mq_items, config)
            pipelines.append(asdict(pv))

        return JSONResponse(pipelines)

    async def api_pipeline_action(request: Request) -> JSONResponse:
        """POST /api/pipeline/action — advance an assignment through a gate.

        Body: {"assignment_id": "...", "action": "..."}

        Supported actions: dispatch_review, dispatch_smoke, enqueue, merge,
        retry (501), dispatch_fix (501).
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

        board = load_board() or build_board()
        assignment = board.find_by_id(assignment_id)
        if assignment is None:
            return JSONResponse({"error": "assignment not found"}, status_code=404)

        if action == "dispatch_review":
            from coord.review import dispatch_review

            try:
                result = dispatch_review(assignment, board, config)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            if result:
                save_board(board)
                return JSONResponse({
                    "ok": True,
                    "machine_name": result.machine_name,
                    "assignment_id": result.assignment_id,
                })
            return JSONResponse({
                "ok": False,
                "error": "could not find a suitable reviewer machine (check reviews config and machine availability)",
            })

        elif action == "dispatch_smoke":
            from coord.smoke import dispatch_smoke

            try:
                result = dispatch_smoke(assignment, board, config)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            if result:
                save_board(board)
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
            from coord.merge_queue import enqueue

            try:
                entry = enqueue(assignment, repo.github, repo.default_branch)
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
            save_board(board)
            return JSONResponse({"ok": True, "cancelled_on_agent": cancelled_on_agent})

        elif action in ("retry", "dispatch_fix"):
            return JSONResponse(
                {"ok": False, "error": f"{action!r} is not yet implemented in the dashboard"},
                status_code=501,
            )

        else:
            return JSONResponse(
                {"error": f"unknown action: {action!r}"}, status_code=400
            )

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/api/board", api_board, methods=["GET"]),
        Route("/api/machines", api_machines, methods=["GET"]),
        Route("/api/proposals", api_proposals, methods=["GET"]),
        Route("/api/approve", api_approve, methods=["POST"]),
        Route("/api/reject", api_reject, methods=["POST"]),
        Route("/api/diff/{id}", api_diff, methods=["GET"]),
        Route("/api/chat", api_chat, methods=["POST"]),
        Route("/api/pipeline", api_pipeline, methods=["GET"]),
        Route("/api/pipeline/action", api_pipeline_action, methods=["POST"]),
        build_events_route(event_source),
    ]
    return Starlette(routes=routes, lifespan=_lifespan)
