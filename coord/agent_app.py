"""Starlette HTTP layer over `AgentServer`."""

from __future__ import annotations

import asyncio
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

from coord import __version__, agent_update
from coord.agent import RUNNING, PENDING, AgentAssignment, AgentServer, AssignmentSpec
from coord.dist_name import resolve_installed, resolve_installed_name
from coord.dist_name import pkg_spec as _dist_pkg_spec
from coord.events import stream_assignment_log
from coord.openapi import build_spec, dataclass_schema, openapi_and_docs_routes


def _agent_pkg_spec() -> str:
    """What `POST /update` asks pip to install (#1237). An agent *is* the
    server half of the package, so it must reinstall itself WITH the
    `[server]` extra — a bare upgrade would, on a fresh venv, leave the
    agent without starlette/uvicorn and dead on the next restart.

    #2103: resolved tolerantly against whichever of `code-coordinator` /
    `claude-coordinator` is currently installed, rather than the old
    hardcoded `claude-coordinator[server]` — installing the wrong name once
    the fleet is mid-rename either 404s against PyPI or, worse, silently
    reinstalls the stale package. Deliberately NOT caught here: if neither
    name resolves, the caller (`_do_update`'s existing try/except) turns
    that into an explicit `last_update.json` failure naming both names
    tried, instead of guessing.
    """
    return _dist_pkg_spec(extra="server")


#: The sibling systemd *user* units `POST /restart-services` (#2069) is
#: allowed to restart. `coord-agent` is deliberately excluded — that unit
#: restarts itself, via `/update`/`/rollback`/`/restart`, and doing it again
#: here would race those endpoints' own restart threads. Matches
#: `coord.health.checks.spawned_coord.DEFAULT_UNITS` minus `coord-agent` and
#: `coord-notify` (the latter has no deploy lane of its own to be behind on).
RESTARTABLE_SIBLING_UNITS: frozenset[str] = frozenset(
    {"coord-serve", "coord-web", "coord-drive-queue"}
)


def _venv_dir() -> Path:
    """Root of the venv `coord agent update` swaps blue/green (#1241).

    Overridable via `COORD_VENV_DIR` (tests, non-default installs);
    defaults to `~/.coord-venv` — the path `install-agent.sh` creates and
    every `deploy/coord-*.service` unit hardcodes as `ExecStart`'s venv.
    """
    override = os.environ.get("COORD_VENV_DIR")
    return Path(override) if override else Path.home() / ".coord-venv"


def _installed_version() -> str | None:
    """Return the currently-installed coordinator distribution's version.

    #1238: ``coord.__version__`` (imported once, at module-import time — see
    the module-level ``from coord import __version__`` above) and this are
    deliberately different reads. This one re-queries ``importlib.metadata``
    fresh on every call, so it reflects a ``pip install``/``pip install
    --upgrade`` that happened to site-packages *after* this process started,
    without needing a restart — exactly what ``/health`` needs to tell "the
    process hasn't restarted since the last update" apart from "the update
    never happened".

    #2103: tries `code-coordinator` then falls back to `claude-coordinator`
    (see ``coord.dist_name``) rather than hardcoding one name — installing
    under the name this process doesn't query used to make a fully-updated
    agent report ``None`` here, the exact false negative behind the
    fleet's most-recurring `✗ did not come back`.
    """
    try:
        return resolve_installed().version
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


def _running_under_systemd() -> bool:
    """True when this process was started by systemd (in practice, a user
    unit — see ``deploy/coord-agent.service``).

    ``INVOCATION_ID`` is set by systemd for every unit invocation (since
    v232) and is the standard "am I running under systemd" signal — unlike
    checking the parent PID, it survives the process being reparented.
    """
    return bool(os.environ.get("INVOCATION_ID"))


def _restart_via_systemctl(unit: str = "coord-agent") -> bool:
    """Best-effort ``systemctl --user restart <unit>``, run from *inside*
    the unit's own process.

    #404 / #1886: ``os.execv`` self-restart does not take under systemd —
    same PID survives with stale code loaded, and nothing detected it
    (that silent survival is the concrete failure #1886 reports). Asking
    systemd itself to restart the unit is the mechanism that's known to
    work — it's the documented manual workaround, and what
    ``coord.commands.agent_ops._escalate_restart`` already does over SSH
    as a fallback from the CLI side. Doing it from inside the process
    removes the dependency on a human noticing the stall and running it
    by hand.

    Returns True once the ``systemctl`` command has been launched — NOT
    whether the restart actually completed; the caller's process is about
    to exit either way, so there is nothing left here to poll for.
    """
    env = dict(os.environ)
    # Should already be set for a process systemd itself started, but
    # setting it explicitly costs nothing and matches the SSH-driven
    # fallback in agent_ops.py, where it IS load-bearing.
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        subprocess.Popen(
            ["systemctl", "--user", "restart", unit],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return True


def _restart_sibling_unit(unit: str, *, timeout: float = 30.0) -> tuple[bool, str]:
    """``systemctl --user restart <unit>`` for a UNIT OTHER THAN THIS ONE,
    and wait for it to report active (#2069).

    Unlike :func:`_restart_via_systemctl` — used for the agent's OWN restart,
    where the caller is about to exit and there is nothing left here to poll
    for — this process stays alive throughout a sibling's restart, so it can
    and should wait rather than fire-and-forget. "The systemctl command was
    launched" is a statement about the request, not the outcome; #2052 fault
    1 is exactly what trusting that distinction cost on a self-restart, and
    there's no reason to reintroduce it here just because it's a neighbour's
    process instead of this one's.
    """
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", unit],
            env=env, capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "systemctl restart failed").strip()[:300]

    deadline = time.time() + max(timeout, 0.0)
    while True:
        try:
            probe = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                env=env, capture_output=True, text=True, timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        state = probe.stdout.strip()
        if state == "active":
            return True, "active"
        if time.time() >= deadline:
            return False, f"still {state or 'unknown'} {timeout:.0f}s after restart"
        time.sleep(0.5)


def _default_exec_restart(argv: list[str]) -> None:
    """Restart the agent process — via systemd when running under it,
    otherwise by re-exec'ing in place.

    #404 / #1886: a bare ``os.execv`` doesn't take under systemd (same
    PID, stale code), and nothing used to detect it. Under systemd, ask
    systemd to restart the unit instead — the mechanism actually known to
    work — and let this process exit; that also re-runs `ExecStart`
    through `~/.coord-venv` fresh, which is what makes a #1241 blue/green
    swap actually take effect (see below).

    #1241: falls back to ``os.execv`` using the *current* `~/.coord-venv`
    symlink's python, re-resolved right now — NOT ``sys.executable``.
    ``sys.executable`` is the literal interpreter path baked into this
    process's own venv *slot* at the time it started (e.g.
    ``~/.coord-venv.blue/bin/python3``, from that slot's own shebang line)
    and stays pinned to that slot for the process's whole life, even after
    a blue/green swap flips the symlink onto the other slot. Re-exec'ing
    with it would silently keep running the OLD slot forever — the process
    "restarts" but never advances. Resolving through the symlink instead
    picks up whichever slot is live *right now*. Falls back to
    ``sys.executable`` when there's no such venv at all (dev/editable
    installs not using the blue/green layout), preserving the pre-#1241
    behaviour there.
    """
    if _running_under_systemd() and _restart_via_systemctl():
        os._exit(0)
    venv_python = _venv_dir() / "bin" / "python"
    executable = str(venv_python) if venv_python.exists() else sys.executable
    os.execv(executable, [executable] + argv)


def _detect_install_mode() -> tuple[bool, str | None]:
    """Return ``(is_editable, project_path)``.

    *is_editable* is True when the package is installed in editable mode (i.e.
    ``pip install -e .``).  *project_path* is the on-disk source directory for
    editable installs, or *None* for regular (site-packages) installs.

    #2103: ``pip show`` needs an exact distribution name, so this resolves
    which of `code-coordinator` / `claude-coordinator` is actually installed
    (see ``coord.dist_name``) first rather than hardcoding one — asking
    `pip show` for a name nothing is installed under always reports "not
    editable", which would misreport a real editable install once the
    fleet's mid-rename.
    """
    dist_name = resolve_installed_name()
    if dist_name is None:
        return False, None
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", dist_name],
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
                "description": (
                    "#1567: by default, any uncommitted worker changes are "
                    "committed locally but NOT pushed anywhere — the "
                    "worker's remote branch is left unchanged. Pass "
                    "?rescue=1 to push the WIP commit to a disposable "
                    "rescue/<id> ref instead (the worker's own branch is "
                    "still never touched)."
                ),
                "parameters": [
                    _path_param("id", "assignment id"),
                    {
                        "name": "rescue",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "boolean", "default": False},
                        "description": (
                            "Push the WIP commit to rescue/<id> instead of "
                            "leaving it local-only."
                        ),
                    },
                ],
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
        "/deploy-units": {
            "post": {
                "summary": (
                    "Install this host's systemd user units from the units "
                    "packaged in the running release (#1831/#1835). Restarts "
                    "nothing — daemon-reload only."
                ),
                "responses": {
                    "200": {"description": "Units deployed (or nothing to do)"},
                    "500": {"description": "A unit could not be written, or daemon-reload failed"},
                },
            }
        },
        "/restart-services": {
            "post": {
                "summary": (
                    "Restart whichever of coord-serve/coord-web/coord-drive-queue "
                    "are actually running on this host (#2069) — the rest of a "
                    "python-lane roll that /update itself only does for "
                    "coord-agent. Call AFTER /update on the same host."
                ),
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "units": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    }
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Restart attempted for every running sibling unit"},
                    "400": {"description": "\"units\" named something not restartable here"},
                },
            }
        },
        "/rollback": {
            "post": {
                "summary": "Roll back to the previous blue/green venv generation and restart",
                "responses": {
                    "202": {"description": "Rolling back"},
                    "404": {"description": "No previous generation to roll back to"},
                    "409": {"description": "Live sessions running; pass force=true"},
                },
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
                                "properties": {
                                    "recent_secs": {"type": "number"},
                                    "protect": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": (
                                            "#1295: assignment IDs the caller "
                                            "considers non-terminal; the agent "
                                            "keeps their worktrees regardless "
                                            "of its own state.  Optional — an "
                                            "older agent without this field "
                                            "behaves exactly as before."
                                        ),
                                    },
                                },
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
        # server.health() can shell out to probe tool versions (#1570 B,
        # via AgentServer._cached_tool_versions -> probe_all) — real
        # subprocess.run calls with a per-tool timeout. Running that inline
        # would block this event loop (and every in-flight /assign) for up
        # to the probe timeout on a slow/hung tool. Cache TTL means this
        # only bites the first /health after a restart or every few
        # minutes, but push it off-loop regardless.
        data = await asyncio.to_thread(server.health)
        # #1886 Path B: `version` is bound at process import time (see the
        # module-level `from coord import __version__` above) and never
        # changes for the life of this process — it is the *running*,
        # loaded-module version. `installed_version` is a fresh disk read
        # (see `_installed_version` above) and changes the instant `pip`
        # writes to site-packages, regardless of whether this process has
        # restarted to pick it up. Exposing both — instead of just one,
        # ambiguous "version" — lets a caller (`coord agent update`'s poll
        # loop, `coord status`) detect a process that never restarted
        # after an update purely from /health, without inferring it from
        # liveness or PID.
        data["version"] = __version__
        data["installed_version"] = _installed_version()
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
        # #1567: ?rescue=1 opts into pushing the WIP commit to a disposable
        # rescue/<id> ref. Default (no query param, or any falsy value) is
        # to commit locally only and leave the remote branch untouched.
        rescue_param = request.query_params.get("rescue", "")
        rescue = rescue_param.strip().lower() in ("1", "true", "yes")
        # ``push_mode``, when given, overrides the rescue-derived default —
        # an internal-only escape hatch for callers that are not an operator
        # `coord stop` (e.g. `coord resume-stuck`, which cancels a stuck
        # worker but immediately dispatches a continuation onto the SAME
        # branch and needs the WIP pushed there, not withheld or diverted to
        # a rescue ref — see coord/commands/plan_followup.py::resume_stuck).
        push_mode = request.query_params.get("push_mode") or None
        try:
            assignment = server.cancel(
                assignment_id, rescue=rescue, push_mode=push_mode
            )
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
        """Atomically install the target version and restart the agent (#1241).

        Installs into a *fresh* venv slot next to the live one, smoke-checks
        it, then atomically flips ``~/.coord-venv`` onto it — see
        :mod:`coord.agent_update` for why an in-place ``pip install
        --upgrade`` isn't safe (it can leave a concurrent ``coord``
        invocation observing a half-written ``site-packages``). The process
        is restarted with ``exec_restart`` after a successful swap. Both the
        install and the restart run in a daemon-less background thread so
        the HTTP response reaches the caller before the process is
        replaced.

        Refuses outright — HTTP 409, nothing touched, no restart — rather
        than acting, in two cases:

        - **Editable install** (``pip install -e .``): ``~/.coord-venv``
          must stay a PyPI install (mirrors ``coord.health.checks.
          agent_install``'s ``agent_venv`` check). An editable checkout is
          reported as drift, never silently ``git pull``ed — the operator
          switches it back by hand (see ``docs/AGENT_OPERATIONS.md``'s
          editable → PyPI section).
        - **Live sessions**: when this agent has active (RUNNING/PENDING)
          assignments and the caller didn't pass ``{"force": true}`` — the
          restart-after-swap kills any in-flight worker, the same "never
          restart during live sessions" operator rule ``/restart`` already
          documents, now enforced here too.

        Request body (JSON, optional)::

            {"target_version": "0.4.85", "force": false}

        #1568: when the caller (``coord agent update``) knows exactly which
        release it's asking for, it passes ``target_version``, pinning the
        pip install to that exact version rather than a bare ``--upgrade``
        so a stale PyPI index/cache produces a loud pip failure ("no
        matching distribution") instead of a silent no-op. ``target_version``
        is echoed back in ``last_update`` so ``/health`` lets the caller
        verify the upgrade actually landed.
        """
        is_editable, project_path = _detect_install_mode()
        if is_editable:
            payload = {
                "mode": "editable (refused)",
                "started_at": time.time(),
                "finished_at": time.time(),
                "result": "refused",
                "error": (
                    f"editable install detected at {project_path!r} — "
                    "refusing to touch it automatically (#1241: "
                    "~/.coord-venv must stay a PyPI install). Switch it "
                    "back by hand — see docs/AGENT_OPERATIONS.md's "
                    "editable → PyPI section — then retry."
                ),
            }
            _write_last_update(server.state_dir, payload)
            return JSONResponse(payload, status_code=409)

        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if not isinstance(body, dict):
            body = {}
        target_version = body.get("target_version") or None
        force = bool(body.get("force"))

        with server._lock:
            active_count = sum(
                1
                for a in server._assignments.values()
                if a.status in (PENDING, RUNNING)
            )
        if active_count and not force:
            payload = {
                "mode": "pip install (blue/green)",
                "started_at": time.time(),
                "finished_at": time.time(),
                "target_version": target_version,
                "result": "refused",
                "error": (
                    f"{active_count} active assignment(s) running — "
                    "updating restarts the process and kills them "
                    'mid-flight. Pass {"force": true} (CLI: `coord agent '
                    "update --force`) to update anyway, or wait for them "
                    "to finish."
                ),
            }
            _write_last_update(server.state_dir, payload)
            return JSONResponse(payload, status_code=409)

        mode = "pip install (blue/green)"

        # Capture argv now — exec_restart replaces the process later.
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
                "target_version": target_version,
                "result": "failed",
                "error": None,
                "log_excerpt": "",
            }
            try:
                venv_dir = _venv_dir()
                result = agent_update.perform_update(
                    venv_dir, _agent_pkg_spec(), target_version=target_version,
                )
                payload["finished_at"] = time.time()
                # Persist the full venv/pip/smoke-check transcript to a log
                # file so the user can read it after the agent restarts.
                log_path = state_dir / "last_update.log"
                try:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(
                        f"# mode: {mode}\n"
                        f"# venv_dir: {venv_dir}\n"
                        f"# ok: {result.ok}  swapped: {result.swapped}\n"
                        f"# slot: {result.slot}  previous_slot: {result.previous_slot}\n\n"
                        f"{result.log}\n"
                    )
                except Exception:  # noqa: BLE001
                    pass
                # Keep a short excerpt inline so it appears in /health.
                tail = (result.log or "").splitlines()
                payload["log_excerpt"] = "\n".join(tail[-20:])

                if not result.ok:
                    payload["error"] = result.error or (
                        "blue/green update failed; see "
                        "~/.coord/last_update.log on this machine"
                    )
                    _write_last_update(state_dir, payload)
                    return

                # #1241: prefer the version the smoke check already read
                # straight from the new slot (deterministic, no reliance on
                # this process's own site-packages resolution having
                # noticed the symlink flip yet) — fall back to a fresh
                # in-process read only if that's somehow missing.
                version_after = result.new_version or _installed_version() or "unknown"
                payload["version_after"] = version_after
                if version_after == version_before:
                    # Swap "succeeded" but landed on the same version —
                    # shouldn't happen (the smoke check already verified
                    # target_version when one was given) but don't restart
                    # into a no-op.
                    payload["result"] = "no_change"
                    payload["error"] = (
                        f"swap completed but resolved to {version_after} "
                        "(same as before) — unexpected for a successful "
                        "blue/green update"
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

    async def deploy_units(request: Request) -> JSONResponse:
        """Install this host's systemd user units from the wheel (#1831/#1835).

        The `deploy/**` lane's missing *deploy step*. ``unit_drift`` already
        detects that ``~/.config/systemd/user/coord-*.service`` has fallen
        behind the units packaged in the installed release; until now the
        remedy was a human with ``cp`` and ``systemctl``, which is exactly
        the gap #1835 cannot ship around — #1543's whole mechanism was three
        unit files and a shell script.

        Ordering matters and is the caller's job, not this endpoint's: the
        reference is ``coord/deploy/`` *inside the installed distribution*,
        so this must run **after** that host's ``/update`` swapped the venv,
        or it re-installs the version the host already had. ``coord release
        propagate`` (:func:`coord.release_propagate.plan_lanes`) encodes that
        order.

        Synchronous, unlike ``/update``: writing a handful of unit files and
        running ``daemon-reload`` takes milliseconds and — critically —
        **restarts nothing**. A ``daemon-reload`` re-reads unit files; it
        does not restart running services, so no in-flight worker dies here.
        Restarting the affected services stays an explicit, separate
        operator action.

        Body (JSON, optional)::

            {"dry_run": false}

        A units-lane deploy touches only units this host *already* has
        installed, and keeps a ``.bak`` of each — see
        :mod:`coord.deploy_units` for why both are deliberate.
        """
        from coord import deploy_units as du  # noqa: PLC0415
        from coord.brain import AGENT_PORT  # noqa: PLC0415

        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if not isinstance(body, dict):
            body = {}
        dry_run = bool(body.get("dry_run"))

        report = du.install_units(
            machine_name=getattr(server, "machine_name", None),
            port=AGENT_PORT,
            version=_installed_version(),
            dry_run=dry_run,
        )
        payload = report.to_dict()
        payload["dry_run"] = dry_run
        payload["reloaded"] = False
        payload["reload_detail"] = ""
        if report.changed and not dry_run:
            ok, detail = du.daemon_reload()
            payload["reloaded"] = ok
            payload["reload_detail"] = detail
            if not ok:
                payload["ok"] = False
        return JSONResponse(payload, status_code=200 if payload.get("ok") else 500)

    async def restart_services(request: Request) -> JSONResponse:
        """Restart this host's sibling coord-* units (#2069).

        ``POST /update`` swaps the venv and re-execs **the agent** — and
        nothing else. ``coord-serve``, ``coord-web`` and ``coord-drive-queue``
        keep running the generation they started with until something
        restarts THEM, which used to mean a human. This is that something,
        meant to be called right after ``/update`` lands on the same host
        (see ``coord/commands/release.py``'s ``_roll_python``).

        Which of the three units to touch is decided HERE, from what
        ``coord.health.checks.spawned_coord`` finds actually running on this
        host right now — the same "which services a host runs is a topology
        decision, not a release decision" rule ``/deploy-units`` already
        follows. A host that never ran ``coord-web`` never gets one started
        by this call.

        Synchronous, unlike ``/update``: restarting a sibling process does
        not kill the request handling this restart (that's the whole reason
        it can wait for confirmation rather than fire-and-forget — see
        :func:`_restart_sibling_unit`).

        Request body (JSON, optional)::

            {"units": ["coord-serve", "coord-web"]}

        Omit ``units`` (or POST ``{}``/no body) to consider all of
        :data:`RESTARTABLE_SIBLING_UNITS`. Naming a unit outside that set is
        a 400 — ``coord-agent`` restarts itself through ``/update``,
        ``/rollback`` and ``/restart``, and doing it again from here would
        race those endpoints' own restart threads.
        """
        from coord.health.checks.spawned_coord import running_unit_pids  # noqa: PLC0415

        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if not isinstance(body, dict):
            body = {}
        raw_units = body.get("units")
        if raw_units is None:
            wanted = set(RESTARTABLE_SIBLING_UNITS)
        elif isinstance(raw_units, list):
            wanted = {str(u) for u in raw_units}
        else:
            return JSONResponse({"error": "\"units\" must be a list"}, status_code=400)
        unknown = wanted - RESTARTABLE_SIBLING_UNITS
        if unknown:
            return JSONResponse(
                {
                    "error": (
                        f"not a restartable sibling unit here: {', '.join(sorted(unknown))} "
                        f"— must be one of {', '.join(sorted(RESTARTABLE_SIBLING_UNITS))}"
                    )
                },
                status_code=400,
            )

        if not _running_under_systemd():
            return JSONResponse(
                {
                    "units": {},
                    "detail": (
                        "this agent is not running under systemd — nothing here "
                        "can restart a sibling unit"
                    ),
                },
                status_code=200,
            )

        running = running_unit_pids(tuple(sorted(wanted)))
        results: dict[str, dict] = {}
        all_ok = True
        for unit in sorted(wanted):
            if unit not in running:
                results[unit] = {"restarted": None, "detail": "not running on this host"}
                continue
            # #2069 follow-up: _restart_sibling_unit is synchronous — subprocess.run
            # calls plus a time.sleep(0.5) poll loop for up to `timeout` seconds per
            # unit. This handler is `async def`, so Starlette does not thread it
            # automatically; running that blocking work inline here would freeze the
            # single uvicorn event loop (no other request — status polls, cancels,
            # health checks — could be served) for up to ~90s across 3 units. Same
            # fix as `server.health` above: hand it to a worker thread and await it.
            restarted, detail = await asyncio.to_thread(_restart_sibling_unit, unit)
            results[unit] = {"restarted": restarted, "detail": detail}
            all_ok = all_ok and restarted
        return JSONResponse({"units": results}, status_code=200 if all_ok else 500)

    async def rollback(request: Request) -> JSONResponse:
        """Flip ``~/.coord-venv`` back onto the previous blue/green
        generation and restart (#1241).

        Every successful ``/update`` keeps exactly one prior generation on
        disk (see :mod:`coord.agent_update`) precisely so this exists.
        Refuses — 404, nothing touched — when there's no previous
        generation (e.g. this machine has never run a blue/green
        ``/update``), and 409 (same as ``/update``, same ``{"force":
        true}`` override) when live sessions are running.

        Request body (JSON, optional)::

            {"force": false}
        """
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if not isinstance(body, dict):
            body = {}
        force = bool(body.get("force"))

        with server._lock:
            active_count = sum(
                1
                for a in server._assignments.values()
                if a.status in (PENDING, RUNNING)
            )
        if active_count and not force:
            payload = {
                "mode": "rollback",
                "started_at": time.time(),
                "finished_at": time.time(),
                "result": "refused",
                "error": (
                    f"{active_count} active assignment(s) running — rolling "
                    "back restarts the process and kills them mid-flight. "
                    'Pass {"force": true} to roll back anyway, or wait for '
                    "them to finish."
                ),
            }
            _write_last_update(server.state_dir, payload)
            return JSONResponse(payload, status_code=409)

        venv_dir = _venv_dir()
        version_before = _installed_version() or "unknown"
        result = agent_update.rollback(venv_dir)
        if not result.ok:
            payload = {
                "mode": "rollback",
                "started_at": time.time(),
                "finished_at": time.time(),
                "version_before": version_before,
                "result": "failed",
                "error": result.error,
                "log_excerpt": "\n".join((result.log or "").splitlines()[-20:]),
            }
            _write_last_update(server.state_dir, payload)
            return JSONResponse(payload, status_code=404 if "no previous generation" in (result.error or "") else 500)

        saved_argv = list(sys.argv)
        state_dir = server.state_dir

        def _do_rollback() -> None:
            payload = {
                "mode": "rollback",
                "started_at": time.time(),
                "finished_at": time.time(),
                "version_before": version_before,
                "version_after": result.new_version or "unknown",
                "result": "upgraded",
                "error": None,
                "log_excerpt": "\n".join((result.log or "").splitlines()[-20:]),
            }
            _write_last_update(state_dir, payload)
            time.sleep(0.5)
            exec_restart(saved_argv)

        threading.Thread(target=_do_rollback, daemon=False, name="agent-rollback").start()
        return JSONResponse(
            {"status": "rolling back", "slot": str(result.slot)}, status_code=202
        )

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

        Optional JSON body::

            {
                "recent_secs": 300,           # override recency window (s)
                "protect": ["aid1", "aid2"]   # #1295: never sweep these AIDs
            }

        ``protect`` is optional and free-form — unknown/extra keys in the
        body are ignored, so a coordinator sending the new field to an
        older agent that ignores it, and a coordinator omitting the field
        entirely against a new agent, both work.  A protected entry is
        counted as ``kept`` in the response; the return shape is
        unchanged.
        """
        body: dict = {}
        try:
            body = await request.json()
        except Exception:
            pass
        recent_secs = float(body.get("recent_secs", 300.0))
        # #1295: accept "protect" as either a list or omitted.  Anything
        # non-list-like (a string, a dict, garbage) is dropped rather
        # than 400ing — we prefer to sweep what we can over rejecting an
        # otherwise-valid request because of a malformed side field.
        raw_protect = body.get("protect")
        protect: list[str] | None
        if isinstance(raw_protect, list):
            protect = [str(x) for x in raw_protect if isinstance(x, str)]
        else:
            protect = None
        result = server.clean_worktrees(recent_secs=recent_secs, protect=protect)
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
                    # #1567: this is an infra-triggered restart, not an
                    # operator `coord stop` — nobody decided this work was
                    # unwanted, so keep the pre-#1567 behaviour of pushing
                    # any WIP straight onto the worker's own branch.
                    server.cancel(aid, push_mode="branch")
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
        # #1831/#1835: the `deploy/**` lane's deploy step. Must be POSTed
        # AFTER /update on the same host — see the handler's docstring.
        Route("/deploy-units", deploy_units, methods=["POST"]),
        # #2069: restarts coord-serve/coord-web/coord-drive-queue — the rest
        # of a python-lane roll /update itself only does for coord-agent.
        # Must be POSTed AFTER /update on the same host, same reason as
        # /deploy-units above.
        Route("/restart-services", restart_services, methods=["POST"]),
        Route("/rollback", rollback, methods=["POST"]),
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
