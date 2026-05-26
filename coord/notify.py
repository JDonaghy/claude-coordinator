"""Poll agent servers and post completion/failure comments to GitHub."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# Cache: machine_name → host. Populated by `run(config)` so post_transition →
# _try_parse_and_post_review can fetch a remote agent's log via /logs/<id>
# without threading the Config through every helper.
_AGENT_HOSTS: dict[str, str] = {}


def _agent_host(machine_name: str) -> str | None:
    return _AGENT_HOSTS.get(machine_name)

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
from coord.state import (
    load_dispatched,
    load_done_reviews_needing_post,
    load_notified,
    mark_notified,
    mark_review_posted,
    save_plan,
)


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
            # Cancelled-on-agent for an assignment the DB already marks done
            # is cleanup noise (e.g. operator ran POST /cancel to unstick a
            # hung reap). Don't post a false failure for it.
            db_status = (record.get("status") or "").lower()
            if entry_status == "cancelled" and db_status == "done":
                continue
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


def _persist_review_verdict(assignment_id: str, verdict: str) -> None:
    """Store the parsed reviewer verdict on the review assignment row.

    #253: consumed by ``coord.merge_queue.has_approved_review`` so the merge
    gate can refuse to merge work whose review hasn't approved.  Best-effort;
    a DB error is logged and swallowed (the merge gate falls back to "no
    approval found" which is the safe answer).
    """
    if verdict not in ("approve", "request-changes"):
        return
    try:
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
        with conn:
            conn.execute(
                "UPDATE assignments SET review_verdict = ? WHERE assignment_id = ?",
                (verdict, assignment_id),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Failed to persist review_verdict for %s: %s", assignment_id, exc
        )


def _try_parse_and_post_review(
    transition: Transition,
    record: dict,
    entry: dict,
    duration: float | None,
) -> bool:
    """Parse reviewer findings from the log and post as a PR review or issue comment.

    Returns True if a review was successfully posted (either as a ``gh pr review``
    or as an issue comment when no PR number is available), False on any failure.
    Silently swallows all errors so callers can fall back gracefully.
    """
    from coord.review import parse_review_from_log, parse_review_from_agent  # noqa: PLC0415

    log_path = entry.get("log_path")
    findings = None
    if log_path:
        try:
            findings = parse_review_from_log(log_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to parse review log for %s: %s", transition.assignment_id, exc)

    # Local file unavailable (worker ran on a remote agent whose log isn't on
    # this filesystem) — fetch via the agent's /logs endpoint and parse the
    # same way. Agents never use gh; the coordinator pulls + posts.
    if findings is None:
        host = _agent_host(transition.machine_name)
        if host:
            try:
                findings = parse_review_from_agent(host, transition.assignment_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Failed to fetch review log from agent %s for %s: %s",
                    host, transition.assignment_id, exc,
                )

    if findings is None:
        return False

    # #253: persist the parsed verdict on the review assignment so the merge
    # gate can refuse to merge work whose review hasn't approved.  Independent
    # of auto_loop (which may be disabled in config).
    _persist_review_verdict(transition.assignment_id, findings.verdict)

    review_target = record.get("review_target")
    repo_github = record["repo_github"]

    # Determine whether review_target is a PR number (integer string) or a branch.
    pr_number: int | None = None
    if review_target:
        try:
            pr_number = int(review_target)
        except (ValueError, TypeError):
            pr_number = None

    if pr_number is not None:
        try:
            github_ops.post_pr_review(repo_github, pr_number, findings.verdict, findings.body)
            mark_review_posted(transition.assignment_id)
            return True
        except Exception as exc:  # noqa: BLE001
            # GitHub rejects self-reviews (same user who opened the PR can't
            # review it via the API). Log the actual error and fall through to
            # post the findings as an issue comment instead of silently failing.
            log.warning(
                "Failed to post PR review for %s PR#%s via gh: %s — "
                "falling back to issue comment",
                transition.assignment_id, pr_number, exc,
            )
            # Fall through to the issue-comment path below.

    # No PR number available, or gh pr review was rejected — post findings as
    # an issue comment so they are never silently lost.
    verdict_label = "✅ Approved" if findings.verdict == "approve" else "⚠️ Changes Requested"
    if pr_number is not None:
        preamble = (
            f"*Reviewer findings could not be posted directly to PR #{pr_number} "
            f"(gh pr review was rejected — likely a self-review restriction). "
            f"Findings are reproduced here.*"
        )
    else:
        preamble = (
            "*Reviewer could not post directly to a PR (no PR number available). "
            "Findings are reproduced here.*"
        )
    body = (
        f"## Review Complete — {verdict_label}\n\n"
        f"{preamble}\n\n"
        f"{findings.body}"
    )
    try:
        github_ops.post_issue_comment(repo_github, transition.issue_number, body)
        mark_review_posted(transition.assignment_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Failed to post review comment for %s: %s", transition.assignment_id, exc
        )
        return False


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
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to parse plan log for %s: %s", transition.assignment_id, exc)
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
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to post plan comment for %s: %s", transition.assignment_id, exc)
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
    elif transition.event == EVENT_COMPLETION and assignment_type == "review":
        # For review assignments, parse the structured findings and post as a
        # PR review (or issue comment when no PR number is available).  Fall
        # back to a plain completion comment noting the parse failure.
        posted = _try_parse_and_post_review(transition, record, entry, duration)
        if not posted:
            post_completion(
                exit_code=transition.exit_code or 0,
                summary=(
                    "Review assignment completed but findings could not be extracted "
                    "from the worker log. The reviewer may not have produced the "
                    "expected structured output (REVIEW_VERDICT / REVIEW_BODY / END_REVIEW)."
                ),
                **common,
            )
        mark_notified(
            transition.assignment_id,
            transition.event,
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


def post_orphaned_review_findings(
    config: Config,
    repo_name: str | None = None,
) -> list[str]:
    """Walk done-review assignments with unposted findings and attempt to post.

    Handles two scenarios that cause findings to be lost:

    1. The agent reported the assignment as 'done' but notify never ran (or
       ran at the wrong time) — no notification record in the DB at all.
    2. Notify ran and posted a fallback completion comment (because the log
       couldn't be parsed at that time), but findings were never extracted.

    In both cases ``review_posted_at`` is NULL on the assignment row.

    The function queries each relevant agent server to discover the log path,
    then re-parses and re-posts.  If the agent is offline or its completed
    list no longer contains the assignment, the entry is silently skipped
    so ``coord notify`` stays non-fatal.

    Returns a list of assignment_ids for which findings were successfully posted.
    Optionally filter to a single *repo_name*.
    """
    from coord.review import parse_review_from_log  # noqa: PLC0415

    candidates = load_done_reviews_needing_post(repo_name=repo_name)
    if not candidates:
        return []

    notified = load_notified()
    machines_by_name = {m.name: m for m in config.machines}

    # Group by machine so we query each agent server once.
    by_machine: dict[str, list[dict]] = {}
    for row in candidates:
        by_machine.setdefault(row["machine_name"], []).append(row)

    posted_ids: list[str] = []
    for machine_name, rows in by_machine.items():
        machine = machines_by_name.get(machine_name)
        if machine is None:
            log.debug("post_orphaned: unknown machine %r — skipping %d assignment(s)", machine_name, len(rows))
            continue

        status = _agent_status(machine.host)
        log_by_id: dict[str, str] = {}
        if status:
            for entry in status.get("completed", []):
                eid = entry.get("id")
                lp = entry.get("log_path")
                if eid and lp:
                    log_by_id[eid] = lp

        for row in rows:
            aid = row["assignment_id"]
            log_path = log_by_id.get(aid)
            findings = None
            # Try local file first (cheap) — works when notify runs on the
            # same host as the agent. Falls back to fetching via HTTP so the
            # coordinator can post reviews from any machine.
            if log_path:
                try:
                    findings = parse_review_from_log(log_path)
                except Exception as exc:  # noqa: BLE001
                    log.warning("post_orphaned: failed to parse local log for %s: %s", aid, exc)
            if findings is None and machine.host:
                from coord.review import parse_review_from_agent  # noqa: PLC0415
                try:
                    findings = parse_review_from_agent(machine.host, aid)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "post_orphaned: failed to fetch log from agent %s for %s: %s",
                        machine.host, aid, exc,
                    )
            if findings is None:
                log.debug("post_orphaned: no findings (local + agent both missed) for %s", aid)
                continue

            review_target = row.get("review_target")
            repo_github = row.get("repo_github") or ""
            issue_number = row.get("issue_number", 0)

            pr_number: int | None = None
            if review_target:
                try:
                    pr_number = int(review_target)
                except (ValueError, TypeError):
                    pr_number = None

            # Build a preamble that distinguishes retroactive posts from fresh ones.
            already_notified = aid in notified
            if already_notified:
                retro_note = (
                    "\n\n*Note: a completion comment was posted earlier but findings "
                    "could not be extracted at that time. These are the retroactive findings.*"
                )
            else:
                retro_note = ""

            posted = False
            if pr_number is not None:
                try:
                    github_ops.post_pr_review(repo_github, pr_number, findings.verdict, findings.body + retro_note)
                    posted = True
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "post_orphaned: failed gh pr review for %s PR#%s: %s — "
                        "falling back to issue comment",
                        aid, pr_number, exc,
                    )

            if not posted:
                verdict_label = "✅ Approved" if findings.verdict == "approve" else "⚠️ Changes Requested"
                if pr_number is not None:
                    preamble = (
                        f"*Reviewer findings could not be posted directly to PR #{pr_number} "
                        f"(gh pr review was rejected — likely a self-review restriction). "
                        f"Findings are reproduced here.*"
                    )
                else:
                    preamble = (
                        "*Reviewer could not post directly to a PR (no PR number available). "
                        "Findings are reproduced here.*"
                    )
                body = (
                    f"## Review Complete — {verdict_label}\n\n"
                    f"{preamble}{retro_note}\n\n"
                    f"{findings.body}"
                )
                try:
                    github_ops.post_issue_comment(repo_github, issue_number, body)
                    posted = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("post_orphaned: failed to post comment for %s: %s", aid, exc)

            if posted:
                mark_review_posted(aid)
                if not already_notified:
                    mark_notified(aid, EVENT_COMPLETION)
                posted_ids.append(aid)
                log.info("post_orphaned: posted findings for review %s", aid)

    return posted_ids


def _dispatch_board_pending_reviews(config: Config) -> None:
    """Load the board, dispatch any pending reviews, and save.

    Mirrors the review-dispatch loop in reconcile() so that ``coord notify``
    also triggers review dispatch — not just ``coord status --reconcile``.
    Safe to call even when the board file doesn't exist.
    """
    from coord.review import dispatch_review
    from coord.state import load_board, save_board

    board = load_board()
    if board is None:
        return

    # Match the gating in reconcile() exactly so notify and reconcile agree
    # on whether a review should be dispatched.  See coord/reconcile.py.
    test_gate_active = "test" in (config.pipeline.default_gates or [])

    changed = False
    for completed in board.completed:
        # NULL counts as "pending" — see comment in reconcile().
        if completed.review_state not in (None, "pending"):
            continue
        if completed.type != "work":
            continue
        if test_gate_active and completed.test_state not in ("passed", "skipped"):
            continue
        review = dispatch_review(completed, board, config)
        if review is not None:
            completed.review_state = "dispatched"
            changed = True
        # On failure leave as "pending" so the next notify call retries.

    if changed:
        save_board(board)


def run(config: Config) -> tuple[list[Transition], list[StuckDetection]]:
    """Detect and post all pending transitions and stuck signals.

    Also dispatches any pending reviews found on the saved board so that
    ``coord notify`` acts as a reliable review-dispatch trigger in addition
    to ``coord status --reconcile``.

    Returns (posted_transitions, posted_stuck).
    """
    # Refresh the agent-host cache so _try_parse_and_post_review (and any
    # other helper using _agent_host) can resolve hostnames without
    # threading config through every call.
    global _AGENT_HOSTS
    _AGENT_HOSTS = {m.name: m.host for m in config.machines}

    # Collect (transition, record, entry) tuples for review completions so we
    # can feed them to the auto-loop after all notifications are posted.
    review_completions: list[tuple[Transition, dict, dict]] = []

    posted: list[Transition] = []
    for transition, record, entry in detect_transitions(config):
        try:
            post_transition(transition, record, entry)
        except Exception:  # noqa: BLE001 — surface to caller; continue with rest
            continue
        posted.append(transition)
        # Track completed reviews for auto-loop processing below.
        from coord.comments import EVENT_COMPLETION  # noqa: PLC0415
        if (
            record.get("type") == "review"
            and transition.event == EVENT_COMPLETION
        ):
            review_completions.append((transition, record, entry))

    # Also detect and post stuck signals
    stuck_posted: list[StuckDetection] = []
    for detection, record in detect_stuck(config):
        try:
            post_stuck(detection, record)
        except Exception:  # noqa: BLE001
            continue
        stuck_posted.append(detection)

    # Dispatch pending reviews from the saved board (best-effort, non-fatal).
    try:
        _dispatch_board_pending_reviews(config)
    except Exception:  # noqa: BLE001
        pass

    # Post findings for done-review assignments that were never processed
    # (e.g. agent reported 'cancelled', user manually marked done, or notify
    # ran at the wrong time).  Best-effort, non-fatal.
    try:
        post_orphaned_review_findings(config)
    except Exception:  # noqa: BLE001
        log.exception("post_orphaned_review_findings: unexpected error")

    # Auto-loop: for each completed review, optionally dispatch a fix worker.
    # Runs after notify posts the completion comment so GitHub has the full
    # review body before any fix briefing references "previous findings".
    if review_completions:
        try:
            from coord.auto_loop import run_for_review_transition  # noqa: PLC0415
            for transition, record, entry in review_completions:
                try:
                    actions = run_for_review_transition(
                        transition.assignment_id, record, entry, config
                    )
                    for action in actions:
                        log.info(
                            "auto_loop %s: %s (assignment=%s)",
                            action.kind, action.detail, action.assignment_id,
                        )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "auto_loop: error processing review %s",
                        transition.assignment_id,
                    )
        except Exception:  # noqa: BLE001
            log.exception("auto_loop: unexpected error in review completion loop")

    return posted, stuck_posted
