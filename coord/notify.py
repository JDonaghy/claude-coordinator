"""Poll agent servers and post completion/failure comments to GitHub."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from coord.comments import EVENT_COMPLETION, EVENT_FAILURE
from coord.config import Config
from coord.dispatch import AGENT_PORT, post_completion, post_failure
from coord.state import load_dispatched, load_notified, mark_notified


@dataclass
class Transition:
    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    event: str  # completion | failure
    exit_code: int | None


def _agent_status(host: str, port: int = AGENT_PORT, timeout: float = 5.0) -> dict | None:
    try:
        resp = httpx.get(f"http://{host}:{port}/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None


def detect_transitions(config: Config) -> list[tuple[Transition, dict, dict]]:
    """Return (transition, dispatch_record, agent_assignment) for each
    assignment whose terminal state has not yet been notified.

    Splitting detection from posting makes the loop testable without
    mocking GitHub.
    """
    dispatched = load_dispatched()
    if not dispatched:
        return []
    notified = load_notified()
    by_id = {r["assignment_id"]: r for r in dispatched}

    # Collect machine hostnames we care about
    machines_by_name = {m.name: m for m in config.machines}
    needed = {r["machine_name"] for r in dispatched if r["assignment_id"] not in notified}

    transitions: list[tuple[Transition, dict, dict]] = []
    for machine_name in needed:
        machine = machines_by_name.get(machine_name)
        if machine is None:
            continue
        status = _agent_status(machine.host)
        if status is None:
            continue
        for entry in status.get("completed", []):
            aid = entry.get("id")
            record = by_id.get(aid)
            if record is None or aid in notified:
                continue
            entry_status = entry.get("status")
            if entry_status == "done":
                event = EVENT_COMPLETION
            elif entry_status in ("failed", "cancelled"):
                event = EVENT_FAILURE
            else:
                continue
            transitions.append(
                (
                    Transition(
                        assignment_id=aid,
                        machine_name=record["machine_name"],
                        repo_name=record["repo_name"],
                        issue_number=record["issue_number"],
                        event=event,
                        exit_code=entry.get("exit_code"),
                    ),
                    record,
                    entry,
                )
            )
    return transitions


def post_transition(transition: Transition, record: dict, entry: dict) -> None:
    """Post the GitHub comment for one transition and mark it notified."""
    started = entry.get("started_at")
    finished = entry.get("finished_at")
    duration = (finished - started) if (started and finished) else None
    common = dict(
        assignment_id=transition.assignment_id,
        machine_name=transition.machine_name,
        repo_github=record["repo_github"],
        repo_name=transition.repo_name,
        issue_number=transition.issue_number,
        duration_seconds=duration,
        log_path=entry.get("log_path"),
    )
    if transition.event == EVENT_COMPLETION:
        post_completion(exit_code=transition.exit_code or 0, **common)
    else:
        post_failure(
            exit_code=transition.exit_code,
            error=entry.get("error") or "",
            **common,
        )
    mark_notified(transition.assignment_id, transition.event)


def run(config: Config) -> list[Transition]:
    """Detect and post all pending transitions. Returns what was posted."""
    posted: list[Transition] = []
    for transition, record, entry in detect_transitions(config):
        try:
            post_transition(transition, record, entry)
        except Exception:  # noqa: BLE001 — surface to caller; continue with rest
            continue
        posted.append(transition)
    return posted
