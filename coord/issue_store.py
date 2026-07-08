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

import json
import time
from dataclasses import asdict, dataclass
from typing import Literal

from coord import github_ops
from coord.comments import (
    EVENT_ADVISORY,
    EVENT_COMPLETION,
    EVENT_FAILURE,
    format_advisory,
    format_audit_scorecard,
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
    "AuditVerdict",
    "STATUS_DONE",
    "STATUS_BLOCKED",
    "STATUS_ALREADY_IMPLEMENTED",
    "VERDICT_APPROVE",
    "VERDICT_REQUEST_CHANGES",
    "get_audit_runs_for_epic",
    "diff_audit_goals",
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

# #886 Phase 2: per-goal verdict for a Milestone Outcome Audit run.
AuditVerdict = Literal["met", "partial", "gap"]
_VALID_AUDIT_VERDICTS = ("met", "partial", "gap")
# Ranking used by diff_audit_goals to classify a goal's movement between runs.
_AUDIT_VERDICT_RANK = {"gap": 0, "partial": 1, "met": 2}


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
    # #886 Phase 2: structured Milestone Outcome Audit verdict — only meaningful
    # for a type="audit" assignment (see #885's --audit-of). One dict per goal:
    # {"goal": str, "metric_before": str, "metric_after": str,
    #  "verdict": "met"|"partial"|"gap", "evidence": str}. When present, the
    # write routes through the audit dual-write path (assignment row + epic
    # comment + #603 context store) instead of the generic done-comment body.
    audit_goals: list[dict] | None = None
    audit_bottom_line: str | None = None


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

    # ── #886 Phase 2: structured audit verdict shape ─────────────────────────
    # A dropped/garbled goal here would corrupt the versioned diff every later
    # `--audit-of` run depends on, so validate the full shape up front rather
    # than discovering a bad goal mid-persist.
    if record.audit_goals is not None:
        if not record.audit_goals:
            raise ValueError(
                "audit_goals must be a non-empty list when supplied — an audit "
                "run reporting zero goals is not a meaningful verdict (#886)"
            )
        for goal in record.audit_goals:
            if not isinstance(goal, dict) or not str(goal.get("goal", "")).strip():
                raise ValueError(
                    f"audit goal missing non-empty 'goal' text: {goal!r}"
                )
            verdict = goal.get("verdict")
            if verdict not in _VALID_AUDIT_VERDICTS:
                raise ValueError(
                    f"invalid audit goal verdict {verdict!r} for goal "
                    f"{goal.get('goal')!r} (expected one of "
                    f"{_VALID_AUDIT_VERDICTS!r})"
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
    import httpx as _httpx  # noqa: PLC0415

    try:
        return StoreOutcome(**post_record(svc, "/result", asdict(record)))
    except _httpx.HTTPStatusError as exc:
        # #676: the daemon's _post_result_local can raise ValueError for
        # guard violations (e.g. chat session attempting to claim done).
        # The daemon serialises ValueError → HTTP 400 {"error": "..."}; convert
        # it back to ValueError here so the CLI's `except ValueError` catches it
        # and shows a clean error instead of a raw HTTPStatusError traceback.
        if exc.response.status_code == 400:
            try:
                detail = exc.response.json().get("error", str(exc))
            except Exception:  # noqa: BLE001
                detail = str(exc)
            raise ValueError(detail) from exc
        if exc.response.status_code == 503:
            # #990: the daemon's _post_result_local raises RuntimeError when a
            # review verdict can't be durably persisted (retries exhausted /
            # readback mismatch); serve_app.py serialises that as HTTP 503
            # {"error": "result write failed", "detail": "..."}. Convert back
            # to RuntimeError so the CLI's `except RuntimeError` shows a clean
            # message instead of a raw HTTPStatusError traceback.
            try:
                payload = exc.response.json()
                detail = payload.get("detail") or payload.get("error") or str(exc)
            except Exception:  # noqa: BLE001
                detail = str(exc)
            raise RuntimeError(detail) from exc
        raise


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

    **Exception — chat / troubleshoot sessions (#676):** these are
    non-mutating diagnostics that never produce committed work, so they are
    *always* recorded as ``advisory`` regardless of exit code.  A non-zero
    exit from a chat session (e.g. the claude process crashed) must not leave
    a red ``failed`` box on the pipeline.

    Always writes a local state transition.  Always attempts to post a
    coordinator-authored comment.  Comment-post failure is non-fatal.
    """
    # #676: chat and troubleshoot sessions are non-mutating diagnostics.
    # Always mark them advisory — never done or failed — so a crash or
    # abnormal close doesn't create a red box that blocks the pipeline.
    import dataclasses as _dc  # noqa: PLC0415

    atype = _assignment_type_local(record.assignment_id)
    if atype in ("chat", "troubleshoot"):
        if not record.summary:
            record = _dc.replace(
                record,
                summary=(
                    f"Human-attended {atype} session closed"
                    " (diagnostic-only — no committed work)."
                ),
            )
        return _post_advisory_path(record)

    # #812: interactive review session that failed to start or exited without a
    # verdict.  Reviews never commit code, so commits_ahead is always None here
    # (no worktree).  The legitimate "done" path for a review is post_result,
    # which is called by coord report-result or the transcript-floor — both run
    # BEFORE post_completion in finalize_interactive_exit and return early.
    # Reaching post_completion for a review means neither path captured a verdict,
    # so the session was abandoned or never started.  Finalise as failed
    # (→ red/recoverable in the TUI) rather than done (→ permanently stuck blue).
    if atype == "review":
        summary = record.summary or (
            "Interactive review session exited without producing a verdict "
            "(session may have failed to start). "
            "Re-dispatch a fresh review via 'Start review'."
        )
        record = _dc.replace(record, summary=summary)
        return _post_failure_path(record)

    if record.exit_code != 0:
        return _post_failure_path(record)

    if record.commits_ahead == 0:
        return _post_advisory_path(record)

    # commits_ahead is >=1 or None (unknown).  Treat as DONE so the work
    # is eligible for review/smoke.  Matches #448 policy: a git failure
    # never demotes a clean exit to advisory.
    return _post_done_path(record)


def _assignment_type_local(assignment_id: str) -> str | None:
    """The board ``type`` ("work"/"review"/"smoke"/…) for *assignment_id* from
    the local DB, or ``None`` when the row is absent or the lookup fails.

    Used by the verdict-target invariant in :func:`_post_result_local`. A lookup
    failure returns ``None`` (don't gate) so a transient DB hiccup never blocks a
    legitimate write.
    """
    from coord.state import get_connection  # noqa: PLC0415

    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT type FROM assignments WHERE assignment_id = ?",
            (assignment_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001 — a lookup failure must not block the write
        return None
    if row is None:
        return None
    return row["type"] if hasattr(row, "keys") else row[0]


def _read_review_verdict_local(assignment_id: str) -> str | None:
    """Read back the persisted ``review_verdict`` column, or ``None`` if the
    row is absent. Used by :func:`_persist_review_verdict` to verify a write
    actually landed rather than trusting a bare ``commit()`` call."""
    from coord.state import get_connection  # noqa: PLC0415

    conn = get_connection()
    row = conn.execute(
        "SELECT review_verdict FROM assignments WHERE assignment_id = ?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        return None
    return row["review_verdict"] if hasattr(row, "keys") else row[0]


def _persist_review_verdict(record: ResultRecord) -> None:
    """Durably record ``record.verdict`` on the assignment row.

    #990: this used to be a bare ``UPDATE ... ; except Exception: pass`` —
    a transient SQLite lock (the daemon DB is concurrently written by other
    ticks/agents) could make the write silently no-op while the caller
    (``coord report-result``) still reported success and posted a GitHub
    comment showing the verdict. The merge gate (``has_approved_review`` in
    ``coord.merge_queue``) reads exactly this column, so a swallowed failure
    here quietly undermines the merge gate's trustworthiness.

    Retries a few times with backoff to absorb transient contention, then
    reads the column back and compares it to what we intended to write —
    catches both a raised exception AND a write that silently no-ops
    (e.g. a stale connection, or a commit that didn't persist). Raises
    ``RuntimeError`` if it still can't confirm the write landed; callers
    MUST NOT swallow this — let it propagate so the CLI exits non-zero and
    the operator knows to retry, instead of trusting a false success.
    """
    attempts = 4
    delay = 0.15
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
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
            actual = _read_review_verdict_local(record.assignment_id)
            if actual == record.verdict:
                return
            last_exc = RuntimeError(
                f"review_verdict readback mismatch for assignment "
                f"{record.assignment_id!r}: wrote {record.verdict!r}, read back "
                f"{actual!r} (attempt {attempt}/{attempts})"
            )
        except Exception as exc:  # noqa: BLE001 — retried below; re-raised after
            last_exc = exc
        if attempt < attempts:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(
        f"failed to durably persist review_verdict={record.verdict!r} for "
        f"assignment {record.assignment_id!r} after {attempts} attempts "
        f"(#990): {last_exc}"
    ) from last_exc


# ── Milestone Outcome Audit — versioned runs + diff (#886 Phase 2) ─────────


def get_audit_runs_for_epic(repo_name: str, epic_issue_number: int) -> list[dict]:
    """All ``type="audit"`` assignment rows for ``(repo_name, epic_issue_number)``
    that have a persisted verdict, oldest run first.

    The epic's own issue number doubles as the audit assignment's
    ``issue_number`` (see #885's ``_dispatch_audit_of``), so a single
    ``(repo_name, issue_number)`` pair identifies every ``--audit-of`` run ever
    made against that milestone. Used both to compute the next
    ``audit_run_number`` (``len(...) + 1``) and to diff the newest run against
    the previous one. Returns ``[]`` on any lookup failure — a transient DB
    hiccup here must not crash the reporting path (the caller falls back to
    treating this as the first run, which just skips the diff).
    """
    from coord.state import get_connection  # noqa: PLC0415

    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT assignment_id, audit_run_number, audit_goals_json, "
            "audit_bottom_line, dispatched_at FROM assignments "
            "WHERE repo_name=? AND issue_number=? AND type='audit' "
            "AND audit_run_number IS NOT NULL ORDER BY audit_run_number ASC",
            (repo_name, epic_issue_number),
        ).fetchall()
    except Exception:  # noqa: BLE001 — best-effort; treat as "no prior runs"
        return []
    return [dict(r) for r in rows]


def diff_audit_goals(
    prev_goals: list[dict] | None, new_goals: list[dict]
) -> dict[str, list[str]]:
    """Classify how each goal in ``new_goals`` moved relative to ``prev_goals``
    (keyed by the ``goal`` text — the only stable identifier an agent-authored
    scorecard has across runs).

    Returns ``{"closed": [...], "regressed": [...], "still_open": [...],
    "new": [...]}`` — the concrete "v1: 3 gaps -> v2: 0 gaps" delta the issue
    asks for. ``closed`` = moved to ``met`` from something else; ``regressed``
    = moved to a lower rank (e.g. ``met`` -> ``gap``, a real regression worth
    flagging loudly); ``still_open`` = present in both runs, still not
    ``met``; ``new`` = a goal that didn't appear in the prior run at all
    (scope changed, or first time this goal was tracked).
    """
    prev_by_goal = {g.get("goal"): g.get("verdict") for g in (prev_goals or [])}
    closed: list[str] = []
    regressed: list[str] = []
    still_open: list[str] = []
    new: list[str] = []
    for goal in new_goals:
        name = goal.get("goal")
        verdict = goal.get("verdict")
        if name not in prev_by_goal:
            new.append(name)
            continue
        prev_verdict = prev_by_goal[name]
        prev_rank = _AUDIT_VERDICT_RANK.get(prev_verdict, 0)
        new_rank = _AUDIT_VERDICT_RANK.get(verdict, 0)
        if new_rank == _AUDIT_VERDICT_RANK["met"] and prev_rank != new_rank:
            closed.append(name)
        elif new_rank < prev_rank:
            regressed.append(name)
        elif new_rank != _AUDIT_VERDICT_RANK["met"]:
            still_open.append(name)
    return {
        "closed": closed,
        "regressed": regressed,
        "still_open": still_open,
        "new": new,
    }


def _read_audit_run_local(assignment_id: str) -> int | None:
    """Read back the persisted ``audit_run_number`` column, or ``None`` if the
    row is absent. Used by :func:`_persist_audit_result` to verify a write
    actually landed rather than trusting a bare ``commit()`` call."""
    from coord.state import get_connection  # noqa: PLC0415

    conn = get_connection()
    row = conn.execute(
        "SELECT audit_run_number FROM assignments WHERE assignment_id = ?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        return None
    return row["audit_run_number"] if hasattr(row, "keys") else row[0]


def _persist_audit_result(record: ResultRecord, *, run_number: int) -> None:
    """Durably record the structured audit verdict on the assignment row.

    Mirrors :func:`_persist_review_verdict` (#990): retries a few times with
    backoff, then reads the ``audit_run_number`` column back and compares it
    to what was intended — a silently-dropped write here would corrupt the
    versioning invariant every later ``--audit-of`` diff depends on (two runs
    could collide on the same ``run_number``, or a run could vanish from the
    history entirely). Raises ``RuntimeError`` if the write can't be
    confirmed; callers MUST NOT swallow this.
    """
    goals_json = json.dumps(record.audit_goals)
    attempts = 4
    delay = 0.15
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            from coord.state import get_connection  # noqa: PLC0415

            conn = get_connection()
            conn.execute(
                "UPDATE assignments SET audit_goals_json=?, audit_bottom_line=?, "
                "audit_run_number=? WHERE assignment_id=?",
                (
                    goals_json,
                    record.audit_bottom_line,
                    run_number,
                    record.assignment_id,
                ),
            )
            conn.commit()
            actual = _read_audit_run_local(record.assignment_id)
            if actual == run_number:
                return
            last_exc = RuntimeError(
                f"audit_run_number readback mismatch for assignment "
                f"{record.assignment_id!r}: wrote {run_number!r}, read back "
                f"{actual!r} (attempt {attempt}/{attempts})"
            )
        except Exception as exc:  # noqa: BLE001 — retried below; re-raised after
            last_exc = exc
        if attempt < attempts:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(
        f"failed to durably persist audit run {run_number} for assignment "
        f"{record.assignment_id!r} after {attempts} attempts (#886): {last_exc}"
    ) from last_exc


def _post_audit_result_path(record: ResultRecord) -> StoreOutcome:
    """Milestone Outcome Audit (#886 Phase 2) dual-write path.

    Reached from :func:`_post_result_local` when ``record.audit_goals`` is
    supplied (i.e. ``coord report-result --audit-json`` was used). Writes the
    structured verdict three ways for durability, exactly as the issue asks:

    1. the assignment row (``audit_goals_json``/``audit_bottom_line``/
       ``audit_run_number`` — see :func:`_persist_audit_result`);
    2. a comment on the epic issue carrying the rendered scorecard, the delta
       vs the prior run, and the raw JSON (:func:`coord.comments.
       format_audit_scorecard`) so any machine can recover the full verdict
       from the GitHub message bus alone, same as the review-findings block;
    3. the #603 per-issue context store, so the next ``--audit-of`` run (and
       every other future agent on this epic) sees a durable one-line note
       without re-fetching/re-parsing the GitHub comment.
    """
    prior_runs = get_audit_runs_for_epic(record.repo_name, record.issue_number)
    run_number = len(prior_runs) + 1
    prev_goals: list[dict] | None = None
    if prior_runs and prior_runs[-1].get("audit_goals_json"):
        try:
            prev_goals = json.loads(prior_runs[-1]["audit_goals_json"])
        except (TypeError, ValueError):
            prev_goals = None
    diff = diff_audit_goals(prev_goals, record.audit_goals) if prev_goals is not None else None

    _persist_audit_result(record, run_number=run_number)

    bottom_line = (record.audit_bottom_line or record.summary or "").strip()
    scorecard_body = format_audit_scorecard(
        assignment_id=record.assignment_id,
        run_number=run_number,
        bottom_line=bottom_line,
        goals=record.audit_goals,
        diff=diff,
    )
    completion_body = format_completion(
        assignment_id=record.assignment_id,
        machine_name=record.machine_name,
        repo_name=record.repo_name,
        issue_number=record.issue_number,
        exit_code=0,
        duration_seconds=record.duration_seconds,
        log_path=record.log_path,
        summary=record.summary or bottom_line,
    )
    posted, err = _post_github_comment(
        repo_github=record.repo_github,
        issue_number=record.issue_number,
        body=completion_body + "\n\n" + scorecard_body,
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
    # #603: durable one-line finding for every future agent on this epic —
    # the "re-ask the question" payoff without re-parsing the GitHub comment.
    try:
        from coord.state import _add_issue_context_entry_local  # noqa: PLC0415

        total = len(record.audit_goals)
        met = sum(1 for g in record.audit_goals if g.get("verdict") == "met")
        gap = sum(1 for g in record.audit_goals if g.get("verdict") == "gap")
        partial = total - met - gap
        note = f"Audit v{run_number}: {met}/{total} goals met"
        if partial:
            note += f", {partial} partial"
        if gap:
            note += f", {gap} gap"
        if diff:
            if diff.get("closed"):
                note += f" — closed: {', '.join(diff['closed'])}"
            if diff.get("still_open"):
                note += f"; still open: {', '.join(diff['still_open'])}"
            if diff.get("regressed"):
                note += f"; REGRESSED: {', '.join(diff['regressed'])}"
        _add_issue_context_entry_local(
            record.repo_name, record.issue_number, note, source="audit",
        )
    except Exception:  # noqa: BLE001 — best-effort; never blocks the write
        pass
    return StoreOutcome(
        status="done", event=EVENT_COMPLETION, posted=posted, error=err,
    )


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

    # Invariant: a review verdict belongs ONLY on a type="review" assignment.
    # A `report-result --verdict` misrouted onto a work/plan/smoke id would mark
    # that row done AND stamp a bogus review_verdict — exactly what silently
    # finalized a still-live interactive WORK session (#646: a claude-pty work
    # row ended up status=done + review_verdict=approve with no review row in
    # sight, which hid the TUI reattach option). Refuse the misrouted write at
    # this single seam so the bad state is unrepresentable and the caller learns
    # it targeted the wrong id. Only gate when the type is KNOWN and not
    # "review" — an unknown id (row not yet visible) falls through to the
    # existing no-op UPDATE rather than erroring on a benign race.
    if record.verdict is not None:
        atype = _assignment_type_local(record.assignment_id)
        if atype is not None and atype != "review":
            raise ValueError(
                f"refusing to record a review verdict on assignment "
                f"{record.assignment_id!r}: it is type={atype!r}, not 'review'. "
                "A verdict belongs on a review assignment — re-run "
                "`coord report-result` with the review id. A verdict on a "
                "non-review row marks it done and stamps a bogus review_verdict "
                "(the #646 premature-finalize of a live interactive session)."
            )

    # #886 Phase 2: same misrouting invariant as the review-verdict gate above,
    # but for the structured audit verdict — it belongs ONLY on a type="audit"
    # assignment (see #885's --audit-of). Only gate when the type is KNOWN.
    if record.audit_goals is not None:
        atype = _assignment_type_local(record.assignment_id)
        if atype is not None and atype != "audit":
            raise ValueError(
                f"refusing to record a structured audit verdict on assignment "
                f"{record.assignment_id!r}: it is type={atype!r}, not 'audit'. "
                "--audit-json belongs on a --audit-of assignment (#886) — "
                "re-run `coord report-result` with the audit id."
            )

    # #676: chat and troubleshoot sessions are non-mutating diagnostics — they
    # never produce committed work and therefore must never claim `done` or
    # `blocked` (both map to a terminal state that can advance or stall the
    # pipeline).  A chat session claiming `done` without committed work is a
    # false success that masks the real problem (#676 root-mechanism comment).
    # `already-implemented` → `advisory` is the one neutral signal allowed,
    # because it expresses "no work was needed" without a false done/fail.
    # Only gate when the type is KNOWN — an unknown row falls through so a
    # transient DB lookup failure never blocks a legitimate write.
    if record.status in (STATUS_DONE, STATUS_BLOCKED):
        atype = _assignment_type_local(record.assignment_id)
        if atype in ("chat", "troubleshoot"):
            raise ValueError(
                f"refusing to record status={record.status!r} on assignment "
                f"{record.assignment_id!r}: it is type={atype!r}, a non-mutating "
                "diagnostic session. A chat/troubleshoot session cannot claim "
                "'done' or 'blocked' without committed+pushed work — use "
                "`coord assign --work` to dispatch actual work (#676)."
            )

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
    # #886 Phase 2: a structured audit verdict routes through its own
    # dual-write path (assignment row + epic comment + #603 context store)
    # instead of the generic done-comment body below.
    if record.audit_goals is not None:
        return _post_audit_result_path(record)

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
        _persist_review_verdict(record)
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
