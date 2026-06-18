"""Issue-store seam (#466) — the one and only path through which the
interactive-launcher git-floor backstop and the ``coord report-result``
subcommand write a session result to the message bus and the local DB.

This module is the deliberately narrow contract that the future
:issue:`183` ``IssueStore`` refactor (and the planned coordination MCP
server) will slot into.  Until then, the GitHub backend is the existing
:mod:`coord.github_ops` ``gh``-CLI wrapper.  Callers MUST NOT reach
around this seam — the whole point is that swapping the backend later
must not require touching the launcher, the CLI subcommand, or the
pipeline-state code paths that consume completions.

Two public surface entry points are intentionally provided:

* :func:`post_completion` — the **git-floor backstop** sink, called by
  the interactive launcher exit path.  Inputs are everything the
  launcher learned from the local filesystem alone: ``exit_code``,
  ``commits_ahead``, the assignment metadata.  This function chooses
  ``done`` vs ``advisory`` vs ``failed`` purely from those numbers — it
  does NOT trust any agent self-report.

* :func:`post_result` — the structured-report sink, called by
  ``coord report-result``.  Inputs are the result the interactive
  agent typed (``status``, ``verdict``, ``summary``) plus the
  assignment id.  The agent is expected to invoke this **before**
  exiting; this is the only coordinator-mediated command the
  interactive agent is allowed to run.  Required for review sessions
  (0 commits → verdict can only come from the agent).

Both entry points fan in to the same private helpers that update the
local assignments table and post a coordinator-authored comment on the
issue, so the pipeline sees an interactive completion identically to a
``claude -p`` worker completion.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Literal

from coord import github_ops
from coord.comments import (
    EVENT_ADVISORY,
    EVENT_COMPLETION,
    EVENT_FAILURE,
    format_advisory,
    format_completion,
    format_failure,
)


__all__ = [
    "CompletionRecord",
    "ResultRecord",
    "post_completion",
    "post_result",
    "ResultStatus",
    "ResultVerdict",
    "STATUS_DONE",
    "STATUS_BLOCKED",
    "STATUS_ALREADY_IMPLEMENTED",
    "VERDICT_APPROVE",
    "VERDICT_REQUEST_CHANGES",
]


# ── Public enum-style constants ─────────────────────────────────────────────

STATUS_DONE = "done"
STATUS_BLOCKED = "blocked"
STATUS_ALREADY_IMPLEMENTED = "already-implemented"

VERDICT_APPROVE = "approve"
VERDICT_REQUEST_CHANGES = "request-changes"

_VALID_STATUSES = (STATUS_DONE, STATUS_BLOCKED, STATUS_ALREADY_IMPLEMENTED)
_VALID_VERDICTS = (VERDICT_APPROVE, VERDICT_REQUEST_CHANGES)

ResultStatus = Literal["done", "blocked", "already-implemented"]
ResultVerdict = Literal["approve", "request-changes"]


# ── Records (the wire shape the future IssueStore interface accepts) ────────


@dataclass
class CompletionRecord:
    """Inputs to :func:`post_completion` — the git-floor backstop path.

    Mirrors the future ``IssueStore.post_completion`` signature so the
    backend can be swapped to MCP without changing the call sites.
    """

    assignment_id: str
    machine_name: str
    repo_name: str
    repo_github: str
    issue_number: int
    exit_code: int
    commits_ahead: int | None  # None = unknown (git failed) → treat as non-zero
    branch: str | None = None
    duration_seconds: float | None = None
    log_path: str | None = None
    summary: str = ""


@dataclass
class ResultRecord:
    """Inputs to :func:`post_result` — the ``coord report-result`` path."""

    assignment_id: str
    machine_name: str
    repo_name: str
    repo_github: str
    issue_number: int
    status: ResultStatus
    verdict: ResultVerdict | None
    summary: str
    duration_seconds: float | None = None
    log_path: str | None = None
    branch: str | None = None
    # Full review/findings body (markdown). When present it is persisted on the
    # assignment row (review_findings) AND posted to the issue under a
    # machine-parseable marker so the fix worker can recover it from any machine
    # via the GitHub message bus (not just the one-line `summary`).
    findings_body: str | None = None


# ── Resolved terminal state (what the seam writes back) ─────────────────────


@dataclass
class StoreOutcome:
    """What the seam ended up writing.  Returned for caller diagnostics
    (and for tests so they can assert the chosen branch without
    re-reading the DB)."""

    status: str  # "done" | "advisory" | "failed"
    event: str   # comments.EVENT_*
    posted: bool  # True iff the GitHub comment post succeeded
    error: str | None = None  # populated when post failed


# ── Internal helpers — the ONE place this module touches state/github_ops ──


def _update_local_state(
    *,
    assignment_id: str,
    terminal_status: str,
    branch: str | None,
    review_state: str | None,
) -> None:
    """Update the local assignments row + notifications ledger.

    Encapsulates the SQL so the rest of the module never touches
    ``coord.state`` or the DB directly — keeps the seam clean for the
    future :issue:`183` refactor (which will likely replace this with
    an :class:`IssueStore` write).
    """
    # Import inside the function so test fixtures that stub the seam can
    # still import this module without dragging in the DB layer.
    from coord.state import get_connection  # noqa: PLC0415

    if not assignment_id:
        return
    now = time.time()
    conn = get_connection()
    fields = ["status=?", "finished_at=?"]
    params: list[object] = [terminal_status, now]
    if branch is not None:
        fields.append("branch=?")
        params.append(branch)
    if review_state is not None:
        fields.append("review_state=?")
        params.append(review_state)
    params.append(assignment_id)
    conn.execute(
        f"UPDATE assignments SET {', '.join(fields)} WHERE assignment_id=?",
        tuple(params),
    )
    conn.commit()


def _record_notification(*, assignment_id: str, event: str, branch: str | None) -> None:
    """Best-effort notification-ledger write so ``coord notify`` won't
    double-post the same completion."""
    from coord.state import get_connection  # noqa: PLC0415

    if not assignment_id:
        return
    conn = get_connection()
    conn.execute(
        """INSERT OR REPLACE INTO notifications
                (assignment_id, event, branch, posted_at)
           VALUES (?, ?, ?, ?)""",
        (assignment_id, event, branch, time.time()),
    )
    conn.commit()


def _post_github_comment(
    *, repo_github: str, issue_number: int, body: str,
) -> tuple[bool, str | None]:
    """Single GitHub-coupling sink for the seam.

    Returns ``(ok, error)``.  We never raise — the local DB write is the
    authoritative state record; a comment post failure is surfaced to
    the caller as diagnostics but must not undo the state transition.
    """
    try:
        github_ops.post_issue_comment(repo_github, issue_number, body)
        return True, None
    except Exception as exc:  # noqa: BLE001 — best-effort notification
        return False, str(exc)


# ── Daemon routing (#590) ───────────────────────────────────────────────────
#
# When ``board_service`` is configured (a thin client over Tailscale, per #584),
# the seam's DB writes must land on the daemon's shared DB, not the client's
# local ``coord.db``.  We route the *whole* record to the daemon — it re-invokes
# the ``_local`` implementation against the one shared DB (posting the GitHub
# comment and writing the assignments/notifications rows there).  This is what
# lets a remote interactive session self-report via ``coord report-result``
# instead of the old "do NOT run report-result here" workaround.
#
# ``board_service`` unset → the ``_local`` path runs unchanged (no regression).
# The daemon endpoints call ``_post_*_local`` directly, so a daemon process can
# never recurse back out over HTTP even if it somehow had a service configured.


def _remote_service():  # -> ServiceConfig | None
    """The configured board service, or ``None`` for the local-DB path."""
    from coord.client import resolve_board_service  # noqa: PLC0415

    return resolve_board_service()


def _validate_result(record: ResultRecord) -> None:
    """Reject invalid ``status`` / ``verdict`` before any write or POST."""
    if record.status not in _VALID_STATUSES:
        raise ValueError(
            f"invalid status {record.status!r} (expected one of {_VALID_STATUSES!r})"
        )
    if record.verdict is not None and record.verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"invalid verdict {record.verdict!r} "
            f"(expected one of {_VALID_VERDICTS!r} or None)"
        )
    # ── Keystone invariant (#617): request-changes MUST carry findings ───────
    # A `request-changes` verdict with no body silently strands the review: the
    # iteration-N+1 fix worker is dispatched with nothing to fix, and the #603
    # per-issue context store (which is auto-injected into every future
    # briefing) never learns why the change was rejected (#607).
    #
    # The #580 guard that catches this lives ONLY in the `coord report-result`
    # CLI command — every OTHER caller (the operator-prompt verdict relay, the
    # transcript-floor, any future path) routes around it and can persist a
    # bodyless verdict.  Enforcing it HERE, at the single write seam through
    # which all of them funnel, makes the bad state unrepresentable: a dropped
    # review becomes a loud, recoverable error instead of silent data loss.
    # Callers that can recover (read the transcript, prompt the operator for the
    # body) catch this and retry with the findings attached.
    if record.verdict == "request-changes" and not (
        record.findings_body and record.findings_body.strip()
    ):
        raise ValueError(
            "request-changes verdict requires findings_body — refusing to record "
            "a review with no body, which would strand the fix worker with "
            "nothing to fix (#607). Recover the findings from the session "
            "transcript or supply them with --body-file."
        )


# ── Public surface ──────────────────────────────────────────────────────────


def post_completion(record: CompletionRecord) -> StoreOutcome:
    """Git-floor backstop — routes to the daemon when ``board_service`` is set.

    A daemon round-trip failure must NOT crash the launcher exit path (the
    backstop is best-effort), so a network error degrades to an ``error``
    outcome rather than raising.  ``board_service`` unset → local DB write.
    """
    svc = _remote_service()
    if svc is None:
        return _post_completion_local(record)
    try:
        from coord.client import post_record  # noqa: PLC0415

        return StoreOutcome(**post_record(svc, "/completion", asdict(record)))
    except Exception as exc:  # noqa: BLE001 — backstop must not crash the exit path
        return StoreOutcome(status="error", event="", posted=False, error=str(exc))


def post_result(record: ResultRecord) -> StoreOutcome:
    """Structured report from the interactive agent — routes to the daemon
    when ``board_service`` is set.

    Validation runs client-side first (fast feedback for the operator), then
    the record is POSTed; a daemon failure raises so ``coord report-result``
    exits non-zero and the operator knows the verdict did not land.
    ``board_service`` unset → local DB write (unchanged).
    """
    _validate_result(record)
    svc = _remote_service()
    if svc is None:
        return _post_result_local(record)
    from coord.client import post_record  # noqa: PLC0415

    return StoreOutcome(**post_record(svc, "/result", asdict(record)))


def _post_completion_local(record: CompletionRecord) -> StoreOutcome:
    """Git-floor backstop.

    Resolves the terminal status from ``exit_code`` and ``commits_ahead``
    (no agent self-report is consulted) and writes the completion through
    the seam:

    * ``exit_code != 0``                  → ``failed``
    * ``exit_code == 0``, commits == 0    → ``advisory`` (the #448 state)
    * ``exit_code == 0``, commits >= 1    → ``done``  (eligible for review/smoke)
    * ``exit_code == 0``, commits is None → ``done``  (git failed; do not
      falsely flag advisory — same policy as #448 in agent.py:_reap)

    Always writes a local state transition.  Always attempts to post a
    coordinator-authored comment.  Comment-post failure is non-fatal.
    """
    if record.exit_code != 0:
        return _post_failure_path(record)

    if record.commits_ahead == 0:
        return _post_advisory_path(record)

    # commits_ahead is >=1 or None (unknown).  Treat as DONE so the work
    # is eligible for review/smoke.  Matches #448 policy: a git failure
    # never demotes a clean exit to advisory.
    return _post_done_path(record)


def _post_result_local(record: ResultRecord) -> StoreOutcome:
    """Structured report from the interactive agent (local-DB write).

    Maps the agent-reported ``status`` to the same three terminal states
    the git-floor backstop produces:

    * ``done``                → ``done`` (eligible for review/smoke).  If a
      ``verdict`` was supplied (only meaningful for a review session
      where no commits exist) it is recorded on the assignment row so
      the merge gate (``has_approved_review``) sees the same field a
      claude-p reviewer would have populated.
    * ``blocked``             → ``failed`` (the operator explicitly says
      the work cannot proceed; pipeline treats it the same as a worker
      that exited non-zero).
    * ``already-implemented`` → ``advisory`` (same shape as a 0-commit
      clean exit; not a clean DONE, not a hard FAIL → no auto_reassign
      loop).
    """
    _validate_result(record)

    if record.status == STATUS_BLOCKED:
        # Render as failure on the issue and in the DB.  This keeps the
        # auto-reassign default OFF unless the user explicitly opts in
        # (concurrency.auto_reassign): mirroring how a claude -p worker
        # exit-1 is handled today.
        body = format_failure(
            assignment_id=record.assignment_id,
            machine_name=record.machine_name,
            repo_name=record.repo_name,
            issue_number=record.issue_number,
            exit_code=1,
            duration_seconds=record.duration_seconds,
            log_path=record.log_path,
            error=record.summary or "Operator reported the session as blocked.",
        )
        posted, err = _post_github_comment(
            repo_github=record.repo_github,
            issue_number=record.issue_number,
            body=body,
        )
        _update_local_state(
            assignment_id=record.assignment_id,
            terminal_status="failed",
            branch=record.branch,
            review_state=None,
        )
        _record_notification(
            assignment_id=record.assignment_id,
            event=EVENT_FAILURE,
            branch=record.branch,
        )
        return StoreOutcome(
            status="failed", event=EVENT_FAILURE, posted=posted, error=err,
        )

    if record.status == STATUS_ALREADY_IMPLEMENTED:
        body = format_advisory(
            assignment_id=record.assignment_id,
            machine_name=record.machine_name,
            repo_name=record.repo_name,
            issue_number=record.issue_number,
            duration_seconds=record.duration_seconds,
            log_path=record.log_path,
            reason=record.summary or "Operator reported: already implemented.",
        )
        posted, err = _post_github_comment(
            repo_github=record.repo_github,
            issue_number=record.issue_number,
            body=body,
        )
        _update_local_state(
            assignment_id=record.assignment_id,
            terminal_status="advisory",
            branch=record.branch,
            # Mark review_state=advisory so the reconcile review-dispatch
            # loop skips this entry (mirrors #448's advisory handling).
            review_state="advisory",
        )
        _record_notification(
            assignment_id=record.assignment_id,
            event=EVENT_ADVISORY,
            branch=record.branch,
        )
        return StoreOutcome(
            status="advisory", event=EVENT_ADVISORY, posted=posted, error=err,
        )

    # status == "done"
    summary_lines: list[str] = []
    if record.summary.strip():
        summary_lines.append(record.summary.strip())
    if record.verdict is not None:
        summary_lines.append("")
        summary_lines.append(f"**Verdict:** {record.verdict}")
    body = format_completion(
        assignment_id=record.assignment_id,
        machine_name=record.machine_name,
        repo_name=record.repo_name,
        issue_number=record.issue_number,
        exit_code=0,
        duration_seconds=record.duration_seconds,
        log_path=record.log_path,
        summary="\n".join(summary_lines),
    )
    # Embed the full findings under a parseable marker so a fix worker can
    # recover them from the GitHub message bus on ANY machine (no shared DB).
    if record.findings_body and record.findings_body.strip():
        from coord.comments import format_findings_block  # noqa: PLC0415
        body = body + "\n\n" + format_findings_block(
            record.assignment_id, record.verdict, record.findings_body.strip()
        )
    posted, err = _post_github_comment(
        repo_github=record.repo_github,
        issue_number=record.issue_number,
        body=body,
    )
    # review_state=pending so reconcile picks it up like a claude -p worker.
    _update_local_state(
        assignment_id=record.assignment_id,
        terminal_status="done",
        branch=record.branch,
        review_state="pending",
    )
    _record_notification(
        assignment_id=record.assignment_id,
        event=EVENT_COMPLETION,
        branch=record.branch,
    )
    # When a verdict was supplied (review session — no commits) record it
    # on the assignment row so the merge-gate sees the same field a
    # claude -p reviewer's parsed REVIEW_VERDICT would have set.  When the full
    # findings body was also supplied (--body-file), persist BOTH together via
    # the same JSON column the claude -p path uses, so the fix worker's DB-cache
    # lookup (load_assignment_review_findings) hits on this machine.
    if record.verdict is not None:
        try:
            if record.findings_body and record.findings_body.strip():
                from coord.state import update_assignment_review_findings  # noqa: PLC0415
                update_assignment_review_findings(
                    record.assignment_id,
                    verdict=record.verdict,
                    body=record.findings_body.strip(),
                )
            else:
                from coord.state import get_connection  # noqa: PLC0415
                conn = get_connection()
                conn.execute(
                    "UPDATE assignments SET review_verdict=? WHERE assignment_id=?",
                    (record.verdict, record.assignment_id),
                )
                conn.commit()
        except Exception:  # noqa: BLE001 — best-effort
            pass
    # #603: a request-changes verdict is durable context for EVERY future agent
    # on the issue — record a short note in the per-issue digest (local writer;
    # daemon-side on a thin client, so use the _local variant).
    if record.verdict == VERDICT_REQUEST_CHANGES:
        try:
            from coord.state import _add_issue_context_entry_local  # noqa: PLC0415

            summary = (record.findings_body or record.summary or "").strip()
            if summary:
                if len(summary) > 240:
                    summary = summary[:240].rstrip() + "…"
                _add_issue_context_entry_local(
                    record.repo_name,
                    record.issue_number,
                    f"Review requested changes: {summary}",
                    source="review",
                )
        except Exception:  # noqa: BLE001 — best-effort
            pass
    return StoreOutcome(
        status="done", event=EVENT_COMPLETION, posted=posted, error=err,
    )


# ── private terminal-path helpers for post_completion ──────────────────────


def _post_done_path(record: CompletionRecord) -> StoreOutcome:
    body = format_completion(
        assignment_id=record.assignment_id,
        machine_name=record.machine_name,
        repo_name=record.repo_name,
        issue_number=record.issue_number,
        exit_code=record.exit_code,
        duration_seconds=record.duration_seconds,
        log_path=record.log_path,
        summary=record.summary,
    )
    posted, err = _post_github_comment(
        repo_github=record.repo_github,
        issue_number=record.issue_number,
        body=body,
    )
    _update_local_state(
        assignment_id=record.assignment_id,
        terminal_status="done",
        branch=record.branch,
        review_state="pending",
    )
    _record_notification(
        assignment_id=record.assignment_id,
        event=EVENT_COMPLETION,
        branch=record.branch,
    )
    return StoreOutcome(
        status="done", event=EVENT_COMPLETION, posted=posted, error=err,
    )


def _post_advisory_path(record: CompletionRecord) -> StoreOutcome:
    reason = record.summary or (
        "Interactive session exited cleanly but pushed 0 commits "
        "and produced no structured result via `coord report-result`."
    )
    body = format_advisory(
        assignment_id=record.assignment_id,
        machine_name=record.machine_name,
        repo_name=record.repo_name,
        issue_number=record.issue_number,
        duration_seconds=record.duration_seconds,
        log_path=record.log_path,
        reason=reason,
    )
    posted, err = _post_github_comment(
        repo_github=record.repo_github,
        issue_number=record.issue_number,
        body=body,
    )
    _update_local_state(
        assignment_id=record.assignment_id,
        terminal_status="advisory",
        branch=record.branch,
        review_state="advisory",
    )
    _record_notification(
        assignment_id=record.assignment_id,
        event=EVENT_ADVISORY,
        branch=record.branch,
    )
    return StoreOutcome(
        status="advisory", event=EVENT_ADVISORY, posted=posted, error=err,
    )


def _post_failure_path(record: CompletionRecord) -> StoreOutcome:
    body = format_failure(
        assignment_id=record.assignment_id,
        machine_name=record.machine_name,
        repo_name=record.repo_name,
        issue_number=record.issue_number,
        exit_code=record.exit_code,
        duration_seconds=record.duration_seconds,
        log_path=record.log_path,
        error=record.summary or f"Interactive session exited with status {record.exit_code}.",
    )
    posted, err = _post_github_comment(
        repo_github=record.repo_github,
        issue_number=record.issue_number,
        body=body,
    )
    _update_local_state(
        assignment_id=record.assignment_id,
        terminal_status="failed",
        branch=record.branch,
        review_state=None,
    )
    _record_notification(
        assignment_id=record.assignment_id,
        event=EVENT_FAILURE,
        branch=record.branch,
    )
    return StoreOutcome(
        status="failed", event=EVENT_FAILURE, posted=posted, error=err,
    )
