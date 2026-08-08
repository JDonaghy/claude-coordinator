"""Poll agent servers and post completion/failure comments to GitHub."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from coord.models import Assignment, Board

log = logging.getLogger(__name__)

# Cache: machine_name → host. Populated by `run(config)` so post_transition →
# _try_parse_and_post_review can fetch a remote agent's log via /logs/<id>
# without threading the Config through every helper.
_AGENT_HOSTS: dict[str, str] = {}


def _agent_host(machine_name: str) -> str | None:
    return _AGENT_HOSTS.get(machine_name)

from coord import github_ops
from coord.comments import (
    EVENT_ADVISORY,
    EVENT_COMPLETION,
    EVENT_FAILURE,
    EVENT_NEEDS_ATTENTION,
    EVENT_PLAN,
    EVENT_STALLED,
    EVENT_STUCK,
    format_needs_attention,
    format_plan,
    format_stalled_pipeline,
    format_stalled_pipeline_dispatch,
    format_stuck,
)
from coord.config import Config
from coord.dispatch import AGENT_PORT, post_advisory, post_completion, post_failure
from coord.progress import parse_progress
from coord.state import (
    load_dispatched,
    load_done_reviews_needing_post,
    load_notified,
    mark_notified,
    mark_review_posted,
    save_plan,
)

# #1710 inventory: kept as a direct import — `is_usage_limit_reason` is a
# trivial string-prefix predicate over `Assignment.failure_reason` (a
# coordinator-authored value stamped by `format_usage_limit_reason`, itself
# only ever produced by the reap path's claude-specific kill detection), not
# a per-provider log-format parse. Any provider's `failure_reason` would be
# checked the same way.
from coord.worker_events import is_usage_limit_reason


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


@dataclass
class NeedsAttentionDetection:
    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    reason: str  # "wall_clock" | "non_convergence"
    detail: str


def _needs_attention_notified_key(assignment_id: str) -> str:
    """Notified ledger key for needs-attention events (#846).

    Composite key (mirrors :func:`_stuck_notified_key`) so a one-shot
    needs-attention comment does not block later completion/failure/stuck
    notifications, and vice versa.
    """
    return f"{assignment_id}:needs-attention"


@dataclass
class StalledDetection:
    """#1441: a pipeline row whose auto-loop transition already fired once
    but which is stuck on a precondition that landed too late for that
    one-shot reaction to see. See :func:`detect_stalled_pipeline`."""

    assignment_id: str
    machine_name: str
    repo_name: str
    issue_number: int
    reason: str  # "review_request_changes_no_fix" | "review_done_no_verdict" |
    # "done_no_review" | "approved_not_queued" | "merge_conflict_unresolved"
    # (#1478, #1582) | "review_failed_no_verdict" (#1584)
    detail: str


def _stalled_notified_key(assignment_id: str) -> str:
    """Notified ledger key for stalled-pipeline events (#1441).

    Composite key (mirrors :func:`_needs_attention_notified_key`) so a
    one-shot stalled-pipeline comment does not block later completion/
    failure/stuck/needs-attention notifications for the same assignment_id,
    and vice versa.
    """
    return f"{assignment_id}:stalled"


def _fmt_minutes(seconds: float) -> str:
    minutes = seconds / 60.0
    if minutes < 1:
        return f"{seconds:.0f}s"
    if minutes == int(minutes):
        return f"{int(minutes)}m"
    return f"{minutes:.1f}m"


def attention_signal(
    *,
    assignment_type: str,
    status: str | None,
    dispatched_at: float | None,
    review_iteration: int,
    config: Config,
    now: float | None = None,
    provider_name: str | None = None,
    review_of_assignment_id: str | None = None,
) -> tuple[str, str] | tuple[None, None]:
    """Pure #846 detection core: the two "needs attention" signals, decoupled
    from where the assignment's fields come from.

    1. **Non-convergence**: ``review_iteration >= config.pipeline.
       convergence_rounds`` fix/review rounds without reaching a terminal
       green test verdict + approved review. Checked first — a thrashing
       assignment is worth flagging even if it hasn't yet cleared the
       wall-clock threshold.
    2. **Wall-clock**: running longer than
       ``config.pipeline.attention_threshold_for(assignment_type,
       provider_name=..., review_of_assignment_id=...)``, computed from
       *dispatched_at*. ``provider_name``/``review_of_assignment_id``
       (#1137) let an interactive ``--fix-of``/``--rework-of`` session be
       recognized despite sharing ``type="work"`` with headless coding
       workers — see :meth:`Config.pipeline.attention_threshold_for`'s
       docstring. Both default to ``None`` (no effect) for callers that
       don't have the full assignment record.

    Deliberately time/round-based rather than self-report-based (#448: the
    failure mode that motivated this was a worker that never emitted a
    ``STUCK:`` line — it just silently burned budget while looking
    "productive").

    Shared by :func:`detect_needs_attention` (the coordinator backstop,
    dispatch-ledger-dict based), ``coord.pipeline.compute_pipeline`` (the
    ``/api/pipeline`` field the web dashboard renders), and the dashboard's
    background poller (``Assignment``-object based) — one signal, several
    call sites, instead of three copies of the same threshold logic.

    Returns ``(reason, detail)`` — ``reason`` is ``"wall_clock"`` or
    ``"non_convergence"`` — or ``(None, None)`` when nothing is flagged.
    """
    if (status or "").lower() != "running":
        return None, None
    if now is None:
        now = time.time()

    if review_iteration >= config.pipeline.convergence_rounds:
        return "non_convergence", (
            f"{review_iteration} fix/review round(s) on this assignment "
            f"without reaching a green test verdict + approved review "
            f"(threshold: {config.pipeline.convergence_rounds})."
        )

    threshold = config.pipeline.attention_threshold_for(
        assignment_type,
        provider_name=provider_name,
        review_of_assignment_id=review_of_assignment_id,
    )
    if dispatched_at is not None:
        running_for = now - dispatched_at
        if running_for > threshold:
            return "wall_clock", (
                f"Running {_fmt_minutes(running_for)}, past the "
                f"{_fmt_minutes(threshold)} threshold for "
                f"type={assignment_type!r}."
            )

    return None, None


def detect_needs_attention(
    config: Config, *, now: float | None = None
) -> list[tuple[NeedsAttentionDetection, dict]]:
    """Scan dispatched assignments for the two #846 "needs attention" signals
    (see :func:`attention_signal`). Detection only — no dispatch/kill/handoff
    behaviour.

    Returns ``(NeedsAttentionDetection, dispatch_record)`` pairs for
    assignments that haven't already been notified as needing attention (or
    reached a terminal notification), mirroring :func:`detect_stuck`'s shape
    so callers can post + mark idempotently the same way.
    """
    dispatched = load_dispatched()
    if not dispatched:
        return []
    notified = load_notified()

    active_records = [
        r for r in dispatched
        if r["assignment_id"] not in notified
        and _needs_attention_notified_key(r["assignment_id"]) not in notified
    ]
    if not active_records:
        return []

    results: list[tuple[NeedsAttentionDetection, dict]] = []
    for record in active_records:
        reason, detail = attention_signal(
            assignment_type=record.get("type") or "work",
            status=record.get("status"),
            dispatched_at=record.get("dispatched_at"),
            review_iteration=record.get("review_iteration") or 0,
            config=config,
            now=now,
            provider_name=record.get("provider_name"),
            review_of_assignment_id=record.get("review_of_assignment_id"),
        )
        if reason is None:
            continue
        results.append((
            NeedsAttentionDetection(
                assignment_id=record["assignment_id"],
                machine_name=record["machine_name"],
                repo_name=record["repo_name"],
                issue_number=record["issue_number"],
                reason=reason,
                detail=detail,
            ),
            record,
        ))

    return results


def post_needs_attention(detection: NeedsAttentionDetection, record: dict) -> None:
    """Post a needs-attention comment to GitHub and mark notified (#846)."""
    body = format_needs_attention(
        assignment_id=detection.assignment_id,
        machine_name=detection.machine_name,
        repo_name=detection.repo_name,
        issue_number=detection.issue_number,
        reason=detection.reason,
        detail=detection.detail,
    )
    github_ops.post_issue_comment(
        record["repo_github"], detection.issue_number, body
    )
    mark_notified(_needs_attention_notified_key(detection.assignment_id), EVENT_NEEDS_ATTENTION)


# ── Stalled-pipeline sweeper (#1441) ────────────────────────────────────────
#
# The auto-loop (coord.auto_loop) only reacts to review/fix TRANSITIONS — the
# instant `coord notify` sees a review or fix flip to `done` during THAT
# pass. Once the transition is consumed nothing ever re-examines the row, so
# a precondition that lands late (a Test verdict backfilled two days after
# the review completed — vimcode #602) leaves it stranded: looks complete on
# the board, isn't. This sweeper re-scans every *done* work chain on the
# board each notify pass and flags the ones stuck on an unmet precondition
# a fresh transition would have already resolved. Detection only — no
# dispatch, mirroring detect_needs_attention's contract.


def _pipeline_heads(board: "Board") -> list["Assignment"]:
    """Return the most-recent WORK_LIKE_TYPES assignment per (repo, issue).

    A row can be bounced through 1+ auto-loop fix iterations, each a
    separate ``Assignment`` sharing the same ``(repo_name, issue_number)``.
    Only the most recent one reflects the pipeline's actual current
    position — earlier rows in the chain are superseded, and evaluating them
    too would re-flag a condition a later fix already addressed.
    """
    from coord.models import WORK_LIKE_TYPES  # noqa: PLC0415

    all_assignments = list(board.active) + list(board.completed)
    heads: dict[tuple[str, int], "Assignment"] = {}
    for a in all_assignments:
        if a.type not in WORK_LIKE_TYPES:
            continue
        key = (a.repo_name, a.issue_number)
        ts = a.dispatched_at or a.finished_at or 0.0
        cur = heads.get(key)
        cur_ts = (cur.dispatched_at or cur.finished_at or 0.0) if cur is not None else -1.0
        if cur is None or ts >= cur_ts:
            heads[key] = a
    return list(heads.values())


def detect_stalled_pipeline(
    config: Config,
    *,
    board: "Board | None" = None,
    merge_queue_items: list | None = None,
    terminal_cache: dict | None = None,
) -> list[tuple[StalledDetection, "Assignment"]]:
    """Scan the board for *done* work chains stuck on an unmet precondition
    that a fresh review/fix transition would already have resolved (#1441).

    Five candidate stall states, checked per pipeline "head" (the most
    recent work-like assignment for a given (repo, issue) — see
    :func:`_pipeline_heads`):

    1. ``review_request_changes_no_fix`` — the head's linked review
       completed with verdict ``request-changes`` and no fix assignment was
       ever dispatched in response (the vimcode #602 reference case: the
       review's transition fired and was consumed while some other
       precondition was outstanding, and nothing has re-examined it since).
    2. ``review_done_no_verdict`` (#1582) — the head's linked review is
       ``status="done"`` but ``review_verdict IS NULL``: the reviewing
       session finalised without ever capturing a verdict (#812 — a session
       that failed to start, or exited before ``coord report-result``/the
       transcript-floor ran; elitebook's documented ~14% review-verdict
       drop rate, #873). This matches NONE of the other three arms — it
       isn't ``request-changes`` (no verdict at all), a review WAS
       dispatched (so not ``done_no_review``), and there is no approval (so
       not ``approved_not_queued``) — so before this arm existed it fell
       through every check and parked the drive forever (#1582's own
       observed case, #1563).
    3. ``done_no_review`` — the head carries a terminal Test verdict
       (``passed``/``skipped``), the "review" gate is required, the
       completion is not an interactive (``provider_name="claude-pty"``)
       session (interactive completions are deliberately excluded from
       automatic review dispatch — #555), and yet no review assignment was
       ever dispatched for it.
    4. ``approved_not_queued`` — the head satisfies every merge gate
       (:func:`coord.merge_queue.passes_merge_gates` — reused rather than
       re-derived, per #1441's own request) but has no merge-queue entry.
    5. ``merge_conflict_unresolved`` (#1478) — the head already HAS a
       merge-queue entry, but that entry is parked ``CONFLICT`` with an
       error :func:`coord.merge_queue.classify_conflict` calls
       ``"rebaseable"`` and no conflict-fix attempt is active or already
       failed (:func:`coord.conflict_fix.has_prior_conflict_fix`). This is
       exactly the gap :mod:`coord.commands.merge`'s
       ``_dispatch_conflict_fixes`` docstring calls out for the ``--only``
       path pre-#1474 — a bare ``CONFLICT`` row that never got a second
       classify-and-dispatch pass, except here for *any* path (not only
       ``--only``): a ``coord merge`` invocation that dispatched a
       conflict-fix which then failed to actually attempt (no idle
       machine) leaves the entry parked with nothing watching it.
    5. ``review_failed_no_verdict`` (#1584) — the head's linked review
       WORKER died (transient API error, network drop, ...) before ever
       producing a verdict — ``status="failed"`` with no
       ``review_verdict``. Before #1584 this could not happen (a dying
       review was mislabelled ``done``, silently masquerading as a real
       completion); now that it is correctly ``failed``, it needs its own
       arm here so it is not silently skipped (``reason`` staying ``None``)
       the way an unrecognized status would be.

    Every candidate is checked against the shared #522 terminal-state guard
    (:func:`coord.github_ops.work_is_terminal`, via *terminal_cache* — the
    same cache :func:`coord.notify.run` threads through the review/fix
    auto-loop calls) so a closed issue or merged PR never surfaces, and
    against the ``notified`` ledger (composite key, :func:`_stalled_notified_key`)
    so a flagged row is not re-flagged every pass.

    Detection only, mirroring :func:`detect_needs_attention`'s contract — no
    dispatch, no kill, no handoff (that lives in
    :func:`dispatch_stalled_pipeline_action`, #1478, gated behind
    ``config.pipeline.auto_dispatch_stalled``). *board* / *merge_queue_items*
    / *terminal_cache* are all optional so callers (tests, or a future
    ``reconcile()`` caller) can supply their own instead of hitting the
    board service / DB / GitHub.
    """
    # `github_ops` is already imported at module level (used by every other
    # post_* helper in this file) — no local re-import here, so a caller
    # that mocks `coord.notify.github_ops.post_issue_comment` for the
    # posting side doesn't also have to reason about a separately-imported
    # local name for the terminal-state check below.
    from coord.auto_loop import FIX_DISPATCH_TYPES  # noqa: PLC0415
    from coord.conflict_fix import has_prior_conflict_fix  # noqa: PLC0415
    from coord.merge_queue import (  # noqa: PLC0415
        CONFLICT,
        classify_conflict,
        load_queue,
        passes_merge_gates,
    )

    if board is None:
        from coord.board_service import read_board  # noqa: PLC0415
        board = read_board()
    if merge_queue_items is None:
        merge_queue_items = load_queue()
    if terminal_cache is None:
        terminal_cache = {}

    notified = load_notified()
    all_assignments = list(board.active) + list(board.completed)

    results: list[tuple[StalledDetection, "Assignment"]] = []
    for work in _pipeline_heads(board):
        if work.status != "done" or not work.assignment_id:
            continue
        if _stalled_notified_key(work.assignment_id) in notified:
            continue

        repo = config.repo(work.repo_name)
        repo_github = repo.github if repo is not None else None
        if repo_github and github_ops.work_is_terminal(
            repo_github, work.issue_number, work.branch, cache=terminal_cache
        ):
            continue

        required_gates = work.required_gates or list(config.pipeline.default_gates)

        review = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == work.assignment_id and a.type == "review"
            ),
            None,
        )

        # #1566: a review that just finished lands on status="finalizing"
        # (not "done") until `coord notify`'s own _try_parse_and_post_review
        # promotes it — i.e. THIS function is what closes that window. None
        # of the `review.status == "done"` checks below match "finalizing",
        # so a still-finalizing review falls through this whole if/elif
        # chain with `reason` left unset (no stall reported), which is
        # correct as long as the finalizing window stays short. That relies
        # on `coord notify` actually running again soon — nothing here
        # guards against `coord notify` itself never running (e.g. daemon
        # down), which would leave the row on "finalizing" forever without
        # ever tripping this stall detector.
        reason: str | None = None
        detail = ""

        if (
            review is not None
            and review.status == "done"
            and review.review_verdict == "request-changes"
        ):
            fix = next(
                (
                    a for a in all_assignments
                    if a.review_of_assignment_id == work.assignment_id
                    and a.type in FIX_DISPATCH_TYPES
                ),
                None,
            )
            if fix is None:
                reason = "review_request_changes_no_fix"
                detail = (
                    f"Review {review.assignment_id} completed with "
                    "request-changes and no fix worker was ever dispatched "
                    "for it."
                )
        elif (
            review is not None
            and review.status == "done"
            and review.review_verdict is None
        ):
            # #1582: a review that finalised `done` with NO verdict ever
            # captured. Checked BEFORE the `review is None or review.status
            # == "done"` catch-all below — that branch's merge-gate check
            # (`passes_merge_gates`) never fires for a `None` verdict (no
            # approval), so this row would otherwise fall all the way
            # through with `reason` left unset.
            reason = "review_done_no_verdict"
            detail = (
                f"Review {review.assignment_id} finalised as done but no "
                "verdict was ever captured — the session likely failed to "
                "start or exited before recording one (#812)."
            )
        elif (
            review is None
            and "review" in required_gates
            and work.provider_name != "claude-pty"
            and work.test_state in ("passed", "skipped")
        ):
            reason = "done_no_review"
            detail = (
                f"Work is done with test_state={work.test_state!r} but no "
                "review assignment was ever dispatched for it."
            )
        elif review is not None and review.status == "failed":
            # #1584: the review worker died (transient API error, network
            # drop, ...) before producing a verdict. Checked before the
            # `review is None or review.status == "done"` catch-all below so
            # a failed review is never mistaken for "no review dispatched"
            # or "review approved" — neither of which is true here.
            #
            # ...UNLESS it was killed by the account's usage limit. That is
            # an account-wide exhausted budget, not a per-review defect:
            # `AgentServer._reap` lands a usage-limit kill on FAILED exactly
            # like an api_error kill, so without this guard the sweep would
            # spend this work row's ONE auto-recovery action (the
            # `_stalled_notified_key` ledger is one-shot per work row) on a
            # `dispatch_review` that is guaranteed to die the same way until
            # the reset — the precise anti-pattern `reconcile.py`'s
            # `auto_reassign` block was hardened against in #1461, and the
            # one `coord/drive.py`'s `_decide_review` already guards with
            # this same predicate. Skipped at CLASSIFICATION rather than
            # declined at dispatch so the row is never marked notified: a
            # later review attempt that fails for a *different* (genuinely
            # recoverable) reason can still be picked up by a future tick.
            if is_usage_limit_reason(review.failure_reason):
                continue
            reason = "review_failed_no_verdict"
            detail = (
                f"Review {review.assignment_id} failed "
                f"({review.failure_reason or 'no reason recorded'}) before "
                "producing a verdict, and no retry was dispatched."
            )
        elif review is None or review.status == "done":
            # Either the review gate doesn't apply, or a review already
            # completed without leaving a request-changes verdict blocking
            # it (approved, or advanced past advisory-only nits) — the only
            # remaining question is whether it made it into the merge queue,
            # and if it did, whether that entry is stuck.
            matching_entry = next(
                (m for m in merge_queue_items if m.assignment_id == work.assignment_id),
                None,
            )
            if matching_entry is None:
                if passes_merge_gates(work, config, board):
                    reason = "approved_not_queued"
                    detail = (
                        "Work passes every merge gate (review + test) but has "
                        "no merge-queue entry."
                    )
            elif (
                matching_entry.state == CONFLICT
                and classify_conflict(matching_entry.error) == "rebaseable"
                and not has_prior_conflict_fix(board, matching_entry.assignment_id)
            ):
                # #1478: a rebaseable CONFLICT with no active/failed
                # conflict-fix attempt — the #1474 classify-and-dispatch step
                # never got (or never got a second) chance at this entry.
                reason = "merge_conflict_unresolved"
                detail = (
                    f"Merge queue entry for branch {matching_entry.branch!r} is "
                    f"stuck in CONFLICT ({matching_entry.error or 'no error recorded'}) "
                    "with no active or previously-failed conflict-fix attempt."
                )

        if reason is None:
            continue

        results.append((
            StalledDetection(
                assignment_id=work.assignment_id,
                machine_name=work.machine_name,
                repo_name=work.repo_name,
                issue_number=work.issue_number,
                reason=reason,
                detail=detail,
            ),
            work,
        ))

    return results


def post_stalled_pipeline(detection: StalledDetection, config: Config) -> None:
    """Post a stalled-pipeline comment to GitHub and mark notified (#1441)."""
    repo = config.repo(detection.repo_name)
    repo_github = repo.github if repo is not None else None
    if not repo_github:
        return
    body = format_stalled_pipeline(
        assignment_id=detection.assignment_id,
        machine_name=detection.machine_name,
        repo_name=detection.repo_name,
        issue_number=detection.issue_number,
        reason=detection.reason,
        detail=detection.detail,
    )
    github_ops.post_issue_comment(repo_github, detection.issue_number, body)
    mark_notified(_stalled_notified_key(detection.assignment_id), EVENT_STALLED)


# ── #1478: dispatch arm ──────────────────────────────────────────────────────


@dataclass
class StalledDispatchAction:
    """The outcome of :func:`dispatch_stalled_pipeline_action` for one
    :class:`StalledDetection`."""

    kind: str
    """One of:
    - ``"fix_dispatch_attempted"`` — re-ran the review-completion transition
      (:func:`coord.auto_loop.process_review_completion`) for
      ``review_request_changes_no_fix`` (or, #1582, for a
      ``review_done_no_verdict`` whose verdict was just recovered from the
      transcript) and it dispatched a fix worker; see *detail* for what it
      did.
    - ``"review_transition_applied"`` — re-ran
      :func:`coord.auto_loop.process_review_completion` for
      ``review_request_changes_no_fix`` (or a transcript-recovered
      ``review_done_no_verdict``, #1582) and it resolved as ``approved``,
      ``approved_with_nits`` (the #476 advisory-only gate), or
      ``terminal_skip`` — no fix worker was dispatched, but the call still
      mutated *board* in place (``review.review_verdict``,
      ``work.review_state = "done"``, a merge-queue ``refresh_entry_assignment``)
      per that function's own "the caller is responsible for persisting the
      board after this returns" contract. Must be persisted exactly like a
      real dispatch even though no agent was launched.
    - ``"review_verdict_recovered"`` — ``review_done_no_verdict``: a verdict
      was recovered from the reviewing session's own transcript (#617's
      ``_review_findings_from_transcript``, the same recovery
      ``coord diagnose --stage review`` runs) and durably persisted, but
      ``process_review_completion`` made no further board mutation from it
      (e.g. ``pipeline.auto_loop`` is off). See *detail* for the recovered
      verdict.
    - ``"review_reset_redispatched"`` — ``review_done_no_verdict``: nothing
      was recoverable from the transcript, so the review stage was reset
      (the review rows deleted, ``work.review_state`` cleared — #1180's
      ``_reset_review_stage``, branch/commits always kept) and a fresh
      review dispatched for the same work.
    - ``"review_dispatched"``       — a review was dispatched for
      ``done_no_review``.
    - ``"enqueued"``                — the work was enqueued for merge for
      ``approved_not_queued`` (including when a *different* row's
      ``enqueue_approved_work`` call already enqueued this one earlier in
      the same sweep tick — see the queue-membership check below).
    - ``"conflict_fix_dispatched"`` — a conflict-fix worker was dispatched
      for ``merge_conflict_unresolved``.
    - ``"no_action"``               — the reused dispatcher declined (no
      capable machine, already in flight, gate not actually satisfied,
      entry vanished from the board/queue between detection and dispatch).
    - ``"skipped_live_session"``    — a running/pending assignment already
      exists for this (repo, issue); never act underneath a live session
      (#602).
    - ``"skipped_human_required"``  — the conflict-fix retry cap was already
      hit; surfacing to a human, not auto-retrying.
    - ``"disabled"``                — ``pipeline.auto_dispatch_stalled`` is
      off; detection/narration still happened, dispatch did not.
    """
    detail: str = ""


# Action kinds that represent a REAL dispatch OR a board mutation that must
# be persisted (mutate the board / merge queue / fire an agent request) —
# used to decide (a) whether the board needs writing back, (b) which GitHub
# comment to post, and (c) whether the audit row is business-tier (a real
# transition) or operational-tier (a no-op/skip, informational only).
#
# ``review_transition_applied`` belongs here even though it does not launch
# an agent: an approved/approved-with-nits/terminal-skip resolution from
# ``process_review_completion`` still flips ``work.review_state``/
# ``review.review_verdict`` in place, and losing that mutation while the
# one-shot ledger marks the row notified anyway is exactly the #1478 review
# bug this set exists to prevent.
_STALLED_DISPATCH_KINDS = frozenset({
    "fix_dispatch_attempted", "review_transition_applied", "review_dispatched",
    "enqueued", "conflict_fix_dispatched",
    # #1582
    "review_verdict_recovered", "review_reset_redispatched",
})

# process_review_completion (and the _dispatch_fix_for_review it may call)
# kinds that mutate `board` in place per its own documented contract, even
# when they don't dispatch a fix worker. `disabled`/`no_findings` return
# before any mutation; `no_work_found`/`max_iterations` return without
# touching `board` (only a GitHub notice for the latter).
_MUTATING_REVIEW_COMPLETION_KINDS = frozenset({
    "fix_dispatched", "approved", "approved_with_nits", "terminal_skip",
})


def _stalled_row_has_live_session(board: "Board", work: "Assignment") -> bool:
    """#602 guardrail: true when a running/pending assignment already exists
    for *work*'s (repo, issue) — e.g. an interactive ``--fix-of``/
    ``--review-of``/``--merge-of`` session a human is actively driving.
    :func:`dispatch_stalled_pipeline_action` must never act underneath one:
    racing an auto-dispatch against a live session can duplicate or clobber
    it. Broader than :func:`coord.claim.has_active_work_followup` (which
    only checks ``work``/``conflict-fix``) — any live assignment type
    (review, smoke, chat, ...) for the same issue counts here.
    """
    for a in board.active:
        if a.status not in ("running", "pending"):
            continue
        if a.repo_name == work.repo_name and a.issue_number == work.issue_number:
            return True
    return False


def dispatch_stalled_pipeline_action(
    detection: StalledDetection,
    work: "Assignment",
    board: "Board",
    config: Config,
    *,
    terminal_cache: dict | None = None,
) -> StalledDispatchAction:
    """#1478: act on a #1441 stalled-pipeline detection instead of only
    narrating it.

    Gated by ``config.pipeline.auto_dispatch_stalled`` (default ``False`` —
    detection/narration via :func:`post_stalled_pipeline` is unconditional;
    this is the opt-in action half). Mutates *board* in place exactly like
    the auto-loop / review-dispatch helpers it delegates to — the caller is
    responsible for persisting it.

    Reuses the SAME dispatch machinery the original, on-time transition
    would have used for each reason, rather than re-deriving new logic:

    - ``review_request_changes_no_fix`` → re-locates the ``request-changes``
      review and re-runs :func:`coord.auto_loop.process_review_completion`
      on it — the exact function the auto-loop calls the instant a review
      transitions to done, complete with its iteration cap and terminal
      guard.
    - ``review_done_no_verdict`` (#1582) → :func:`coord.diagnose._recover_review`
      (the exact recovery ``coord diagnose --stage review`` runs: try the
      session transcript first). A recovered verdict is then run through
      :func:`coord.auto_loop.process_review_completion` like a normal
      transition; nothing recoverable falls through to
      :func:`coord.diagnose._reset_review_stage` (the exact reset
      ``coord diagnose --stage review --reset`` runs — keeps the branch,
      wipes the review rows + review_state) followed by a fresh
      :func:`coord.review.dispatch_review` call.
    - ``done_no_review`` → :func:`coord.review.dispatch_review`, the same
      call ``detect_transitions``/``dispatch_pending_reviews`` make on a
      fresh work completion.
    - ``approved_not_queued`` → :func:`coord.merge_queue.enqueue_approved_work`,
      the same bulk gate-checked enqueue the daemon passive tick already
      runs on every interval.
    - ``merge_conflict_unresolved`` → :func:`coord.conflict_fix.dispatch_conflict_fix`,
      the #1474 ``_dispatch_conflict_fixes`` path.
    - ``review_failed_no_verdict`` (#1584) → :func:`coord.review.dispatch_review`
      again, the SAME call as ``done_no_review`` — the failed review left no
      verdict behind, so recovery is identical to "no review was ever
      dispatched": open a fresh one against the still-``done`` work row.

    Never re-entrant across ticks: the caller only reaches this after
    :func:`detect_stalled_pipeline` has already filtered out any row whose
    ``_stalled_notified_key`` is in the ``notified`` ledger, and the caller
    marks that key notified right after this returns (via
    :func:`post_stalled_pipeline` or :func:`post_stalled_pipeline_dispatch`)
    — so a given assignment_id gets exactly one dispatch attempt per stall,
    mirroring the one-shot comment (#1441's own guardrail, reused rather
    than re-derived per #1478's own request).
    """
    if not config.pipeline.auto_dispatch_stalled:
        return StalledDispatchAction(
            kind="disabled", detail="pipeline.auto_dispatch_stalled is False",
        )

    if _stalled_row_has_live_session(board, work):
        return StalledDispatchAction(
            kind="skipped_live_session",
            detail=(
                f"a running/pending assignment already exists for "
                f"{work.repo_name}#{work.issue_number} — not acting "
                "underneath a live session (#602)"
            ),
        )

    if detection.reason == "review_request_changes_no_fix":
        from coord.auto_loop import process_review_completion  # noqa: PLC0415

        all_assignments = list(board.active) + list(board.completed)
        review = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == work.assignment_id and a.type == "review"
            ),
            None,
        )
        if review is None:
            return StalledDispatchAction(
                kind="no_action", detail="review no longer found on board",
            )
        machine_host = next(
            (m.host for m in config.machines if m.name == review.machine_name), None,
        )
        actions = process_review_completion(
            review, board, config,
            machine_host=machine_host, terminal_cache=terminal_cache,
        )
        kind_set = {a.kind for a in actions}
        kinds = ", ".join(a.kind for a in actions) or "no_action"
        details = "; ".join(a.detail for a in actions if a.detail)
        detail_msg = f"process_review_completion → {kinds}" + (f" ({details})" if details else "")
        # #1478 review fix: `process_review_completion` mutates `board` in
        # place for several outcomes besides `fix_dispatched` — an
        # `approved`/`approved_with_nits`/`terminal_skip` resolution still
        # flips `review.review_verdict`/`work.review_state` and refreshes the
        # merge-queue entry (see that function's own "caller is responsible
        # for persisting the board" contract). Classifying those as
        # `no_action` silently dropped the mutation (the sweep's `board_dirty`
        # never got set) while the one-shot ledger still marked the row
        # notified — permanently losing the transition. Any kind in
        # `_MUTATING_REVIEW_COMPLETION_KINDS` must therefore map to a
        # `_STALLED_DISPATCH_KINDS` member so `_sweep_stalled_pipeline`
        # persists it.
        if "fix_dispatched" in kind_set:
            return StalledDispatchAction(kind="fix_dispatch_attempted", detail=detail_msg)
        if kind_set & _MUTATING_REVIEW_COMPLETION_KINDS:
            return StalledDispatchAction(kind="review_transition_applied", detail=detail_msg)
        return StalledDispatchAction(kind="no_action", detail=detail_msg)

    if detection.reason == "review_done_no_verdict":
        # #1582: a review finalised `done` with no verdict ever captured
        # (#812). Reuse the SAME two steps `coord diagnose --stage review
        # [--reset]` runs for this exact shape, rather than re-deriving new
        # recovery/reset logic — `_recover_review`/`_reset_review_stage` are
        # the private functions behind that command for this branch. Called
        # directly (not through the full `diagnose_stage` orchestration),
        # which skips that command's tmux session-state probe and
        # issue-wide phantom-row cleanup — the review here is already
        # terminal, so neither applies, and both would add real
        # subprocess/ssh cost to every notify sweep tick.
        from coord.diagnose import (  # noqa: PLC0415
            DiagnoseResult,
            _recover_review,
            _reset_review_stage,
        )

        all_assignments = list(board.active) + list(board.completed)
        review = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == work.assignment_id and a.type == "review"
            ),
            None,
        )
        if review is None:
            return StalledDispatchAction(
                kind="no_action", detail="review no longer found on board",
            )

        diag = DiagnoseResult(
            repo_name=work.repo_name, issue_number=work.issue_number, stage="review",
        )
        # `state="unknown"` is safe: `_recover_review`'s live/dead-session
        # branches are only reached when `latest.status != "done"`, which
        # can't happen here (`detect_stalled_pipeline` only flags this
        # reason for a `status="done"` review).
        _recover_review(board, config, review, "unknown", diag, dry_run=False)

        if diag.recovered:
            # A verdict was recovered from the session transcript and
            # durably persisted (#617's `_review_findings_from_transcript` →
            # `issue_store.post_result`). Run it through the SAME auto-loop
            # chokepoint a live review completion would have used — mirrors
            # `review_request_changes_no_fix` just above — so a recovered
            # `request-changes` still gets its fix worker and a recovered
            # `approve` still advances the pipeline.
            from coord.auto_loop import process_review_completion  # noqa: PLC0415

            machine_host = next(
                (m.host for m in config.machines if m.name == review.machine_name), None,
            )
            actions = process_review_completion(
                review, board, config,
                machine_host=machine_host, terminal_cache=terminal_cache,
            )
            kind_set = {a.kind for a in actions}
            kinds = ", ".join(a.kind for a in actions) or "no_action"
            details = "; ".join(a.detail for a in actions if a.detail)
            detail_msg = (
                "recovered verdict from the session transcript → "
                f"process_review_completion → {kinds}" + (f" ({details})" if details else "")
            )
            if "fix_dispatched" in kind_set:
                return StalledDispatchAction(kind="fix_dispatch_attempted", detail=detail_msg)
            if kind_set & _MUTATING_REVIEW_COMPLETION_KINDS:
                return StalledDispatchAction(kind="review_transition_applied", detail=detail_msg)
            return StalledDispatchAction(kind="review_verdict_recovered", detail=detail_msg)

        if not diag.needs_reset:
            return StalledDispatchAction(
                kind="no_action", detail="; ".join(diag.findings) or "nothing to do",
            )

        # Nothing recoverable — reset the review stage (delete the review
        # rows, clear review_state — #1180's `_reset_review_stage`, KEEPS
        # the branch/commits) and re-dispatch a fresh review.
        reset_res = DiagnoseResult(
            repo_name=work.repo_name, issue_number=work.issue_number, stage="review",
        )
        _reset_review_stage(
            config, work.repo_name, work.issue_number, reset_res,
            dry_run=False, assignment_id=work.assignment_id,
        )
        if not reset_res.reset_performed:
            return StalledDispatchAction(
                kind="no_action",
                detail="reset did not complete: " + "; ".join(reset_res.findings),
            )

        # `_reset_review_stage` writes the canonical DB directly (the same
        # seam `coord diagnose --reset` uses — see commands/status.py's
        # "NOTE: deliberately NO save_board" comment for why) WITHOUT
        # touching `board`. Mirror the same two writes on `board` in place
        # so a later `write_board` upsert of the now-stale `review`/`work`
        # objects doesn't resurrect the just-deleted review row or clobber
        # the just-cleared review_state back to its wedged value.
        board.active[:] = [
            a for a in board.active
            if not (a.type == "review" and a.review_of_assignment_id == work.assignment_id)
        ]
        board.completed[:] = [
            a for a in board.completed
            if not (a.type == "review" and a.review_of_assignment_id == work.assignment_id)
        ]
        work.review_state = "pending"
        work.review_verdict = None
        work.review_posted_at = None

        from coord.review import dispatch_review  # noqa: PLC0415

        new_review = dispatch_review(work, board, config, terminal_cache=terminal_cache)
        if new_review is None:
            return StalledDispatchAction(
                kind="no_action",
                detail=(
                    "review stage reset (no verdict recoverable) but "
                    "re-dispatch declined (no machine / already in flight / gate)"
                ),
            )
        return StalledDispatchAction(
            kind="review_reset_redispatched",
            detail=(
                "no verdict recoverable from transcript — reset the review "
                f"stage and re-dispatched as {new_review.assignment_id} to "
                f"{new_review.machine_name}"
            ),
        )

    if detection.reason == "done_no_review":
        from coord.review import dispatch_review  # noqa: PLC0415

        review = dispatch_review(work, board, config, terminal_cache=terminal_cache)
        if review is None:
            return StalledDispatchAction(
                kind="no_action",
                detail="dispatch_review declined (no machine / already in flight / gate)",
            )
        return StalledDispatchAction(
            kind="review_dispatched",
            detail=f"review {review.assignment_id} dispatched to {review.machine_name}",
        )

    if detection.reason == "review_failed_no_verdict":
        # #1584: the previous review died with no verdict — recovery is
        # identical to `done_no_review` above: `work` itself is still
        # `status="done"` (only the review it spawned failed), so a fresh
        # `dispatch_review` call is a normal, ungated re-dispatch. Reusing
        # the same call (rather than e.g. `coord retry` against the dead
        # review row) also picks up any board state that changed since —
        # same reasoning `done_no_review` already relies on.
        from coord.review import dispatch_review  # noqa: PLC0415

        # Belt-and-braces against the usage-limit kill (#1461/#1584):
        # `detect_stalled_pipeline` already skips those rows at
        # classification, but this function is public and is also reachable
        # with a caller-built detection, or after a race in which the
        # usage-limit `failure_reason` was stamped onto the review row
        # between detection and dispatch. Re-dispatching into an
        # account-wide exhausted budget only produces another corpse, so
        # decline — mirroring `_decide_review`'s WAIT in `coord/drive.py`.
        all_assignments = list(board.active) + list(board.completed)
        dead_review = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == work.assignment_id
                and a.type == "review"
                and a.status == "failed"
            ),
            None,
        )
        if dead_review is not None and is_usage_limit_reason(dead_review.failure_reason):
            return StalledDispatchAction(
                kind="no_action",
                detail=(
                    f"review {dead_review.assignment_id} was killed by the "
                    f"usage limit ({dead_review.failure_reason}) — waiting "
                    "for the reset instead of re-dispatching"
                ),
            )

        review = dispatch_review(work, board, config, terminal_cache=terminal_cache)
        if review is None:
            return StalledDispatchAction(
                kind="no_action",
                detail="dispatch_review declined (no machine / already in flight / gate)",
            )
        return StalledDispatchAction(
            kind="review_dispatched",
            detail=f"review {review.assignment_id} dispatched to {review.machine_name}",
        )

    if detection.reason == "approved_not_queued":
        from coord.merge_queue import enqueue_approved_work, load_queue  # noqa: PLC0415

        changed = enqueue_approved_work(config, board)
        if work.assignment_id in changed:
            return StalledDispatchAction(
                kind="enqueued", detail=f"{work.assignment_id} enqueued for merge",
            )
        # #1478 review non-blocking finding: `enqueue_approved_work` bulk-
        # enqueues EVERY eligible row on `board.completed`, not just this one.
        # If an earlier row in the same sweep tick already triggered the
        # enqueue for this assignment, this call's `changed` list comes back
        # without it (nothing new to do) even though it genuinely is queued —
        # checking `changed` alone would misreport a real outcome as
        # `no_action`. Check queue membership directly instead of relying
        # solely on `changed`.
        if any(m.assignment_id == work.assignment_id for m in load_queue()):
            return StalledDispatchAction(
                kind="enqueued",
                detail=(
                    f"{work.assignment_id} already enqueued for merge (queued "
                    "earlier in this sweep tick)"
                ),
            )
        return StalledDispatchAction(
            kind="no_action",
            detail="enqueue_approved_work made no change for this assignment",
        )

    if detection.reason == "merge_conflict_unresolved":
        from coord.conflict_fix import (  # noqa: PLC0415
            dispatch_conflict_fix,
            has_prior_conflict_fix,
        )
        from coord.merge_queue import load_queue  # noqa: PLC0415

        entry = next(
            (m for m in load_queue() if m.assignment_id == work.assignment_id), None,
        )
        if entry is None:
            return StalledDispatchAction(
                kind="no_action", detail="merge queue entry no longer found",
            )
        if has_prior_conflict_fix(board, entry.assignment_id):
            return StalledDispatchAction(
                kind="skipped_human_required",
                detail="conflict-fix already active or its retry cap was already hit",
            )
        fix = dispatch_conflict_fix(entry, board, config, prefer_machine=work.machine_name)
        if fix is None:
            return StalledDispatchAction(
                kind="no_action",
                detail="dispatch_conflict_fix declined (no machine / no repo_path)",
            )
        return StalledDispatchAction(
            kind="conflict_fix_dispatched",
            detail=f"conflict-fix {fix.assignment_id} dispatched to {fix.machine_name}",
        )

    return StalledDispatchAction(
        kind="no_action", detail=f"no dispatch arm for reason={detection.reason!r}",
    )


def post_stalled_pipeline_dispatch(
    detection: StalledDetection, action: StalledDispatchAction, config: Config,
) -> None:
    """Post the #1478 auto-dispatch outcome comment and mark notified.

    Posted INSTEAD OF :func:`post_stalled_pipeline` when
    :func:`dispatch_stalled_pipeline_action` actually dispatched something
    for this row (see that function's *kind* values) — the two write to the
    same GitHub thread, so posting both would leave a directly
    contradictory "nothing was dispatched automatically" comment sitting
    right above this one.
    """
    repo = config.repo(detection.repo_name)
    repo_github = repo.github if repo is not None else None
    if not repo_github:
        return
    body = format_stalled_pipeline_dispatch(
        assignment_id=detection.assignment_id,
        repo_name=detection.repo_name,
        issue_number=detection.issue_number,
        reason=detection.reason,
        action_kind=action.kind,
        action_detail=action.detail,
    )
    github_ops.post_issue_comment(repo_github, detection.issue_number, body)
    mark_notified(_stalled_notified_key(detection.assignment_id), EVENT_STALLED)


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
            elif entry_status == "advisory":
                # #448: advisory (0-commit clean exit) — post a distinctive
                # GitHub comment so operators who rely on GitHub (not just
                # coord status) know the worker finished with no code change
                # and that human review is needed.
                event = EVENT_ADVISORY
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
                    # #1710: thread the dispatch record's resolved provider
                    # name through so a non-claude worker's log parses via
                    # its own provider rather than always assuming claude.
                    parsed = parse_progress(
                        entry_log, provider_name=record.get("provider_name"),
                    )
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


def _capture_completion_summary(transition: Transition, entry: dict) -> None:
    """#874: parse the worker's ### Summary block and persist it on the row.

    Tries the local log first, then falls back to the agent's /logs/<id>
    endpoint for remote-agent assignments.  Silent on failure — a worker
    that emits no summary leaves the field NULL without error.
    """
    from coord.progress import (  # noqa: PLC0415
        parse_completion_summary_from_agent,
        parse_completion_summary_from_log,
    )
    from coord.state import update_assignment_completion_summary  # noqa: PLC0415

    prose: str | None = None
    log_path = entry.get("log_path")
    if log_path:
        try:
            prose = parse_completion_summary_from_log(Path(log_path))
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "_capture_completion_summary: failed to parse local log for %s: %s",
                transition.assignment_id, exc,
            )

    if prose is None:
        # Local log unavailable (remote-agent assignment) — fetch via the
        # agent's /logs/<id> endpoint.  Same fallback used by smoke tests.
        host = _agent_host(transition.machine_name)
        if host:
            try:
                prose = parse_completion_summary_from_agent(host, transition.assignment_id)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "_capture_completion_summary: failed to fetch from agent %s for %s: %s",
                    host, transition.assignment_id, exc,
                )

    if prose is None:
        # No ### Summary block anywhere — leave completion_summary NULL.
        return
    try:
        update_assignment_completion_summary(transition.assignment_id, prose)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_capture_completion_summary: failed to persist summary for %s: %s",
            transition.assignment_id, exc,
        )


def _capture_smoke_tests(transition: Transition, entry: dict) -> None:
    """#252: parse the worker's SMOKE_TESTS block and persist it on the row.

    Tries the local log first, then falls back to the agent's /logs/<id>
    endpoint for remote-agent assignments (mirrors the plan and review
    capture paths).  Silent on failure.
    """
    from coord.progress import (  # noqa: PLC0415
        parse_smoke_tests_from_agent,
        parse_smoke_tests_from_log,
    )
    from coord.state import update_assignment_smoke_tests  # noqa: PLC0415

    parsed: list[str] | None = None
    log_path = entry.get("log_path")
    if log_path:
        try:
            parsed = parse_smoke_tests_from_log(Path(log_path))
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "_capture_smoke_tests: failed to parse local log for %s: %s",
                transition.assignment_id, exc,
            )

    if parsed is None:
        # Local log unavailable (remote-agent assignment) — fetch via the
        # agent's /logs/<id> endpoint.  Same fallback the plan and review
        # paths use.
        host = _agent_host(transition.machine_name)
        if host:
            try:
                parsed = parse_smoke_tests_from_agent(host, transition.assignment_id)
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "_capture_smoke_tests: failed to fetch from agent %s for %s: %s",
                    host, transition.assignment_id, exc,
                )

    if parsed is None:
        # No SMOKE_TESTS block anywhere — leave smoke_tests NULL so the
        # TUI shows the graceful-degradation placeholder.
        return
    try:
        update_assignment_smoke_tests(transition.assignment_id, parsed)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_capture_smoke_tests: failed to persist list for %s: %s",
            transition.assignment_id, exc,
        )


def _capture_cost(transition: Transition, entry: dict, record: dict | None = None) -> None:
    """#208/#546: parse the worker's final cost+tokens and persist them.

    Preferred source is the local stream-json log (cheap, no network).
    Falls back to the agent's status entry, which carries ``cost_so_far``
    / ``total_cost_usd`` reported live by the worker.  Tokens are only
    available from the log (not from the agent status dict), so they are
    captured when the local log exists.  Either path is best-effort —
    failure is silent so it can't block the comment post.

    #1710: *record* (the dispatch record from ``load_dispatched()``) carries
    ``provider_name`` — threaded into :func:`coord.usage.parse_usage_from_log`
    so cost/token parsing uses the assignment's actual provider instead of
    always assuming claude. ``None`` (no record, or predates #324) falls back
    to the claude default, unchanged from before #1710.
    """
    from coord.state import update_assignment_cost, update_assignment_tokens  # noqa: PLC0415
    from coord.usage import parse_usage_from_log  # noqa: PLC0415

    cost: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    provider_name = (record or {}).get("provider_name")

    log_path = entry.get("log_path")
    if log_path:
        try:
            parsed = parse_usage_from_log(Path(log_path), provider_name=provider_name)
            if parsed is not None:
                if parsed.total_cost_usd > 0:
                    cost = parsed.total_cost_usd
                # #546: also capture token counts from the same parse.
                input_tokens = parsed.input_tokens
                output_tokens = parsed.output_tokens
                cache_creation_tokens = parsed.cache_creation_tokens
                cache_read_tokens = parsed.cache_read_tokens
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "_capture_cost: failed to parse log for %s: %s",
                transition.assignment_id, exc,
            )

    if cost is None:
        # Fall back to the live value the agent had at reap time.
        remote_cost = entry.get("total_cost_usd") or entry.get("cost_so_far")
        if remote_cost is not None:
            try:
                cost = float(remote_cost)
            except (TypeError, ValueError):
                cost = None

    # #667: token fallback — when the local log was absent/unreadable the
    # token counts are still 0.  The agent now includes them in the /status
    # completed entry, so read them from there.
    if input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens == 0:
        try:
            input_tokens = int(entry.get("input_tokens") or 0)
            output_tokens = int(entry.get("output_tokens") or 0)
            cache_creation_tokens = int(entry.get("cache_creation_tokens") or 0)
            cache_read_tokens = int(entry.get("cache_read_tokens") or 0)
        except (TypeError, ValueError):
            pass

    if cost is not None and cost > 0:
        try:
            update_assignment_cost(transition.assignment_id, cost)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "_capture_cost: failed to persist cost for %s: %s",
                transition.assignment_id, exc,
            )

    # #546: persist token counts (best-effort; silent on missing columns).
    if input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens > 0:
        try:
            update_assignment_tokens(
                transition.assignment_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_tokens=cache_creation_tokens,
                cache_read_tokens=cache_read_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "_capture_cost: failed to persist tokens for %s: %s",
                transition.assignment_id, exc,
            )


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


def _persist_review_findings(assignment_id: str, verdict: str, body: str) -> None:
    """#bounce: persist both verdict + findings body in one shot.

    Mirrors `_persist_review_verdict` (which we keep for callers that
    only have the verdict) but also caches the body so `coord bounce`
    can skip the slow HTTP log fetch.  Best-effort; a DB error is
    logged and swallowed.
    """
    if verdict not in ("approve", "request-changes"):
        return
    try:
        from coord.state import update_assignment_review_findings  # noqa: PLC0415

        update_assignment_review_findings(
            assignment_id, verdict=verdict, body=body,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Failed to persist review_findings for %s: %s", assignment_id, exc
        )


def _fetch_raw_log_text(transition: Transition, entry: dict) -> str | None:
    """Best-effort raw log text for #1956/#1348 diagnostics.

    Mirrors the local-file-then-agent-fetch fallback :func:`_try_parse_and_post_review`
    itself uses to PARSE the log, but returns the raw text instead — the
    diagnostic detectors (:func:`coord.review.detect_end_review_without_verdict`,
    :func:`coord.review.detect_unparsed_review_marker`) need the text the
    strict parser already rejected, not another parse attempt. Returns
    ``None`` on any I/O failure — diagnostics are best-effort by design and
    must never be the reason ``coord notify`` raises.
    """
    log_path = entry.get("log_path")
    if log_path:
        try:
            return Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    host = _agent_host(transition.machine_name)
    if host:
        try:
            resp = httpx.get(
                f"http://{host}:{AGENT_PORT}/logs/{transition.assignment_id}",
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.text
        except (httpx.HTTPError, httpx.TimeoutException):
            return None
    return None


def _warn_missing_review_verdict(
    transition: Transition, entry: dict, diagnostic: list,
) -> None:
    """#1956: when a review's structured verdict could not be parsed, make it
    LOUD instead of silent — run the #1348/#1956 diagnostics against the raw
    log text and ``log.warning`` a recovery command.

    Before this, a review that reached ``END_REVIEW`` with a full body but
    no ``REVIEW_VERDICT:`` header (quadraui#533's live incident — grepping
    the raw log found the string exactly once, inside the briefing's own
    instructions, never in an assistant message) landed ``status="done"``
    with ``review_verdict IS NULL`` and nothing anywhere said so; the merge
    gate just read ``review_required`` forever. Appends whichever
    diagnostic fired (if any) to *diagnostic* so :func:`post_transition` can
    tailor the GitHub-visible completion comment too — the operator should
    not have to go spelunking in ``coord notify``'s own log to learn this.
    Best-effort throughout: a failure to even fetch the raw text is
    swallowed, matching this module's "never crash notify" contract.
    """
    from coord.review import (  # noqa: PLC0415
        detect_end_review_without_verdict,
        detect_unparsed_review_marker,
    )

    text = _fetch_raw_log_text(transition, entry)
    if not text:
        return
    aid = transition.assignment_id
    log_path = entry.get("log_path")
    recover_hint = (
        f"coord report-result --assignment {aid} "
        "--verdict <approve|request-changes> --verdict-source recovered "
        '--verdict-reason "<why>" --body-file <extracted-review.md>'
    )

    end_marker = detect_end_review_without_verdict(text, transcript_path=log_path)
    if end_marker is not None:
        log.warning(
            "review %s: reviewer wrote END_REVIEW but never emitted "
            "REVIEW_VERDICT: anywhere (#1956) — this is NOT a crashed/"
            "truncated session, the verdict is very likely recoverable "
            "from the transcript. Recover with:\n  %s\nExcerpt before "
            "END_REVIEW:\n%s",
            aid, recover_hint, end_marker.excerpt,
        )
        diagnostic.append(end_marker)
        return

    marker = detect_unparsed_review_marker(text, transcript_path=log_path)
    if marker is not None:
        log.warning(
            "review %s: a REVIEW_VERDICT: marker is present but malformed "
            "(#1348, detected word=%r) — the strict parser rejected it. "
            "Recover with:\n  %s",
            aid, marker.verdict_word, recover_hint,
        )
        diagnostic.append(marker)
        return

    log.debug(
        "review %s: no REVIEW_VERDICT:/END_REVIEW markers found at all — "
        "likely a crashed or truncated session, not a #1956/#1348 "
        "recoverable case",
        aid,
    )


def _try_parse_and_post_review(
    transition: Transition,
    record: dict,
    entry: dict,
    duration: float | None,
    *,
    _diagnostic: list | None = None,
) -> bool:
    """Parse reviewer findings from the log and post as a PR review or issue comment.

    Returns True if a review was successfully posted (either as a ``gh pr review``
    or as an issue comment when no PR number is available), False on any failure.
    Silently swallows all errors so callers can fall back gracefully.

    *_diagnostic* (#1956): optional out-parameter, mirroring
    ``coord.interactive``'s identically-shaped convention for #1348. When a
    list is supplied and the structured verdict cannot be parsed, whichever
    of :func:`coord.review.detect_end_review_without_verdict` /
    :func:`coord.review.detect_unparsed_review_marker` fires is appended to
    it, so the caller can tailor the fallback GitHub comment instead of a
    generic "could not be extracted" message every single time.
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
        if _diagnostic is not None:
            try:
                _warn_missing_review_verdict(transition, entry, _diagnostic)
            except Exception as exc:  # noqa: BLE001 — diagnostics must never crash notify
                log.debug(
                    "review %s: #1956 diagnostic itself failed: %s",
                    transition.assignment_id, exc,
                )
        return False

    # #253: persist the parsed verdict on the review assignment so the merge
    # gate can refuse to merge work whose review hasn't approved.  Independent
    # of auto_loop (which may be disabled in config).
    # #bounce: also persist the findings.body so `coord bounce` (and the
    # future per-stage display) can read it from the DB without re-fetching
    # the worker's full log.
    _persist_review_findings(
        transition.assignment_id, findings.verdict, findings.body
    )

    review_target = record.get("review_target")
    repo_github = record["repo_github"]

    # Determine whether review_target is a PR number (integer string) or a branch.
    pr_number: int | None = None
    if review_target:
        try:
            pr_number = int(review_target)
        except (ValueError, TypeError):
            pr_number = None

    # #248: prepend a machine-readable header so the TUI / coordinator can
    # surface the verdict + counts without re-ingesting the prose body.
    body_with_header = _attach_review_header(
        findings.body,
        verdict=findings.verdict,
        reviewer_machine=transition.machine_name,
        assignment_id=transition.assignment_id,
    )

    if pr_number is not None:
        try:
            github_ops.post_pr_review(repo_github, pr_number, findings.verdict, body_with_header)
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
        f"{body_with_header}"
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


def _attach_review_header(
    body: str,
    *,
    verdict: str,
    reviewer_machine: str | None = None,
    assignment_id: str | None = None,
) -> str:
    """#248: prepend the machine-readable header line to a review *body*.

    Counts are derived heuristically from the body's markdown sections.
    The header always carries the verdict; counts/identity fields are
    omitted when unavailable.
    """
    from coord.review import (  # noqa: PLC0415 — local import keeps import graph clean
        estimate_review_counts, format_review_header,
    )
    blocking, nonblocking, nits = estimate_review_counts(body)
    header = format_review_header(
        verdict=verdict,
        reviewer_machine=reviewer_machine,
        assignment_id=assignment_id,
        blocking=blocking,
        nonblocking=nonblocking,
        nits=nits,
    )
    return f"{header}\n\n{body}"


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
    from coord.plan_parser import parse_plan_from_log, parse_plan_from_agent  # noqa: PLC0415

    log_path = entry.get("log_path")
    worker_plan = None
    if log_path:
        try:
            worker_plan = parse_plan_from_log(log_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to parse plan log for %s: %s", transition.assignment_id, exc)

    # Local log unavailable (worker ran on a remote agent — entry.log_path
    # is the agent's filesystem path, not the coordinator's).  Mirror the
    # review path: fall back to the agent's /logs/<id> endpoint.  Without
    # this, every remote-agent plan got posted as a generic "completion"
    # comment and the structured plan was lost (we hit this on quadraui#264).
    if worker_plan is None or worker_plan.is_empty():
        host = _agent_host(transition.machine_name)
        if host:
            try:
                worker_plan = parse_plan_from_agent(host, transition.assignment_id)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Failed to fetch plan log from agent %s for %s: %s",
                    host, transition.assignment_id, exc,
                )

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


def _capture_claude_session_id(transition: Transition, entry: dict) -> None:
    """#315: persist the worker's claude session ID to the coordinator DB.

    The agent captures this from the ``system.init`` event in the worker log
    and includes it in the ``/status`` response.  Once stored in the DB,
    ``coord chat-continue`` can read it and pass ``--resume <id>`` to the
    next worker so it loads the prior conversation.  Best-effort; a missing
    ID just means chat-continue will refuse with a clear error.
    """
    session_id = entry.get("claude_session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    try:
        from coord.state import update_assignment_claude_session_id  # noqa: PLC0415
        update_assignment_claude_session_id(transition.assignment_id, session_id)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "_capture_claude_session_id: failed for %s: %s",
            transition.assignment_id, exc,
        )


def post_transition(transition: Transition, record: dict, entry: dict) -> None:
    """Post the GitHub comment for one transition and mark it notified."""
    started = entry.get("started_at")
    finished = entry.get("finished_at")
    duration = (finished - started) if (started and finished) else None
    # #208: capture worker cost as soon as the assignment completes — the
    # value is in the worker's final stream-json result event and would
    # otherwise be lost when the agent prunes the log.  Best-effort:
    # local log → remote agent entry → skip.
    _capture_cost(transition, entry, record)
    # #252: capture the worker-emitted SMOKE_TESTS block at the same
    # moment so the TUI can render it under the Test stage.  Same
    # best-effort discipline — failure is silent.
    _capture_smoke_tests(transition, entry)
    # #874: capture the worker's ### Summary prose block at the same moment
    # so the board has a durable, queryable summary field.  Best-effort.
    _capture_completion_summary(transition, entry)
    # #315: persist the worker's claude session ID so chat-continue can
    # pass --resume to the next worker.  Best-effort; silent on failure.
    _capture_claude_session_id(transition, entry)
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
    if transition.event == EVENT_COMPLETION and assignment_type in (
        "refinement",
        "milestone-chat",
    ):
        # #315: refinement chat turns are developer-side conversation — do NOT
        # post completion comments to GitHub.  Each turn would spam the issue
        # with identical "assignment completed" noise.  We still capture cost,
        # smoke tests, and session ID above; just skip the GitHub post.
        # #770: milestone-chat is dispatched AGAINST the tracking issue
        # itself (unlike refinement's target issue, this one is the live
        # planning document a human reads) — a generic completion comment on
        # every conversational turn would be even noisier here. The
        # meaningful GitHub-visible effect is the tracking issue's body
        # update via `coord milestone write-order`, not a completion comment.
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
    elif transition.event == EVENT_COMPLETION and assignment_type == "plan":
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
        # back to a plain completion comment noting the parse failure — #1956:
        # tailored per-diagnostic instead of one generic message, so an
        # operator reading GitHub (not `coord notify`'s own log) can ALSO see
        # that a verdict is recoverable, not just that parsing failed.
        _diag: list = []
        posted = _try_parse_and_post_review(
            transition, record, entry, duration, _diagnostic=_diag,
        )
        if not posted:
            from coord.review import EndReviewWithoutVerdict  # noqa: PLC0415

            if _diag and isinstance(_diag[0], EndReviewWithoutVerdict):
                fallback_summary = (
                    "Review assignment completed and the reviewer wrote END_REVIEW, "
                    "but never emitted the machine-readable REVIEW_VERDICT: header "
                    "(#1956) — this is NOT a crashed/truncated session, the verdict "
                    "is very likely recoverable from the transcript. Recover with: "
                    f"`coord report-result --assignment {transition.assignment_id} "
                    "--verdict <approve|request-changes> --verdict-source recovered "
                    '--verdict-reason "<why>" --body-file <extracted-review.md>`.'
                )
            elif _diag:
                fallback_summary = (
                    "Review assignment completed but a REVIEW_VERDICT: marker in "
                    "the worker log was malformed and could not be parsed (#1348) "
                    "— the verdict is likely still recoverable from the transcript. "
                    "Recover with: "
                    f"`coord report-result --assignment {transition.assignment_id} "
                    "--verdict <approve|request-changes> --verdict-source recovered "
                    '--verdict-reason "<why>" --body-file <extracted-review.md>`.'
                )
            else:
                fallback_summary = (
                    "Review assignment completed but findings could not be extracted "
                    "from the worker log. The reviewer may not have produced the "
                    "expected structured output (REVIEW_VERDICT / REVIEW_BODY / END_REVIEW)."
                )
            post_completion(
                exit_code=transition.exit_code or 0,
                summary=fallback_summary,
                **common,
            )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
    elif transition.event == EVENT_COMPLETION and assignment_type == "conflict-fix":
        post_completion(exit_code=transition.exit_code or 0, **common)
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
        # Re-enqueue the parent merge entry so the next `coord merge` retries.
        # This mirrors the reconcile() path — whichever runs first wins.
        parent_id = record.get("review_of_assignment_id")
        if parent_id:
            from coord.reconcile import on_conflict_fix_done  # noqa: PLC0415
            on_conflict_fix_done(
                parent_assignment_id=parent_id,
                fix_assignment_id=transition.assignment_id,
                machine_name=transition.machine_name,
                succeeded=True,
            )
    elif transition.event == EVENT_COMPLETION and assignment_type == "smoke":
        # #1021: propagate the headless smoke exit code to the parent work
        # row's Test verdict so the merge gate is satisfied automatically.
        post_completion(exit_code=transition.exit_code or 0, **common)
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
        parent_id = record.get("review_of_assignment_id")
        if parent_id:
            # Guard: only auto-certify when the issue's test-mode is "auto"
            # or unset (no label).  A "smoke" label means the TUI offers an
            # interactive smoke agent — do NOT auto-certify here.
            from coord.state import get_issue_test_mode, record_test_verdict  # noqa: PLC0415
            test_mode = get_issue_test_mode(
                transition.repo_name, transition.issue_number
            )
            if test_mode != "smoke":
                succeeded = (transition.exit_code or 0) == 0
                # #1384: no `smoke_test=` argument needed — the writer
                # (`state._record_test_verdict_local`) derives the legacy
                # mirror from `test_state`, so a headless smoke FAILURE lands
                # as test_state='failed' AND smoke_test='fail' and stays
                # reachable from `coord fix`.
                record_test_verdict(
                    assignment_id=parent_id,
                    test_state="passed" if succeeded else "failed",
                    test_reason="headless smoke",
                )
    elif transition.event == EVENT_FAILURE and assignment_type == "smoke":
        # #1605: the Test-stage WORKER itself died (a dead agent, a killed
        # process group, a terminal API error — anything short of the
        # worker actually printing `SMOKE: pass`/`SMOKE: fail`) without ever
        # producing a verdict. Mirrors the EVENT_COMPLETION branch above
        # (#1021) but for the terminal-FAILED case that branch never
        # covered: before this, a failed smoke row left the parent's
        # `test_state` at whatever `dispatch_smoke` set it to (almost always
        # `"running"`, #1426) — forever, since no gate ever resolves
        # `"running"` on its own. That is the #1598 incident: a smoke worker
        # died on a terminal API error and the issue was permanently
        # stranded with the board reporting a plausible in-progress state.
        # #1797: `push_failure_reason` is the same column too — see the
        # identical `or` chain in `coord.reconcile.reconcile_completed_assignments`.
        _failure_reason = (
            entry.get("usage_limit_reason")
            or entry.get("api_error_reason")
            or entry.get("push_failure_reason")
        )
        post_failure(
            exit_code=transition.exit_code,
            error=entry.get("error") or _failure_reason or "",
            **common,
        )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
            failure_reason=_failure_reason,
            exit_code=transition.exit_code,
        )
        parent_id = record.get("review_of_assignment_id")
        if parent_id:
            from coord.reconcile import (  # noqa: PLC0415
                propagate_smoke_terminal_failure,
            )
            propagate_smoke_terminal_failure(
                parent_assignment_id=parent_id,
                failure_reason=_failure_reason,
            )
    elif transition.event == EVENT_COMPLETION:
        post_completion(exit_code=transition.exit_code or 0, **common)
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
    elif transition.event == EVENT_ADVISORY:
        # #448: 0-commit clean exit — post a distinctive advisory comment.
        # No ❌ emoji, no re-dispatch suggestion; just surfaces the advisory
        # state on GitHub so operators not watching coord status are informed.
        post_advisory(
            reason=entry.get("zero_commit_reason") or "",
            **common,
        )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
        )
    else:
        # #1605/#1797: carry the agent's own diagnostic (a usage-limit kill,
        # a terminal API-error classification, or an auth-shaped push
        # failure — all stamped by `AgentServer._reap` onto this same
        # `/status` completed entry — see
        # `coord.reconcile.reconcile_completed_assignments`'s identical
        # `or`) through to `mark_notified` so a `status='failed'` row is
        # never left with both `failure_reason` and `exit_code` null. This
        # is the branch a `type="work"` push-auth failure actually hits
        # (none of the type-specific `elif`s above match "work"), so
        # `_failure_reason` also feeds `error=` below — otherwise the
        # posted GitHub failure comment's `error` field is blank for
        # exactly the failure #1797 exists to surface.
        _failure_reason = (
            entry.get("usage_limit_reason")
            or entry.get("api_error_reason")
            or entry.get("push_failure_reason")
        )
        post_failure(
            exit_code=transition.exit_code,
            error=entry.get("error") or _failure_reason or "",
            **common,
        )
        mark_notified(
            transition.assignment_id,
            transition.event,
            branch=entry.get("branch"),
            failure_reason=_failure_reason,
            exit_code=transition.exit_code,
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

            # #bounce: cache the parsed findings so coord bounce + the
            # per-stage display can skip the HTTP fetch on later runs.
            _persist_review_findings(aid, findings.verdict, findings.body)

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

            # #248: same header injection as the live path.
            body_with_header = _attach_review_header(
                findings.body,
                verdict=findings.verdict,
                reviewer_machine=machine.name,
                assignment_id=aid,
            )

            posted = False
            if pr_number is not None:
                try:
                    github_ops.post_pr_review(repo_github, pr_number, findings.verdict, body_with_header + retro_note)
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
                    f"{body_with_header}"
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


def _dispatch_board_pending_smoke(config: Config) -> None:
    """Load the board, dispatch any pending Test-stage smoke, and save.

    #1426: `dispatch_pending_smoke` (:mod:`coord.smoke`) was previously only
    ever called from `reconcile()`'s per-item loop, and the ONLY sanctioned
    caller of the full `reconcile()` is `coord resume`, a human-invoked
    command. A thin-client setup driven purely by `coord-notify.timer` (which
    calls `notify.run()`, not `reconcile()`) never dispatched the Test stage
    at all — the exact gap `scripts/drive-issue.sh` had to paper over with a
    local `scripts/coord-test-runner.sh` subprocess (#1395). Mirrors
    :func:`_dispatch_board_pending_reviews` exactly, and is safe to call even
    when the board file doesn't exist.
    """
    from coord.board_service import read_board, write_board
    from coord.smoke import dispatch_pending_smoke

    board = read_board()
    dispatched = dispatch_pending_smoke(board, config)
    if dispatched:
        write_board(board)


def _dispatch_board_pending_reviews(config: Config) -> None:
    """Load the board, dispatch any pending reviews, and save.

    Mirrors the review-dispatch loop in reconcile() so that ``coord notify``
    also triggers review dispatch — not just ``coord status --reconcile``.
    Safe to call even when the board file doesn't exist.
    """
    from coord.board_service import read_board, write_board
    from coord.review import dispatch_pending_reviews, dispatch_scoped_reviews_for_queue

    # #749: read_board()/write_board() route through the daemon when
    # board_service is configured, so this no longer silently no-ops on a
    # thin client's empty local DB — read_board() falls back to an
    # effectively-empty board when nothing has been saved yet, which is
    # exactly as harmless as the old "return early" guard.
    board = read_board()

    # #465: review fires immediately on work completion — no manual smoke
    # prerequisite.  Mirrors reconcile().  dispatch_pending_reviews() enforces
    # the bulk-dispatch flood guard (per-pass cap + surge gate, incident
    # 2026-06-08) and the #459 active-fix dedupe, so notify can't flood either.
    dispatched = dispatch_pending_reviews(board, config)

    # #1476: same scoped-re-review dispatch reconcile() runs, so a conflict-fix
    # that voids an approval by changing content gets a delta-scoped re-review
    # from `coord notify` too, not just `coord status --reconcile`.
    dispatched = dispatched + dispatch_scoped_reviews_for_queue(board, config)

    if dispatched:
        write_board(board)


def _sweep_stalled_pipeline(
    config: Config, *, terminal_cache: dict | None = None,
) -> list[StalledDetection]:
    """Detect #1441 stalled-pipeline rows, post one comment per row, and —
    when ``config.pipeline.auto_dispatch_stalled`` is on — dispatch the
    action the original transition would have taken (#1478).

    Loads its own board (rather than accepting one) so mutations from a
    dispatched action (a freshly-enqueued merge entry, a newly dispatched
    review/fix/conflict-fix, ``board.review_state`` flips) can be persisted
    back via ``write_board`` — mirrors ``_dispatch_board_pending_reviews``/
    ``_dispatch_board_pending_smoke`` above. A comment-posting failure for
    one row must not stop the sweep from reaching the rest (matches every
    other best-effort loop in this module) — the ``continue`` on failure
    means that row's ``notified`` key is never set, so it is picked back up
    on the next tick rather than silently dropped.

    An unexpected exception *from* ``dispatch_stalled_pipeline_action``
    itself (e.g. a momentarily-unreachable agent during ``dispatch_review``/
    ``dispatch_conflict_fix``) gets the same treatment, not the "declined"
    treatment: no comment is posted and the row is NOT marked notified, so
    it is retried on the next tick rather than permanently foreclosed. A
    considered decline (``no_action`` returned normally — no capable
    machine, gate not satisfied, entry vanished) still posts the diagnostic
    comment and marks notified per the one-shot "act once" guardrail; only a
    genuine raised exception gets the retry treatment.
    """
    from coord.board_service import read_board, write_board

    board = read_board()
    detections = detect_stalled_pipeline(config, board=board, terminal_cache=terminal_cache)

    posted: list[StalledDetection] = []
    board_dirty = False
    for detection, work in detections:
        try:
            action = dispatch_stalled_pipeline_action(
                detection, work, board, config, terminal_cache=terminal_cache,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "dispatch_stalled_pipeline_action: unexpected error for %s — "
                "not marking notified so this row is retried next tick",
                detection.assignment_id,
            )
            continue

        dispatched = action.kind in _STALLED_DISPATCH_KINDS
        try:
            if dispatched:
                post_stalled_pipeline_dispatch(detection, action, config)
            else:
                post_stalled_pipeline(detection, config)
        except Exception:  # noqa: BLE001
            continue
        posted.append(detection)
        if dispatched:
            board_dirty = True

        # #1478 guardrail: "log every auto-dispatch to the audit trail with
        # the detection that triggered it" — business-tier (never dropped
        # by the operational/business audit-level gate) for an actual
        # dispatch; operational-tier for a no-op/skip, so the "nothing
        # happened" rows don't inflate the business audit stream but are
        # still reconstructable when `audit.level` includes operational.
        try:
            from coord.audit import record_audit  # noqa: PLC0415

            record_audit(
                tier="business" if dispatched else "operational",
                category="pipeline",
                event_type="stalled_pipeline_auto_dispatch",
                actor="coordinator",
                summary=(
                    f"stalled-pipeline sweep ({detection.reason}) -> {action.kind} "
                    f"for {detection.repo_name}#{detection.issue_number}"
                ),
                repo=detection.repo_name,
                issue=detection.issue_number,
                assignment_id=detection.assignment_id,
                machine=detection.machine_name,
                details={
                    "stalled_reason": detection.reason,
                    "stalled_detail": detection.detail,
                    "action_kind": action.kind,
                    "action_detail": action.detail,
                },
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "record_audit failed for stalled dispatch %s", detection.assignment_id,
            )

    if board_dirty:
        try:
            write_board(board)
        except Exception:  # noqa: BLE001
            log.exception("write_board failed after stalled-pipeline dispatch")

    return posted


@dataclass(frozen=True)
class DrainResult:
    """What one :func:`run_drain` pass actually did.

    ``skipped_locked`` is the "someone else is draining" outcome, which is a
    success, not an error — the next tick picks the work up.

    ``propagated_verdicts`` (#1663) lists the review assignment IDs whose
    verdict this pass wrote through onto the parent **work** row.  Never
    implies a fix worker was dispatched — the drain cannot dispatch one.
    """

    transitions: list[Transition] = field(default_factory=list)
    orphaned_findings: list[str] = field(default_factory=list)
    propagated_verdicts: list[str] = field(default_factory=list)
    skipped_locked: bool = False

    def __bool__(self) -> bool:
        """Truthy when this pass advanced something (for terse log guards)."""
        return bool(
            self.transitions or self.orphaned_findings or self.propagated_verdicts
        )


def run_drain(
    config: Config,
    *,
    lock_path: "Path | None" = None,
    lock_timeout: float = 0.0,
) -> DrainResult:
    """The pipeline's **clock** (#1616) — advance terminal rows' side effects.

    ``reconcile_completed_assignments`` (the daemon's passive tick) writes
    ``status='done'`` and stops there, by contract.  Everything downstream —
    ``finished_at``, the completion comment, the #1076/#1152 test-gate
    backfill, the Test-stage smoke dispatch, the review dispatch, the #1610
    ``finalizing`` → verdict capture — is a side effect of ``coord notify``.
    On this fleet ``coord-notify.timer`` is deliberately disabled and the only
    caller of ``coord notify`` is a live ``coord drive``'s **stall nudge**, so
    a completed stage sat until the stall detector gave up (9 min on #1123,
    47 min on #1122) — and rows with no drive at all (vimcode#611/#613) sat
    until a human poked the daemon.  This function is what the daemon tick
    calls so the pipeline advances on a clock instead of on an accident.

    **Scope is the whole point — this is deliberately NOT ``run()``.**
    ``coord notify`` triggers five side effects; four are bookkeeping with no
    race and no cost if repeated, and one spawns a metered worker.  The line
    sits at exactly one place:

    ==========================================  =======  ===================================
    side effect                                 here?    why
    ==========================================  =======  ===================================
    ``finished_at`` stamped                     yes      no race, no cost
    completion comment posted                   yes      ``coord:`` markers make it idempotent
    test-gate backfill (#1076/#1152)            yes      no race, no cost
    Test-stage smoke dispatch (#1426)           yes      the gate review waits on; see below
    orphaned review findings posted             yes      comment + verdict capture only
    review dispatch                             yes      guarded; see below
    verdict → parent work row (#1663)           yes      no race, no cost; see below
    merge enqueue                               n/a      the daemon tick already runs
                                                         ``enqueue_approved_work`` right after
    **work dispatch**                           **no**   stays with a drive or a human
    **fix-round dispatch** (``auto_loop``)      **no**   this is where #476/#477 lives
    **stalled-pipeline sweep/dispatch**         **no**   can dispatch work (#1478)
    ==========================================  =======  ===================================

    #1663 is what the "verdict → parent work row" row costs to learn.  That
    write — ``work.review_state='done'``, ``work.review_verdict=<verdict>``,
    ``record_work_review_verdict``, the merge-queue refresh — is bookkeeping by
    every criterion in this table, but it lived *inside*
    ``auto_loop.process_review_completion`` alongside the fix dispatch, and
    excluding the function excluded both.  So every verdict the daemon consumed
    instead of a human's ``coord notify`` was captured on the review row and
    dropped on the way to the work row, for **both** verdicts — the approve
    case stayed invisible only because ``merge_queue.has_approved_review``
    reads the *review* row.  ``coord drive``, the TUI's Review stage and the
    auto-loop all read the *work* row, so an approved issue simply stopped:
    2026-08-01's overnight batch reviewed five issues clean and merged none of
    them in 4h02m.  The propagation half is now separately callable
    (``auto_loop.propagate_review_verdict_for_transition``) and step 5 calls
    only that; fix dispatch is as unreachable from here as it ever was.

    Why review dispatch is in and fix dispatch is out — the asymmetry is the
    whole argument.  #476/#477, the incident that got ``coord-notify.timer``
    disabled, was duplicate **fix-workers**: they create conflicting branches
    on the same issue and cost real recovery work.  A duplicate *review* costs
    a few dollars and a redundant comment.  Withholding reviews inherits a
    mitigation for a risk that does not apply to them.  And bookkeeping-only
    is not sufficient: work→review is the most frequent boundary in the
    pipeline and the one that stalled #1122, so a drain that stamps state but
    will not dispatch reviews fixes the *watched* half and leaves the
    unwatched half exactly as broken as before.

    Smoke dispatch rides along because ``dispatch_pending_reviews`` holds
    review dispatch until ``test_state`` is passed/skipped when
    ``pipeline.test_precedes_review()`` (#1612).  Draining reviews without
    ever dispatching the Test stage would just move the stall one box left —
    that is #1605.  It is a Test-stage worker on the work's own branch, not a
    second author on a fresh branch, so it carries none of the #476/#477
    shape.

    Stuck / needs-attention detection is deliberately absent: those are
    *notifications*, not pipeline advancement, and giving the daemon a
    periodic detector is #1632's job (which is blocked on this).

    **Concurrency.**  The whole pass runs under ``~/.coord/notify.lock`` —
    literally :class:`coord.filelock.FileLock`, the same class on the same
    path ``coord drive``'s ``run_notify()`` takes — so a drive's nudge and the
    daemon's clock can never both be inside ``dispatch_pending_reviews``,
    which reads ``review_state == 'pending'`` and writes ``'dispatched'``
    non-atomically (two concurrent passes would both see ``pending`` and
    dispatch two reviews).  ``lock_timeout`` defaults to **0.0**
    (non-blocking): if another drain holds it, return ``skipped_locked`` and
    let the next tick retry rather than pinning a threadpool worker.

    Every step is independently try/except'd — one failing side effect must
    never sink the rest of the pass, and a drain must never crash the daemon.
    """
    from coord.filelock import FileLock, LockBusy, notify_lock_path  # noqa: PLC0415

    lock = FileLock(lock_path if lock_path is not None else notify_lock_path())
    try:
        lock.acquire(timeout=lock_timeout)
    except LockBusy:
        log.debug("notify drain: %s held elsewhere — skipping this pass", lock.path)
        return DrainResult(skipped_locked=True)
    try:
        return _run_drain_locked(config)
    finally:
        lock.release()


def _run_drain_locked(config: Config) -> DrainResult:
    """:func:`run_drain`'s body, with the lock already held.

    Split out so tests can exercise the side effects without the lock and the
    lock without the side effects.
    """
    # Refresh the agent-host cache so _try_parse_and_post_review (and any other
    # helper using _agent_host) can resolve hostnames without threading config
    # through every call.  Mirrors run().
    global _AGENT_HOSTS
    _AGENT_HOSTS = {m.name: m.host for m in config.machines}

    # Step 1: post completion/failure/advisory/plan/review comments for rows
    # the agent reports terminal.  This is what stamps `finished_at` (via
    # mark_notified) and captures cost / SMOKE_TESTS / summary / session id /
    # the review verdict + findings.  Idempotent: detect_transitions skips any
    # assignment already in the `notifications` table, so a second drain over
    # the same board posts nothing.
    posted: list[Transition] = []
    # #1663: (transition, record, entry) for every review that completed in
    # THIS pass, so step 5 can propagate its verdict onto the parent work row.
    review_completions: list[tuple[Transition, dict, dict]] = []
    try:
        from coord.comments import EVENT_COMPLETION  # noqa: PLC0415

        for transition, record, entry in detect_transitions(config):
            try:
                post_transition(transition, record, entry)
            except Exception:  # noqa: BLE001 — one bad row must not sink the pass
                log.exception(
                    "notify drain: post_transition failed for %s",
                    transition.assignment_id,
                )
                continue
            posted.append(transition)
            if (
                record.get("type") == "review"
                and transition.event == EVENT_COMPLETION
            ):
                review_completions.append((transition, record, entry))
    except Exception:  # noqa: BLE001
        log.exception("notify drain: detect_transitions failed")

    # Step 2: dispatch pending Test-stage smoke (#1426).  Runs BEFORE review
    # dispatch to mirror the pipeline's Work -> Test -> Review order.
    try:
        _dispatch_board_pending_smoke(config)
    except Exception:  # noqa: BLE001
        log.exception("notify drain: smoke dispatch failed")

    # Step 3: dispatch pending reviews.  Carries the #1612 test-precedes-review
    # gate, the #1076/#1152 test-gate backfill, the #946 enqueue gate, the
    # 2026-06-08 flood guard (per-pass cap + surge gate) and the #459 active-fix
    # dedupe — this is calling existing machinery from a clock, not new
    # machinery.
    try:
        _dispatch_board_pending_reviews(config)
    except Exception:  # noqa: BLE001
        log.exception("notify drain: review dispatch failed")

    # Step 4: post findings for done-review assignments that were never
    # processed (agent reported 'cancelled', a human marked the row done, or
    # notify ran at the wrong time).  Comment + verdict capture only.
    orphaned: list[str] = []
    try:
        orphaned = post_orphaned_review_findings(config) or []
    except Exception:  # noqa: BLE001
        log.exception("notify drain: post_orphaned_review_findings failed")

    # Step 5 (#1663): propagate each captured verdict onto its parent WORK row.
    #
    # Steps 1 and 4 both stamp the verdict on the *review* row and stop there.
    # Everything that reads the *work* row — `coord drive`, the TUI's Review
    # stage, `_stalled_pipeline`, any state-derived recovery — therefore saw
    # `review_state='dispatched'` / `review_verdict=NULL` for every verdict the
    # daemon consumed instead of a human's `coord notify`.  The 2026-08-01
    # overnight batch is the receipt: five issues reviewed, four clean approves,
    # not one reached its work row, 4h02m of wall clock and zero merges.
    #
    # This is the bookkeeping half ONLY — `propagate_review_verdict_for_
    # transition` cannot reach `_dispatch_fix_for_review`, so the #476/#477
    # line (no metered fix worker from a clock) is exactly where it was.  The
    # exclusion used to sit at function granularity and took the parent-row
    # write down with the dispatch; it now sits at side-effect granularity,
    # which is where the table above always said it belonged.
    _propagated: list[str] = []
    if review_completions or orphaned:
        try:
            from coord.auto_loop import (  # noqa: PLC0415
                propagate_review_verdict_for_transition,
            )

            seen: set[str] = set()
            # Orphaned rows have no transition tuple (their comment was posted
            # on an earlier pass, or never).  `_load_review_findings` reads the
            # DB findings cache first — which step 4 just populated — so an
            # empty record/entry still resolves the verdict without any I/O.
            pending: list[tuple[str, dict, dict]] = [
                (t.assignment_id, record, entry)
                for t, record, entry in review_completions
            ] + [(aid, {"type": "review"}, {}) for aid in orphaned]

            for aid, record, entry in pending:
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                try:
                    actions = propagate_review_verdict_for_transition(
                        aid, record, entry, config,
                    )
                except Exception:  # noqa: BLE001 — never sink the pass
                    log.exception(
                        "notify drain: verdict propagation failed for %s", aid,
                    )
                    continue
                for action in actions:
                    log.info(
                        "notify drain: verdict propagation %s: %s (assignment=%s)",
                        action.kind, action.detail, action.assignment_id,
                    )
                    if action.kind in (
                        "approved", "approved_with_nits", "verdict_propagated",
                        "terminal_skip",
                    ):
                        _propagated.append(aid)
        except Exception:  # noqa: BLE001
            log.exception("notify drain: verdict propagation loop failed")

    return DrainResult(
        transitions=posted,
        orphaned_findings=orphaned,
        propagated_verdicts=_propagated,
    )


def run(
    config: Config,
) -> tuple[
    list[Transition],
    list[StuckDetection],
    list[NeedsAttentionDetection],
    list[StalledDetection],
]:
    """Detect and post all pending transitions, stuck signals, #846
    needs-attention detections, and #1441 stalled-pipeline detections.

    Also dispatches any pending reviews found on the saved board so that
    ``coord notify`` acts as a reliable review-dispatch trigger in addition
    to ``coord status --reconcile``.

    Returns (posted_transitions, posted_stuck, posted_needs_attention,
    posted_stalled). The stalled entry is new in #1441 — appended rather than
    inserted, so any existing caller unpacking a 3-tuple positionally would
    break loudly (a good thing: it means the CLI/board/TUI surfacing this
    issue asks for was actually wired up, not silently skipped).
    """
    # Refresh the agent-host cache so _try_parse_and_post_review (and any
    # other helper using _agent_host) can resolve hostnames without
    # threading config through every call.
    global _AGENT_HOSTS
    _AGENT_HOSTS = {m.name: m.host for m in config.machines}

    # #522: one terminal-state cache shared across every gh-hitting check in
    # this notify run (the auto-loop review/fix dispatches below, and the
    # #1441 stalled-pipeline sweep at the end), so a burst of activity for
    # the same merged/closed issue (the #349 ×4 case) costs a single `gh`
    # round-trip, not one per caller.
    terminal_cache: dict = {}

    # Collect (transition, record, entry) tuples for review completions so we
    # can feed them to the auto-loop after all notifications are posted.
    review_completions: list[tuple[Transition, dict, dict]] = []
    # Collect (transition, record) tuples for completed fix workers so we can
    # dispatch a fresh review against each one after notifications are posted.
    fix_completions: list[tuple[Transition, dict]] = []

    posted: list[Transition] = []
    for transition, record, entry in detect_transitions(config):
        try:
            post_transition(transition, record, entry)
        except Exception:  # noqa: BLE001 — surface to caller; continue with rest
            continue
        posted.append(transition)
        # Track completed reviews for auto-loop processing below.
        from coord.comments import EVENT_COMPLETION  # noqa: PLC0415
        from coord.auto_loop import FIX_DISPATCH_TYPES  # noqa: PLC0415
        if (
            record.get("type") == "review"
            and transition.event == EVENT_COMPLETION
        ):
            review_completions.append((transition, record, entry))
        # Track completed fix workers (type in FIX_DISPATCH_TYPES,
        # review_of_assignment_id set, title starts with "[fix-") for
        # auto-loop re-review dispatch. #1176 review: this used to hardcode
        # type == "work", which meant a completed type="test-author" fix
        # (added by #1176 itself) never reached run_for_fix_transition —
        # the same class of bug as #1141 ("test-author was never added to
        # WORK_LIKE_TYPES"). FIX_DISPATCH_TYPES is the single source of
        # truth for what _dispatch_fix can emit, so a future fix-dispatch
        # type can't reintroduce this gap silently.
        elif (
            record.get("type") in FIX_DISPATCH_TYPES
            and transition.event == EVENT_COMPLETION
            and record.get("review_of_assignment_id")
            and (record.get("issue_title") or "").startswith("[fix-")
        ):
            fix_completions.append((transition, record))

    # Also detect and post stuck signals
    stuck_posted: list[StuckDetection] = []
    for detection, record in detect_stuck(config):
        try:
            post_stuck(detection, record)
        except Exception:  # noqa: BLE001
            continue
        stuck_posted.append(detection)

    # #846: coordinator backstop for long-running / non-converging
    # assignments. Best-effort, non-fatal — one bad record must not sink the
    # rest of the notify run.
    needs_attention_posted: list[NeedsAttentionDetection] = []
    try:
        for detection, record in detect_needs_attention(config):
            try:
                post_needs_attention(detection, record)
            except Exception:  # noqa: BLE001
                continue
            needs_attention_posted.append(detection)
    except Exception:  # noqa: BLE001
        log.exception("detect_needs_attention: unexpected error")

    # Dispatch pending Test-stage smoke from the saved board (#1426;
    # best-effort, non-fatal). Runs BEFORE review dispatch to mirror the
    # pipeline's Work -> Test -> Review order, though ordering isn't load-
    # bearing here: dispatch_pending_reviews already holds review dispatch
    # until test_state is passed/skipped regardless of which runs first in
    # a given pass.
    try:
        _dispatch_board_pending_smoke(config)
    except Exception:  # noqa: BLE001
        pass

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
                        transition.assignment_id, record, entry, config,
                        terminal_cache=terminal_cache,
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

    # Auto-loop: for each completed fix worker, dispatch a fresh review so
    # the review → fix → re-review cycle closes without manual coord pr invocations.
    # Runs after review_completions so a simultaneous review + fix completion
    # in the same notify run is handled review-first.
    if fix_completions:
        try:
            from coord.auto_loop import run_for_fix_transition  # noqa: PLC0415
            for transition, _record in fix_completions:
                try:
                    actions = run_for_fix_transition(
                        transition.assignment_id, config,
                        terminal_cache=terminal_cache,
                    )
                    for action in actions:
                        log.info(
                            "auto_loop fix_transition %s: %s (assignment=%s)",
                            action.kind, action.detail, action.assignment_id,
                        )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "auto_loop: error processing fix completion %s",
                        transition.assignment_id,
                    )
        except Exception:  # noqa: BLE001
            log.exception("auto_loop: unexpected error in fix completion loop")

    # #1441/#1478: sweep for pipeline rows whose auto-loop transition
    # already fired once but which are now stuck on a precondition that
    # landed too late (the vimcode #602 reference case), post a diagnostic
    # (or, when `pipeline.auto_dispatch_stalled` is on, act). Runs last,
    # after the review/fix auto-loop above has had a chance to act on THIS
    # pass's transitions, so a row that just got a fresh fix/review
    # dispatched above is not also flagged as stalled in the same pass.
    # Best-effort, non-fatal — mirrors the #846 needs-attention block above;
    # the crucial difference from `reconcile()`-only sweepers (see
    # docs/OPERATING_GOTCHAS.md §7) is that this runs from `coord notify`.
    stalled_posted: list[StalledDetection] = []
    try:
        stalled_posted = _sweep_stalled_pipeline(config, terminal_cache=terminal_cache)
    except Exception:  # noqa: BLE001
        log.exception("detect_stalled_pipeline: unexpected error")

    return posted, stuck_posted, needs_attention_posted, stalled_posted
