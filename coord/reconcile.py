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

    Returns assignment_ids whose status changed.
    """
    machines_by_name = {m.name: m for m in config.machines}
    active_by_machine: dict[str, list] = {}
    for a in board.active:
        active_by_machine.setdefault(a.machine_name, []).append(a)

    changed: list[str] = []
    for machine_name, assignments in active_by_machine.items():
        machine = machines_by_name.get(machine_name)
        if machine is None:
            continue
        status = _query_agent(machine.host)
        if status is None:
            continue

        completed_by_id = {
            e["id"]: e for e in status.get("completed", [])
        }
        for a in assignments:
            if a.assignment_id is None:
                continue
            entry = completed_by_id.get(a.assignment_id)
            if entry is None:
                continue
            branch = entry.get("branch")
            if branch:
                a.branch = branch
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

    return changed
