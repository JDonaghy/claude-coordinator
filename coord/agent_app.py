"""Starlette HTTP layer over `AgentServer`."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from coord.agent import AgentServer, AssignmentSpec


def build_app(server: AgentServer) -> Starlette:
    """Build the Starlette app bound to a specific AgentServer instance."""

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(server.health())

    async def status(request: Request) -> JSONResponse:
        return JSONResponse(server.list_assignments())

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

    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/status", status, methods=["GET"]),
        Route("/assign", assign, methods=["POST"]),
        Route("/cancel/{id}", cancel, methods=["POST"]),
    ]
    return Starlette(routes=routes)
