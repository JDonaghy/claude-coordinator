"""Starlette HTTP layer over `AgentServer`."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from coord import __version__
from coord.agent import RUNNING, PENDING, AgentServer, AssignmentSpec
from coord.events import stream_assignment_log


def _default_exec_restart(argv: list[str]) -> None:
    """Re-exec the current process with the same argv.

    Uses ``sys.executable`` so it works whether *coord* was invoked as a
    console-script entry-point or via ``python -m coord``.
    """
    os.execv(sys.executable, [sys.executable] + argv)


def _detect_install_mode() -> tuple[bool, str | None]:
    """Return ``(is_editable, project_path)``.

    *is_editable* is True when the package is installed in editable mode (i.e.
    ``pip install -e .``).  *project_path* is the on-disk source directory for
    editable installs, or *None* for regular (site-packages) installs.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "claude-coordinator"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Editable project location:"):
                path = line.split(":", 1)[1].strip()
                return True, path
        return False, None
    except Exception:
        return False, None


def build_app(
    server: AgentServer,
    *,
    exec_restart: Callable[[list[str]], None] | None = None,
) -> Starlette:
    """Build the Starlette app bound to a specific AgentServer instance.

    Parameters
    ----------
    server:
        The ``AgentServer`` instance to bind routes to.
    exec_restart:
        Callable invoked to replace the current process when ``/update`` or
        ``/restart`` completes.  Receives ``sys.argv`` as its argument.
        Defaults to :func:`_default_exec_restart` (calls ``os.execv``).
        Tests may inject a no-op or a mock to prevent the test process from
        being replaced.
    """
    if exec_restart is None:
        exec_restart = _default_exec_restart

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(server.health())

    async def status(request: Request) -> JSONResponse:
        data = server.list_assignments()
        data["version"] = __version__
        return JSONResponse(data)

    async def repos(request: Request) -> JSONResponse:
        return JSONResponse(server.list_repos())

    async def assign(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        try:
            spec = AssignmentSpec(**body)
        except TypeError as e:
            return JSONResponse({"error": f"bad assignment payload: {e}"}, status_code=400)
        try:
            assignment = server.assign(spec)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(assignment.to_dict(), status_code=202)

    async def cancel(request: Request) -> JSONResponse:
        assignment_id = request.path_params["id"]
        try:
            assignment = server.cancel(assignment_id)
        except KeyError:
            return JSONResponse({"error": f"unknown assignment {assignment_id}"}, status_code=404)
        return JSONResponse(assignment.to_dict())

    async def logs(request: Request) -> Response:
        assignment_id = request.path_params["id"]
        assignment = server.get(assignment_id)
        if assignment is None or assignment.log_path is None:
            return JSONResponse(
                {"error": f"unknown assignment {assignment_id}"}, status_code=404
            )
        log_path = Path(assignment.log_path)
        if not log_path.exists():
            return JSONResponse(
                {"error": f"no log file for assignment {assignment_id}"}, status_code=404
            )

        since_raw = request.query_params.get("since", "0")
        try:
            since = max(0, int(since_raw))
        except ValueError:
            return JSONResponse(
                {"error": f"invalid since value: {since_raw!r}"}, status_code=400
            )

        with open(log_path, "rb") as f:
            f.seek(since)
            body = f.read()
        total_size = log_path.stat().st_size
        headers = {
            "X-Coord-Log-Total": str(total_size),
            "X-Coord-Log-Status": assignment.status,
        }
        return PlainTextResponse(body.decode("utf-8", errors="replace"), headers=headers)

    async def stream(request: Request) -> Response:
        assignment_id = request.path_params["id"]
        assignment = server.get(assignment_id)
        if assignment is None or assignment.log_path is None:
            return JSONResponse(
                {"error": f"unknown assignment {assignment_id}"}, status_code=404
            )
        log_path = Path(assignment.log_path)

        last_event_id = request.headers.get("last-event-id")
        if last_event_id is not None:
            try:
                start_offset = max(0, int(last_event_id))
            except ValueError:
                start_offset = 0
        else:
            try:
                start_offset = max(0, int(request.query_params.get("since", "0")))
            except ValueError:
                start_offset = 0

        def is_active() -> bool:
            current = server.get(assignment_id)
            return current is not None and current.status in (PENDING, RUNNING)

        gen = stream_assignment_log(
            log_path,
            is_active=is_active,
            request=request,
            start_offset=start_offset,
        )
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    async def update(request: Request) -> JSONResponse:
        """Upgrade the package and restart the agent process.

        Detects whether the install is editable (``pip install -e .``) and
        runs ``git pull --ff-only`` in the project directory in that case.
        For regular installs it runs ``pip install --upgrade claude-coordinator``.
        Either way the process is restarted with ``os.execv`` after the upgrade
        succeeds.  The upgrade and restart run in a daemon-less background
        thread so the HTTP response is returned to the caller before the
        process is replaced.
        """
        is_editable, project_path = _detect_install_mode()
        mode = "editable (git pull)" if is_editable else "pip install --upgrade"

        # Capture argv now — os.execv replaces the process later.
        saved_argv = list(sys.argv)

        def _do_update() -> None:
            try:
                if is_editable and project_path:
                    result = subprocess.run(
                        ["git", "pull", "--ff-only"],
                        cwd=project_path,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if result.returncode != 0:
                        return  # Can't surface this error after the response is sent
                else:
                    result = subprocess.run(
                        [
                            sys.executable, "-m", "pip", "install",
                            "--upgrade", "claude-coordinator",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if result.returncode != 0:
                        return
                # Brief pause so the HTTP response reaches the client first.
                time.sleep(0.5)
                exec_restart(saved_argv)
            except Exception:
                pass

        threading.Thread(target=_do_update, daemon=False, name="agent-update").start()
        return JSONResponse({"status": "updating", "mode": mode}, status_code=202)

    async def restart(request: Request) -> JSONResponse:
        """Gracefully restart the agent process.

        Waits up to ``cancel_timeout`` seconds (default 30) for active workers
        to finish on their own.  Any workers still running after the timeout
        are cancelled before the process is replaced.  Returns HTTP 202
        immediately; the actual restart happens in a background thread.

        Request body (JSON, optional)::

            {"cancel_timeout": 30}
        """
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass

        cancel_timeout = float(body.get("cancel_timeout", 30))
        saved_argv = list(sys.argv)

        with server._lock:
            active_count = sum(
                1
                for a in server._assignments.values()
                if a.status in (PENDING, RUNNING)
            )

        def _do_restart() -> None:
            # Wait for workers to drain.
            deadline = time.time() + cancel_timeout
            while time.time() < deadline:
                with server._lock:
                    still_active = sum(
                        1
                        for a in server._assignments.values()
                        if a.status in (PENDING, RUNNING)
                    )
                if still_active == 0:
                    break
                time.sleep(1)

            # Cancel any workers that are still running.
            with server._lock:
                pending_ids = [
                    aid
                    for aid, a in server._assignments.items()
                    if a.status in (PENDING, RUNNING)
                ]
            for aid in pending_ids:
                try:
                    server.cancel(aid)
                except Exception:
                    pass

            time.sleep(0.5)
            exec_restart(saved_argv)

        threading.Thread(target=_do_restart, daemon=False, name="agent-restart").start()
        return JSONResponse(
            {
                "status": "restarting",
                "active_workers": active_count,
                "cancel_timeout": cancel_timeout,
            },
            status_code=202,
        )

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
        Route("/repos", repos, methods=["GET"]),
        Route("/assign", assign, methods=["POST"]),
        Route("/cancel/{id}", cancel, methods=["POST"]),
        Route("/logs/{id}", logs, methods=["GET"]),
        Route("/stream/{id}", stream, methods=["GET"]),
        Route("/update", update, methods=["POST"]),
        Route("/restart", restart, methods=["POST"]),
    ]
    return Starlette(routes=routes)
