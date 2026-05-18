"""Reconcile the coordinator's board with live agent server state."""

from __future__ import annotations

import httpx

from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.models import Board


def _query_agent(host: str, port: int = AGENT_PORT, timeout: float = 5.0) -> dict | None:
    try:
        resp = httpx.get(f"http://{host}:{port}/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None


def reconcile(board: Board, config: Config) -> list[str]:
    """Poll agent servers and update board assignments that have finished.

    Returns assignment_ids whose status changed or were backfilled.
    """
    machines_by_name = {m.name: m for m in config.machines}

    # Collect all machines we need to query: those with active assignments
    # OR completed assignments missing branch info.
    machines_to_query: set[str] = set()
    for a in board.active:
        machines_to_query.add(a.machine_name)
    for a in board.completed:
        if a.branch is None and a.assignment_id is not None:
            machines_to_query.add(a.machine_name)

    # Query each machine once and cache the result.
    agent_completed: dict[str, dict] = {}
    for machine_name in machines_to_query:
        machine = machines_by_name.get(machine_name)
        if machine is None:
            continue
        status = _query_agent(machine.host)
        if status is None:
            continue
        for e in status.get("completed", []):
            agent_completed[e["id"]] = e

    changed: list[str] = []

    # Pass 1: transition active assignments that have finished.
    for a in board.active[:]:
        if a.assignment_id is None:
            continue
        entry = agent_completed.get(a.assignment_id)
        if entry is None:
            continue
        branch = entry.get("branch")
        if entry.get("status") == "done":
            board.mark_done_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
                branch=branch,
            )
        else:
            board.mark_failed_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
            )
        changed.append(a.assignment_id)

    # Pass 2: backfill branch on completed assignments that are missing it.
    for a in board.completed:
        if a.branch is not None or a.assignment_id is None:
            continue
        entry = agent_completed.get(a.assignment_id)
        if entry is None:
            continue
        branch = entry.get("branch")
        if branch:
            a.branch = branch
            changed.append(a.assignment_id)

    return changed
