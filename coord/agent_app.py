"""Starlette HTTP layer over `AgentServer`."""

from __future__ import annotations

import json
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
from coord.agent import RUNNING, PENDING, AgentAssignment, AgentServer, AssignmentSpec
from coord.events import stream_assignment_log
from coord.openapi import build_spec, dataclass_schema, openapi_and_docs_routes


def _installed_version() -> str | None:
    """Return the currently-installed claude-coordinator version.

    Reads ``__version__`` directly from ``coord/__init__.py`` on disk so
    a bumped version in source is picked up immediately — important for
    editable installs where ``importlib.metadata`` reads from a
    ``.egg-info`` that's only regenerated on ``pip install -e .``.

    Falls back to ``importlib.metadata`` only if the disk read fails.
    """
    try:
        import coord  # noqa: PLC0415
        if coord.__file__:
            from pathlib import Path  # noqa: PLC0415
            text = Path(coord.__file__).read_text()
            for line in text.splitlines():
                if line.startswith("__version__"):
                    # parse: __version__ = "0.4.1"
                    raw = line.split("=", 1)[1].strip()
                    return raw.strip('"').strip("'")
    except Exception:
        pass
    try:
        from importlib.metadata import version as _metaver  # noqa: PLC0415
        return _metaver("claude-coordinator")
    except Exception:
        return None


def _write_last_update(state_dir: Path, payload: dict) -> None:
    """Persist the most recent update attempt summary so /health can
    surface it after the agent restarts."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "last_update.json").write_text(json.dumps(payload, indent=2))
    except Exception:
        pass


def _read_last_update(state_dir: Path) -> dict | None:
    try:
        return json.loads((state_dir / "last_update.json").read_text())
    except Exception:
        return None


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


def _path_param(name: str, description: str = "") -> dict:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": description,
    }


def _openapi_spec() -> dict:
    """#757: the agent's OpenAPI 3 document.

    ``POST /assign`` is fully specified (request = ``AssignmentSpec``,
    response = ``AgentAssignment``, both introspected via
    :func:`coord.openapi.dataclass_schema`); the remaining routes carry a
    summary/description and path-param shapes but a loosely-typed body, since
    they return small ad-hoc dicts rather than a dataclass.
    """
    components: dict = {}
    assign_request = dataclass_schema(AssignmentSpec, components)
    assign_response = dataclass_schema(AgentAssignment, components)
    paths = {
        "/health": {
            "get": {
                "summary": "Agent health + version",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/status": {
            "get": {
                "summary": "List this agent's assignments (active + completed)",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/repos": {
            "get": {
                "summary": "Repos this agent can dispatch work into",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/assign": {
            "post": {
                "summary": "Dispatch a new assignment (spawns `claude -p`)",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": assign_request}},
                },
                "responses": {
                    "202": {
                        "description": "Accepted",
                        "content": {"application/json": {"schema": assign_response}},
                    },
                    "400": {"description": "Bad assignment payload"},
                },
            }
        },
        "/cancel/{id}": {
            "post": {
                "summary": "Cancel a running/pending assignment",
                "parameters": [_path_param("id", "assignment id")],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": assign_response}},
                    },
                    "404": {"description": "Unknown assignment"},
                },
            }
        },
        "/inject/{id}": {
            "post": {
                "summary": "Inject a new user message into a running worker's session",
                "parameters": [_path_param("id", "assignment id")],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            }
                        }
                    },
                },
                "responses": {
                    "202": {"description": "Delivered"},
                    "404": {"description": "Unknown assignment"},
                    "409": {"description": "Worker not running"},
                    "410": {"description": "Worker stdin already closed"},
                },
            }
        },
        "/logs/{id}": {
            "get": {
                "summary": "Read (a tail of) the worker's log file",
                "parameters": [
                    _path_param("id", "assignment id"),
                    {
                        "name": "since",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                        "description": "byte offset to read from",
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "404": {"description": "Unknown assignment or no log file"},
                },
            }
        },
        "/stream/{id}": {
            "get": {
                "summary": "Server-sent-event stream of the worker's log",
                "parameters": [_path_param("id", "assignment id")],
                "responses": {"200": {"description": "text/event-stream"}},
            }
        },
        "/update": {
            "post": {
                "summary": "Upgrade the installed package and restart the agent process",
                "responses": {"202": {"description": "Updating"}},
            }
        },
        "/restart": {
            "post": {
                "summary": "Gracefully restart the agent process",
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"cancel_timeout": {"type": "number"}},
                            }
                        }
                    },
                },
                "responses": {"202": {"description": "Restarting"}},
            }
        },
        "/worktree-clean": {
            "post": {
                "summary": "Remove stale git worktrees managed by this agent",
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"recent_secs": {"type": "number"}},
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/artifact/{repo}/{branch}": {
            "get": {
                "summary": "Manifest of stashed build artifacts for a (repo, branch) pair",
                "parameters": [
                    _path_param("repo", "repo name"),
                    _path_param("branch", "sanitized branch name"),
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "404": {"description": "No artifacts for this repo/branch"},
                },
            }
        },
        "/metrics": {
            "get": {
                "summary": "CPU + memory snapshot for the agent machine",
                "responses": {
                    "200": {"description": "OK"},
                    "503": {"description": "psutil not installed"},
                },
            }
        },
    }
    return build_spec(
        title="coord agent",
        version=__version__,
        description="Per-machine agent server: spawns and tracks `claude -p` workers.",
        paths=paths,
        components=components,
    )


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
        data = server.health()
        data["version"] = __version__
        # Surface the most recent /update attempt so the CLI can show
        # "0.3.0 → 0.4.0" or "no_change (0.3.0)" or "failed: <error>".
        last = _read_last_update(server.state_dir)
        if last is not None:
            data["last_update"] = last
        return JSONResponse(data)

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

    async def inject(request: Request) -> JSONResponse:
        """Inject a new user message into a running worker's session.

        Body (JSON): ``{"text": "..."}``.  Worker picks up the message at
        its next turn boundary.  Returns 404 if the assignment isn't on
        this agent, 409 if it isn't running, 410 if the worker's stdin
        is already closed.
        """
        assignment_id = request.path_params["id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                {"error": "body must be {\"text\": \"<non-empty string>\"}"},
                status_code=400,
            )
        try:
            server.inject_message(assignment_id, text)
        except KeyError:
            return JSONResponse(
                {"error": f"unknown assignment {assignment_id}"}, status_code=404
            )
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except BrokenPipeError as e:
            return JSONResponse({"error": str(e)}, status_code=410)
        return JSONResponse({"status": "delivered"}, status_code=202)

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
        state_dir = server.state_dir

        def _do_update() -> None:
            version_before = _installed_version() or "unknown"
            started_at = time.time()
            payload: dict = {
                "mode": mode,
                "started_at": started_at,
                "version_before": version_before,
                "version_after": version_before,
                "result": "failed",
                "error": None,
                "log_excerpt": "",
            }
            try:
                if is_editable and project_path:
                    result = subprocess.run(
                        ["git", "pull", "--ff-only"],
                        cwd=project_path,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                else:
                    # --no-cache-dir bypasses pip's local wheel cache, which
                    # has caused stale-version resolutions on at least one
                    # machine (PyPI metadata races with `pip install --upgrade`).
                    result = subprocess.run(
                        [
                            sys.executable, "-m", "pip", "install",
                            "--upgrade", "--no-cache-dir",
                            "claude-coordinator",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                payload["finished_at"] = time.time()
                # Persist the full pip/git output to a log file so the
                # user can read it after the agent restarts.
                log_path = state_dir / "last_update.log"
                try:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(
                        f"# mode: {mode}\n"
                        f"# returncode: {result.returncode}\n"
                        f"# argv: {result.args}\n\n"
                        f"--- stdout ---\n{result.stdout}\n"
                        f"--- stderr ---\n{result.stderr}\n"
                    )
                except Exception:  # noqa: BLE001
                    pass
                # Keep a short excerpt inline so it appears in /health.
                tail = (result.stderr or result.stdout or "").splitlines()
                payload["log_excerpt"] = "\n".join(tail[-20:])

                if result.returncode != 0:
                    payload["error"] = (
                        f"upgrade exited {result.returncode}; see "
                        f"~/.coord/last_update.log on this machine"
                    )
                    _write_last_update(state_dir, payload)
                    return

                # Resolve what's installed now so we can report a delta and
                # skip restarting if nothing actually changed.
                version_after = _installed_version() or "unknown"
                payload["version_after"] = version_after
                if version_after == version_before and not is_editable:
                    # Nothing to do — pip reported success but resolved to
                    # the same version. Common cause: PyPI hasn't propagated
                    # the new release yet, or the package isn't on the index
                    # the venv's pip is pointed at.
                    payload["result"] = "no_change"
                    payload["error"] = (
                        f"pip resolved to {version_after} (same as installed). "
                        "PyPI may not have propagated the new release yet, or "
                        "this venv's pip is pointed at a different index."
                    )
                    _write_last_update(state_dir, payload)
                    return

                payload["result"] = "upgraded"
                _write_last_update(state_dir, payload)

                # Brief pause so the HTTP response reaches the client first.
                time.sleep(0.5)
                exec_restart(saved_argv)
            except Exception as e:
                payload["error"] = f"{type(e).__name__}: {e}"
                _write_last_update(state_dir, payload)

        threading.Thread(target=_do_update, daemon=False, name="agent-update").start()
        return JSONResponse({"status": "updating", "mode": mode}, status_code=202)

    async def artifact_manifest(request: Request) -> JSONResponse:
        """Return a JSON manifest of stashed artifacts for a (repo, branch) pair.

        Path parameters:
            repo   — repo name (e.g. ``quadraui``)
            branch — sanitized branch name (slashes already replaced with
                     dashes, e.g. ``issue-305-artifact-pull``)

        Response (200)::

            {
                "files": [{"name": "...", "size": N, "mtime": N}, ...],
                "total_bytes": N,
                "built_by_assignment_id": "abc123" | null
            }

        Returns 404 when no stash exists for the given (repo, branch) pair.
        The 404 body's ``error`` field carries the agent's ground-truth
        reason (#914) — e.g. a live worktree exists but was never stashed,
        vs. genuinely nothing was ever built here — rather than a generic
        message, since only this host can tell the difference.
        """
        repo = request.path_params["repo"]
        branch = request.path_params["branch"]
        manifest = server.artifact_manifest(repo, branch)
        if manifest is None:
            reason = server.artifact_absence_reason(repo, branch)
            return JSONResponse(
                {"error": f"no artifacts for repo={repo!r} branch={branch!r}: {reason}"},
                status_code=404,
            )
        return JSONResponse(manifest)

    async def worktree_clean(request: Request) -> JSONResponse:
        """Remove stale git worktrees managed by this agent.

        Idempotent POST — skips worktrees for running/pending assignments
        and those finished within the last 5 minutes.  Returns a JSON
        summary: ``{"cleaned": N, "kept": M, "bytes_freed": B}``.

        Optional JSON body: ``{"recent_secs": 300}`` to override the
        recency window (default 300 s).
        """
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        recent_secs = float(body.get("recent_secs", 300.0))
        result = server.clean_worktrees(recent_secs=recent_secs)
        return JSONResponse(result)

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

    async def metrics(_request: Request) -> JSONResponse:
        """#207: Return CPU and memory metrics for the agent machine.

        Uses ``psutil`` for sub-millisecond, non-blocking snapshots.
        ``cpu_percent(interval=None)`` returns the CPU utilisation since
        the previous call (or since process start on the very first call),
        which is essentially free — no sleep, no blocking.
        """
        try:
            import psutil  # lazy import — keeps startup fast on old agents
        except ImportError:
            return JSONResponse(
                {"error": "psutil not installed on this agent"},
                status_code=503,
            )
        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        return JSONResponse(
            {
                "cpu_percent": cpu,
                "mem_percent": vm.percent,
                "mem_used_mb": round(vm.used / (1024 * 1024), 1),
                "mem_total_mb": round(vm.total / (1024 * 1024), 1),
                "timestamp": time.time(),
            }
        )

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
        Route("/repos", repos, methods=["GET"]),
        Route("/assign", assign, methods=["POST"]),
        Route("/cancel/{id}", cancel, methods=["POST"]),
        Route("/inject/{id}", inject, methods=["POST"]),
        Route("/logs/{id}", logs, methods=["GET"]),
        Route("/stream/{id}", stream, methods=["GET"]),
        Route("/update", update, methods=["POST"]),
        Route("/restart", restart, methods=["POST"]),
        Route("/worktree-clean", worktree_clean, methods=["POST"]),
        # #305: artifact stash manifest (GET /artifact/<repo>/<branch>)
        Route("/artifact/{repo}/{branch}", artifact_manifest, methods=["GET"]),
        # #207: CPU + memory snapshot for TUI sparklines
        Route("/metrics", metrics, methods=["GET"]),
    ]
    # #757: served OpenAPI 3 spec + Swagger UI docs page.
    routes.extend(openapi_and_docs_routes(_openapi_spec()))
    return Starlette(routes=routes)
