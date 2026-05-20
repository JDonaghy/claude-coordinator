"""Reconcile the coordinator's board with live agent server state."""

from __future__ import annotations

import time
import uuid

import httpx

from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.models import Assignment, Board


def _query_agent(host: str, port: int = AGENT_PORT, timeout: float = 5.0) -> dict | None:
    try:
        resp = httpx.get(f"http://{host}:{port}/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None


def _reassign(
    failed: Assignment, board: Board, config: Config,
    *,
    model: str | None = None,
) -> Assignment | None:
    """Re-dispatch a failed assignment to an idle different machine.

    *model* overrides the model tier on the retry. When None, the
    original assignment's model is reused (escalation happens at the call
    site).
    """
    busy = {a.machine_name for a in board.active if a.status == "running"}
    candidates = [
        m for m in config.machines
        if m.can_work_on(failed.repo_name)
        and m.repo_path(failed.repo_name) is not None
        and m.name not in busy
        and m.name != failed.machine_name
    ]
    if not candidates:
        candidates = [
            m for m in config.machines
            if m.can_work_on(failed.repo_name)
            and m.repo_path(failed.repo_name) is not None
            and m.name not in busy
        ]
    if not candidates:
        return None

    machine = candidates[0]
    repo_path = machine.repo_path(failed.repo_name)

    retry_model = model if model is not None else failed.model

    payload = {
        "repo_name": failed.repo_name,
        "repo_path": repo_path,
        "issue_number": failed.issue_number,
        "issue_title": f"[retry] {failed.issue_title}",
        "briefing": failed.briefing,
        "files_allowed": failed.files_allowed,
        "files_forbidden": failed.files_forbidden,
        "pull_repos": [],
        "type": "work",
        "model": retry_model,
    }

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    try:
        resp = httpx.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    retry_assignment = Assignment(
        machine_name=machine.name,
        repo_name=failed.repo_name,
        issue_number=failed.issue_number,
        issue_title=f"[retry] {failed.issue_title}",
        files_allowed=failed.files_allowed,
        files_forbidden=failed.files_forbidden,
        briefing=failed.briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        dispatched_at=time.time(),
        type="work",
        model=retry_model,
    )
    board.active.append(retry_assignment)
    return retry_assignment


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
    reachable_machines: set[str] = set()
    for machine_name in machines_to_query:
        machine = machines_by_name.get(machine_name)
        if machine is None:
            continue
        status = _query_agent(machine.host)
        if status is None:
            continue
        reachable_machines.add(machine_name)
        for e in status.get("completed", []):
            agent_completed[e["id"]] = e

    changed: list[str] = []
    newly_done_work: list = []  # assignments that just transitioned work → done
    newly_failed: list = []  # assignments that just transitioned to failed

    # Pass 1: transition active assignments that have finished.
    for a in board.active[:]:
        if a.assignment_id is None:
            continue

        # Track unreachable agents for stale detection
        if a.machine_name in machines_to_query and a.machine_name not in reachable_machines:
            a.unreachable_count = getattr(a, "unreachable_count", 0) + 1
            stale_threshold = getattr(config.concurrency, "stale_threshold", 3)
            if a.unreachable_count >= stale_threshold:
                board.mark_failed_by_id(a.assignment_id)
                newly_failed.append(a)
                changed.append(a.assignment_id)
            continue
        elif a.machine_name in reachable_machines:
            a.unreachable_count = 0

        entry = agent_completed.get(a.assignment_id)
        if entry is None:
            continue
        branch = entry.get("branch")
        if entry.get("status") == "done":
            done = board.mark_done_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
                branch=branch,
            )
            if done is not None and getattr(done, "type", "work") == "work":
                newly_done_work.append(done)
        else:
            failed = board.mark_failed_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
            )
            if failed is not None:
                newly_failed.append(failed)
        changed.append(a.assignment_id)

    # Auto-dispatch reviews for any work assignments that just finished.
    if getattr(config, "reviews", None) and config.reviews.enabled and config.reviews.auto_dispatch:
        from coord.review import dispatch_review

        for completed in newly_done_work:
            review = dispatch_review(completed, board, config)
            if review is not None and review.assignment_id is not None:
                changed.append(review.assignment_id)

    # Auto-queue smoke tests for any work assignments that just finished.
    # Independent of review dispatch — both can fire for the same completion.
    smoke_cfg = getattr(config, "smoke_tests", None)
    if smoke_cfg is not None and smoke_cfg.auto_queue:
        from coord.smoke import dispatch_smoke

        for completed in newly_done_work:
            smoke = dispatch_smoke(completed, board, config)
            if smoke is not None and smoke.assignment_id is not None:
                changed.append(smoke.assignment_id)

    # Auto-reassign failed work assignments to a different machine.
    if newly_failed and getattr(config.concurrency, "auto_reassign", False):
        for failed_a in newly_failed:
            if getattr(failed_a, "type", "work") != "work":
                continue
            reassigned = _reassign(failed_a, board, config)
            if reassigned is not None and reassigned.assignment_id is not None:
                changed.append(reassigned.assignment_id)

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
