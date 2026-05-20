"""Poll agent servers and post completion/failure comments to GitHub."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from coord import github_ops
from coord.comments import (
    EVENT_COMPLETION,
    EVENT_FAILURE,
    EVENT_PLAN,
    EVENT_STUCK,
    format_plan,
    format_stuck,
)
from coord.config import Config
from coord.dispatch import AGENT_PORT, post_completion, post_failure
from coord.progress import parse_progress
from coord.state import load_dispatched, load_notified, mark_notified, save_plan


@dataclass
class Transition:
    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    event: str  # completion | failure
    exit_code: int | None


@dataclass
class StuckDetection:
    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    stuck_message: str
    log_path: str | None


def _stuck_notified_key(assignment_id: str) -> str:
    """Notified ledger key for stuck events.

    Uses a composite key so that a stuck notification does not block later
    completion/failure notifications (which key on bare assignment_id).
    """
    return f"{assignment_id}:stuck"


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


def detect_stuck(config: Config) -> list[tuple[StuckDetection, dict]]:
    """Scan active worker logs for STUCK signals.

    Returns (StuckDetection, dispatch_record) for each stuck worker that
    hasn't already been notified as stuck.
    """
    dispatched = load_dispatched()
    if not dispatched:
        return []
    notified = load_notified()
    by_id = {r["assignment_id"]: r for r in dispatched}

    machines_by_name = {m.name: m for m in config.machines}

    # Only look at assignments that haven't been notified at all (still active)
    # and haven't already been notified as stuck.
    active_records = [
        r for r in dispatched
        if r["assignment_id"] not in notified
        and _stuck_notified_key(r["assignment_id"]) not in notified
    ]
    if not active_records:
        return []

    # Group by machine
    by_machine: dict[str, list[dict]] = {}
    for r in active_records:
        by_machine.setdefault(r["machine_name"], []).append(r)

    results: list[tuple[StuckDetection, dict]] = []
    for machine_name, records in by_machine.items():
        machine = machines_by_name.get(machine_name)
        if machine is None:
            continue
        status = _agent_status(machine.host)
        if status is None:
            continue

        # Build lookup of active entries by id
        active_by_id: dict[str, dict] = {}
        for entry in status.get("active", []):
            eid = entry.get("id")
            if eid:
                active_by_id[eid] = entry

        for record in records:
            aid = record["assignment_id"]
            entry = active_by_id.get(aid)
            if entry is None:
                continue

            stuck_message: str | None = None
            log_path: str | None = None

            # Check progress data from agent status
            progress = entry.get("progress")
            if progress and progress.get("stuck"):
                stuck_message = progress["stuck"]
                log_path = entry.get("log_path")

            # Also try parsing the log file directly
            entry_log = entry.get("log_path")
            if entry_log and not stuck_message:
                try:
                    parsed = parse_progress(entry_log)
                    if parsed.stuck:
                        stuck_message = parsed.stuck
                        log_path = entry_log
                except Exception:  # noqa: BLE001
                    pass

            if stuck_message:
                results.append(
                    (
                        StuckDetection(
                            assignment_id=aid,
                            machine_name=record["machine_name"],
                            repo_name=record["repo_name"],
                            issue_number=record["issue_number"],
                            stuck_message=stuck_message,
                            log_path=log_path,
                        ),
                        record,
                    )
                )

    return results


def post_stuck(detection: StuckDetection, record: dict) -> None:
    """Post a stuck comment to GitHub and mark notified."""
    body = format_stuck(
        assignment_id=detection.assignment_id,
        machine_name=detection.machine_name,
        repo_name=detection.repo_name,
        issue_number=detection.issue_number,
        stuck_message=detection.stuck_message,
    )
    github_ops.post_issue_comment(
        record["repo_github"], detection.issue_number, body
    )
    mark_notified(_stuck_notified_key(detection.assignment_id), EVENT_STUCK)


def _try_parse_and_post_plan(
    transition: Transition,
    record: dict,
    entry: dict,
    duration: float | None,
) -> bool:
    """Try to parse a WorkerPlan from the worker log and post it to GitHub.

    Returns True if a plan comment was successfully posted, False otherwise.
    Silently swallows all errors so callers can fall back gracefully.
    """
    from coord.plan_parser import parse_plan_from_log  # noqa: PLC0415

    log_path = entry.get("log_path")
    if not log_path:
        return False

    try:
        worker_plan = parse_plan_from_log(log_path)
    except Exception:  # noqa: BLE001
        return False

    if worker_plan is None or worker_plan.is_empty():
        return False

    try:
        body = format_plan(
            assignment_id=transition.assignment_id,
            machine_name=transition.machine_name,
            repo_name=transition.repo_name,
            issue_number=transition.issue_number,
            plan=worker_plan,
            duration_seconds=duration,
        )
        github_ops.post_issue_comment(
            record["repo_github"], transition.issue_number, body
        )
        # Cache the parsed plan in the state directory.
        save_plan(transition.assignment_id, worker_plan.to_dict())
    except Exception:  # noqa: BLE001
        return False

    return True


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
    assignment_type = record.get("type", "work")
    if transition.event == EVENT_COMPLETION and assignment_type == "plan":
        # For plan assignments, post the structured plan comment.  Fall back
        # to a standard completion comment if the log can't be parsed.
        posted = _try_parse_and_post_plan(transition, record, entry, duration)
        if not posted:
            post_completion(exit_code=transition.exit_code or 0, **common)
        mark_notified(
            transition.assignment_id,
            EVENT_PLAN if posted else EVENT_COMPLETION,
            branch=entry.get("branch"),
        )
    elif transition.event == EVENT_COMPLETION:
        post_completion(exit_code=transition.exit_code or 0, **common)
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
    else:
        post_failure(
            exit_code=transition.exit_code,
            error=entry.get("error") or "",
            **common,
        )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )


def run(config: Config) -> tuple[list[Transition], list[StuckDetection]]:
    """Detect and post all pending transitions and stuck signals.

    Returns (posted_transitions, posted_stuck).
    """
    posted: list[Transition] = []
    for transition, record, entry in detect_transitions(config):
        try:
            post_transition(transition, record, entry)
        except Exception:  # noqa: BLE001 — surface to caller; continue with rest
            continue
        posted.append(transition)

    # Also detect and post stuck signals
    stuck_posted: list[StuckDetection] = []
    for detection, record in detect_stuck(config):
        try:
            post_stuck(detection, record)
        except Exception:  # noqa: BLE001
            continue
        stuck_posted.append(detection)

    return posted, stuck_posted
