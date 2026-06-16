"""``coord serve`` — the portable control center daemon (#584/#589/#594).

A lean, **read-only** Starlette app that fronts the coordinator board so any
Tailscale-reachable machine can render the same live board without a local
``~/.coord/coord.db`` or ``coordinator.yml``.

It mirrors the agent server (``coord/agent_app.py``, port 7433) and the dashboard
(``coord/dashboard/server.py``, port 7434); this daemon listens on **7435**.

Endpoints:

* ``GET /healthz``  — liveness; no DB access, never auth-gated.
* ``GET /board``    — the full board projection (``CoordStore.board_projection``).
* ``GET /config``   — the raw ``coordinator.yml`` bytes the daemon owns, so a
  client needs no local config file.
* ``POST /result``  — record an interactive-session result (#590); body is a
  serialized ``issue_store.ResultRecord``. Re-invokes the seam against the
  shared DB so a remote ``coord report-result`` lands here.
* ``POST /completion`` — record a git-floor backstop completion (#590); body is
  a serialized ``issue_store.CompletionRecord``.

The write endpoints call ``issue_store._post_*_local`` directly (never the
routing wrapper), so the daemon writes its own DB and can never recurse back out
over HTTP.

Auth: optional shared bearer token (defence-in-depth on top of Tailscale ACLs).
When no token is configured the endpoints are open (matching the agent/dashboard
servers, which have no auth). Per-user auth is #282 / team-mode territory.
"""

from __future__ import annotations

import os
from dataclasses import asdict, fields
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from coord.config import Config
from coord.dao import SCHEMA_VERSION, CoordStore

# Default port for the coordination daemon (agent=7433, dashboard=7434).
SERVE_PORT = 7435

# Server-side bearer token sources, in precedence order.  Distinct from the
# *client's* ``COORD_TOKEN`` so the two never collide on a box that runs both.
# The file source is what a systemd unit uses (an ``EnvironmentFile`` or a
# command-line ``--token`` would leak the secret into ``ps``).
SERVE_TOKEN_ENV = "COORD_SERVE_TOKEN"
SERVE_TOKEN_FILE = Path.home() / ".coord" / "serve_token"


def resolve_serve_token(flag_token: str | None = None) -> str | None:
    """Resolve the daemon's bearer token: flag > ``COORD_SERVE_TOKEN`` > file.

    Returns ``None`` when none is configured (the daemon runs open, relying on
    the Tailscale ACL — fine for dev/dogfood; the production daemon should set
    one).  A blank/whitespace token is treated as unset.
    """
    # Each source falls through to the next when blank/whitespace-only, so a
    # blank --token can't silently disable auth ahead of a configured env/file.
    for src in (flag_token, os.environ.get(SERVE_TOKEN_ENV)):
        if src and src.strip():
            return src.strip()
    if SERVE_TOKEN_FILE.exists():
        try:
            from_file = SERVE_TOKEN_FILE.read_text().strip()
        except OSError:
            from_file = ""
        if from_file:
            return from_file
    return None


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without ``Authorization: Bearer <token>`` (``/healthz`` exempt)."""

    def __init__(self, app, token: str) -> None:  # noqa: ANN001
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        if request.url.path != "/healthz":
            if request.headers.get("authorization", "") != self._expected:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app(store: CoordStore, config: Config, *, token: str | None = None) -> Starlette:
    """Build the read-only control-center Starlette app bound to *store* + *config*.

    *token* — when set, every endpoint except ``/healthz`` requires
    ``Authorization: Bearer <token>``.
    """

    async def healthz(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"status": "ok", "schema_version": SCHEMA_VERSION})

    async def board(request: Request) -> Response:  # noqa: ARG001
        try:
            return JSONResponse(store.board_projection())
        except Exception as e:  # noqa: BLE001 — surface a clean 503 rather than a stack trace
            return JSONResponse(
                {"error": "board read failed", "detail": str(e)}, status_code=503
            )

    async def serve_config(request: Request) -> Response:  # noqa: ARG001
        # Serve the raw coordinator.yml text the daemon owns; the client caches
        # it and feeds it to the existing coord.config.load() parser (config.py
        # has no dict round-trip, so raw YAML is the lossless contract).
        path = config.path
        if path is None or not path.exists():
            return JSONResponse(
                {"error": "no config file on the daemon host"}, status_code=404
            )
        return PlainTextResponse(path.read_text(), media_type="application/x-yaml")

    async def post_result(request: Request) -> Response:
        # #590: record an interactive result against the shared DB. Reconstruct
        # the ResultRecord from JSON (dropping unknown keys so a newer client
        # can't break an older daemon) and run the LOCAL seam path.
        from coord import issue_store  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        known = {f.name for f in fields(issue_store.ResultRecord)}
        try:
            record = issue_store.ResultRecord(
                **{k: v for k, v in body.items() if k in known}
            )
        except TypeError as e:
            return JSONResponse({"error": f"bad record: {e}"}, status_code=400)
        try:
            outcome = issue_store._post_result_local(record)
        except ValueError as e:  # invalid status / verdict
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "result write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse(asdict(outcome))

    async def post_completion(request: Request) -> Response:
        # #590: record a git-floor backstop completion against the shared DB.
        from coord import issue_store  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        known = {f.name for f in fields(issue_store.CompletionRecord)}
        try:
            record = issue_store.CompletionRecord(
                **{k: v for k, v in body.items() if k in known}
            )
        except TypeError as e:
            return JSONResponse({"error": f"bad record: {e}"}, status_code=400)
        try:
            outcome = issue_store._post_completion_local(record)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "completion write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(asdict(outcome))

    async def _read_json(request: Request) -> dict | None:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    def _kwargs(cls, data: dict) -> dict:
        known = {f.name for f in fields(cls)}
        return {k: v for k, v in data.items() if k in known}

    async def post_dispatched_work(request: Request) -> Response:
        # #590 Phase 2: record a thin client's work dispatch on the shared DB.
        from coord import state  # noqa: PLC0415
        from coord.models import Proposal  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            proposal = Proposal(**_kwargs(Proposal, body.get("proposal") or {}))
            state._record_dispatched_local(
                assignment_id=body["assignment_id"],
                proposal=proposal,
                repo_github=body["repo_github"],
                provider_name=body.get("provider_name"),
            )
        except (TypeError, KeyError) as e:
            return JSONResponse({"error": f"bad dispatch: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "dispatch write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"ok": True})

    async def post_dispatched(request: Request) -> Response:
        # #590 Phase 2: record a thin client's review/fix/rework/merge dispatch.
        from coord import state  # noqa: PLC0415
        from coord.models import Assignment  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            assignment = Assignment(**_kwargs(Assignment, body.get("assignment") or {}))
            state._record_dispatched_assignment_local(
                assignment=assignment, repo_github=body["repo_github"]
            )
        except (TypeError, KeyError) as e:
            return JSONResponse({"error": f"bad dispatch: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "dispatch write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"ok": True})

    async def post_test_verdict(request: Request) -> Response:
        # #590 Phase 2: record a Test-gate verdict on the shared DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._record_test_verdict_local(
                assignment_id=body["assignment_id"],
                test_state=body["test_state"],
                test_reason=body.get("test_reason"),
                smoke_test=body.get("smoke_test"),
                smoke_test_reason=body.get("smoke_test_reason"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "test-verdict write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_issue_labels(request: Request) -> Response:
        # #601: update one issue's cached labels (coord ready/backlog/refine/track).
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            updated = state._update_issue_labels_local(
                body["repo_name"], body["issue_number"], body.get("labels") or []
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-labels write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"updated": bool(updated)})

    async def post_issues_sync(request: Request) -> Response:
        # #601: upsert a repo's open issues into the shared issue cache (coord sync).
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._upsert_open_issues_local(body["repo_name"], body.get("issues") or [])
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issues-sync write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"ok": True})

    async def get_issue_context(request: Request) -> Response:
        # #603: read an issue's raw context entries (oldest-first) for the
        # briefing read-path / `coord context show` on a thin client.
        from coord import state  # noqa: PLC0415

        repo_name = request.query_params.get("repo_name")
        raw_issue = request.query_params.get("issue_number")
        if not repo_name or raw_issue is None:
            return JSONResponse(
                {"error": "repo_name and issue_number are required"}, status_code=400
            )
        try:
            issue_number = int(raw_issue)
        except (TypeError, ValueError):
            return JSONResponse({"error": "issue_number must be an int"}, status_code=400)
        try:
            entries = state._list_issue_context_local(repo_name, issue_number)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-context read failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"entries": entries})

    async def post_issue_context(request: Request) -> Response:
        # #603: add / pin / clear a per-issue context entry on the shared DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        action = body.get("action")
        try:
            if action == "add":
                entry_id = state._add_issue_context_entry_local(
                    body["repo_name"],
                    body["issue_number"],
                    body["body"],
                    pinned=bool(body.get("pinned")),
                    source=body.get("source"),
                )
                return JSONResponse({"entry_id": entry_id})
            if action == "pin":
                updated = state._set_issue_context_pin_local(
                    body["repo_name"],
                    body["issue_number"],
                    body["entry_id"],
                    bool(body.get("pinned")),
                )
                return JSONResponse({"updated": bool(updated)})
            if action == "clear":
                deleted = state._clear_issue_context_local(
                    body["repo_name"], body["issue_number"]
                )
                return JSONResponse({"deleted": deleted})
            if action == "replace":
                state._replace_issue_context_local(
                    body["repo_name"], body["issue_number"], body.get("entries") or []
                )
                return JSONResponse({"ok": True})
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-context write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"error": f"unknown action: {action!r}"}, status_code=400)

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/board", board, methods=["GET"]),
        Route("/config", serve_config, methods=["GET"]),
        Route("/result", post_result, methods=["POST"]),
        Route("/completion", post_completion, methods=["POST"]),
        Route("/dispatched-work", post_dispatched_work, methods=["POST"]),
        Route("/dispatched", post_dispatched, methods=["POST"]),
        Route("/test-verdict", post_test_verdict, methods=["POST"]),
        Route("/issue-labels", post_issue_labels, methods=["POST"]),
        Route("/issues-sync", post_issues_sync, methods=["POST"]),
        Route("/issue-context", get_issue_context, methods=["GET"]),
        Route("/issue-context", post_issue_context, methods=["POST"]),
    ]
    middleware = [Middleware(_BearerAuthMiddleware, token=token)] if token else []
    return Starlette(routes=routes, middleware=middleware)
