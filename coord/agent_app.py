"""Starlette HTTP layer over `AgentServer`."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from coord.agent import AgentServer, AssignmentSpec


def build_app(server: AgentServer) -> Starlette:
    """Build the Starlette app bound to a specific AgentServer instance."""

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(server.health())

    async def status(request: Request) -> JSONResponse:
        return JSONResponse(server.list_assignments())

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

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
        Route("/repos", repos, methods=["GET"]),
        Route("/assign", assign, methods=["POST"]),
        Route("/cancel/{id}", cancel, methods=["POST"]),
        Route("/logs/{id}", logs, methods=["GET"]),
    ]
    return Starlette(routes=routes)
