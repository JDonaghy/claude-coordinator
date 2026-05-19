"""Web dashboard HTTP server — lightweight UI for phone-accessible coordination."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from coord.config import Config
from coord.network import check_all, fetch_status, AGENT_PORT
from coord.state import build_board, load_board, load_proposals, save_board

DASHBOARD_DIR = Path(__file__).parent


def build_app(config: Config) -> Starlette:
    """Build the dashboard Starlette app bound to a Config."""

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
                assignments = fetch_status(s.machine, timeout=3.0)
                machine_data["assignments"] = assignments
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
                        assignment_id=assignment_id, proposal=p, repo_github=repo.github,
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

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/api/board", api_board, methods=["GET"]),
        Route("/api/machines", api_machines, methods=["GET"]),
        Route("/api/proposals", api_proposals, methods=["GET"]),
        Route("/api/approve", api_approve, methods=["POST"]),
        Route("/api/reject", api_reject, methods=["POST"]),
        Route("/api/diff/{id}", api_diff, methods=["GET"]),
        Route("/api/chat", api_chat, methods=["POST"]),
    ]
    return Starlette(routes=routes)
