"""``coord serve`` — the portable control center daemon (#584/#589/#594).

A lean, **read-only** Starlette app that fronts the coordinator board so any
Tailscale-reachable machine can render the same live board without a local
``~/.coord/coord.db`` or ``coordinator.yml``.

It mirrors the agent server (``coord/agent_app.py``, port 7433) and the dashboard
(``coord/dashboard/server.py``, port 7434); this daemon listens on **7435**.

Endpoints (read-only this slice — writes are #590):

* ``GET /healthz``  — liveness; no DB access, never auth-gated.
* ``GET /board``    — the full board projection (``CoordStore.board_projection``).
* ``GET /config``   — the raw ``coordinator.yml`` bytes the daemon owns, so a
  client needs no local config file.

Auth: optional shared bearer token (defence-in-depth on top of Tailscale ACLs).
When no token is configured the endpoints are open (matching the agent/dashboard
servers, which have no auth). Per-user auth is #282 / team-mode territory.
"""

from __future__ import annotations

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

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/board", board, methods=["GET"]),
        Route("/config", serve_config, methods=["GET"]),
    ]
    middleware = [Middleware(_BearerAuthMiddleware, token=token)] if token else []
    return Starlette(routes=routes, middleware=middleware)
