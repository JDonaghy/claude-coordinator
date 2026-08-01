"""Merge queue: sequence completed assignments into their target branches.

Two-layer design so the logic is testable without hitting `gh`:

- Data + sequencing live here (pure functions over QueuedMerge).
- Wire calls (gh pr create / merge / size) are passed in as `gh_ops` so
  tests can substitute a stub. `coord.cli` wires the real `coord.github_ops`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from coord.audit import record_audit
from coord.ci_store import (
    CiCheckSummary,
    CiStore,
    NoOpCi,
    failed_checks,
    in_flight_checks,
    summarize,
    summarize_counts,
)
from coord.db import get_connection
from coord.models import CLOSES_ISSUE_TYPES, WORK_LIKE_TYPES, Assignment
from coord.pr_body_lint import downgrade_closing_keywords, find_closing_references
from coord.state import COORD_DIR

_log = logging.getLogger(__name__)

# Legacy path constant — kept for backward compat with monkeypatch calls in tests.
QUEUE_FILE = COORD_DIR / "merge_queue.json"

# States
PENDING = "pending"
MERGING = "merging"
MERGED = "merged"
CONFLICT = "conflict"
SKIPPED = "skipped"
# Set on a merge entry whose conflict-fix attempt also failed — the user must
# resolve the conflict by hand.  See #241.
HUMAN_REQUIRED = "human_required"


# ── Conflict classification ─────────────────────────────────────────────────

# #1467: the specific subset of GitHub wording that means a --rebase merge
# was refused purely because the branch contains a merge commit — a
# *linearity* failure, not a content conflict. This distinction matters for
# reconcile_conflict_entries: GitHub's `mergeable` field (what
# check_pr_mergeable reads) only reflects content conflicts and happily
# reports MERGEABLE for a branch that is clean but not rebase-able, so a
# plain mergeable check is not evidence that a retried --rebase will
# succeed. See is_rebase_refusal(). Defined once here and folded into
# _REBASEABLE_SIGNALS below so the two lists can't drift apart.
_REBASE_REFUSAL_SIGNALS = (
    "can't be rebased",
    "cannot be rebased",
)

_REBASEABLE_SIGNALS = (
    "could not be rebased",
    # #1467: GitHub's actual wording when a branch contains a merge commit
    # — distinct from "could not be rebased" above (which never matched it)
    # and previously fell through to "unknown", so #241's conflict-fix
    # worker was never dispatched and the entry parked forever. A local
    # `git rebase origin/main` linearises the branch, which is exactly what
    # the dispatched conflict-fix worker attempts.
    *_REBASE_REFUSAL_SIGNALS,
    "merge conflict",
    "not up to date",
    "non-fast-forward",
    "behind the base branch",
    # `gh pr merge` returns this when the PR is behind base and a rebase
    # would be needed.  Common on PRs that sat open while main moved.
    "merge commit cannot be cleanly created",
    "not mergeable",
)

_HUMAN_SIGNALS = (
    "required status check",
    "review required",
    "permission",
    "protected branch",
    "branch protection",
)


def classify_conflict(error: str | None) -> str:
    """Decide what kind of merge failure ``error`` represents.

    Returns ``"rebaseable"`` (a mechanical rebase conflict an agent can
    attempt), ``"human"`` (permission / branch protection — surface to the
    user), or ``"unknown"`` (don't auto-dispatch; let the user inspect).

    Used by ``coord merge`` (#241) to decide whether to spawn a
    ``type="conflict-fix"`` assignment or surface the failure as-is.
    """
    if not error:
        return "unknown"
    text = error.lower()
    if any(sig in text for sig in _HUMAN_SIGNALS):
        return "human"
    if any(sig in text for sig in _REBASEABLE_SIGNALS):
        return "rebaseable"
    return "unknown"


def is_rebase_refusal(error: str | None) -> bool:
    """True when ``error`` is specifically GitHub's "branch can't be
    rebased" refusal — a merge commit on the branch, not a content
    conflict (#1467).

    Narrower than ``classify_conflict(error) == "rebaseable"``, which also
    matches ordinary content conflicts ("merge conflict", "not mergeable",
    …) that GitHub's own ``mergeable`` field already reports accurately.
    This predicate isolates the one failure mode where ``mergeable:
    MERGEABLE`` is *not* proof a retried ``--rebase`` will succeed, so
    :func:`reconcile_conflict_entries` and the ``coord merge`` CLI can treat
    it differently from a plain conflict.
    """
    if not error:
        return False
    text = error.lower()
    return any(sig in text for sig in _REBASE_REFUSAL_SIGNALS)


# ── Work-chain resolution (#567) ────────────────────────────────────────────

def _chain_work_ids(entry: "QueuedMerge", pool: list) -> set[str]:
    """Collect every work-assignment id connected to *entry*: by branch
    equality (pre-#567 behaviour) **or** by the ``review_of_assignment_id``
    linkage a bounce-fix worker records back to the assignment it fixes.

    #567: a fix worker dispatched under the #557 remote-interactive-rework
    gap has ``branch=NULL``, so it never matches ``branch == entry.branch``
    and a verdict recorded on it is invisible to ``has_approved_review`` /
    ``has_smoke_verdict``. Every ``WORK_LIKE_TYPES`` assignment dispatched as
    a fix records ``review_of_assignment_id`` pointing at the assignment it
    fixes (``auto_loop.py`` fix dispatch), so the chain is reconstructable
    without a branch match. Expansion runs to a fixed point so multi-hop
    bounce chains (a fix of a fix) are fully covered, not just one hop.

    #1601: the walk used to be forward-only — a known PARENT pulled in its
    CHILD (the fix round), but not the reverse. An entry keyed to the child
    (e.g. the fix round's own approved re-review, per #292 Defect 2's
    re-keying) could not walk *backward* to reach the parent's still-useful
    fields (its ``test_state``/``smoke_test`` verdict, when the fix round
    never re-ran one) whenever branch equality alone didn't already bridge
    the two — the same ``branch=NULL`` gap #567 fixed for the forward
    direction. The expansion is now symmetric: a known row pulls in both its
    recorded children AND its own ``review_of_assignment_id`` parent.
    """
    work_ids: set[str] = set()
    if entry.assignment_id:
        work_ids.add(entry.assignment_id)

    work_assignments = [a for a in pool if getattr(a, "type", None) in WORK_LIKE_TYPES]

    # Branch equality — the original (#292) expansion.
    for a in work_assignments:
        aid = getattr(a, "assignment_id", None)
        branch = getattr(a, "branch", None)
        if aid and branch and branch == entry.branch:
            work_ids.add(aid)

    # review_of_assignment_id chain — covers fix workers with branch=NULL,
    # and multi-iteration bounce chains via a fixed-point expansion. Runs in
    # BOTH directions (#1601) so the chain is the same set regardless of
    # which round in it the entry happens to be keyed to.
    changed = True
    while changed:
        changed = False
        for a in work_assignments:
            aid = getattr(a, "assignment_id", None)
            parent = getattr(a, "review_of_assignment_id", None)
            # Forward: a known parent pulls in its child.
            if aid and parent in work_ids and aid not in work_ids:
                work_ids.add(aid)
                changed = True
            # Backward (#1601): a known child pulls in its own parent.
            if aid and aid in work_ids and parent and parent not in work_ids:
                work_ids.add(parent)
                changed = True

    return work_ids


# ── Branch winner resolution (#1490) ────────────────────────────────────────
#
# A fix/bounce cycle dispatches a fresh WORK_LIKE_TYPES assignment for every
# retry, and every one of them keeps its row in `board.completed` forever —
# all targeting the same branch. `enqueue_approved_work` (the daemon tick)
# and `coord merge`'s own auto-enqueue scan both used to process every such
# row independently and hand each one to `refresh_entry_assignment`, which
# re-keys the ONE queue row that exists for the branch to whichever
# assignment_id it was just called with. Processing three rows on one
# branch in a single pass therefore re-keyed the same entry three times in
# a row and printed three "auto-enqueued" lines for what is — and always
# was — a single queue entry; because the gates
# (`passes_merge_gates`/`has_approved_review`/`has_smoke_verdict`) are
# resolved over the whole branch chain rather than the specific row passed
# in, even the row with a *failed* test_state would pass the gate and win a
# later iteration's re-key, so the "current" key flip-flopped across every
# row on every single tick, forever (#1490's observed bug).
#
# The fix: resolve every branch to a single winner *before* touching the
# queue at all, and never enqueue (or re-announce) the other rows.


def _select_winning_work_assignment(work_assignments: list) -> "Assignment":
    """Pick the one row in *work_assignments* — all sharing one branch —
    that should key the branch's merge-queue entry.

    Prefers the most-recently-dispatched row that already carries a fresh
    terminal smoke verdict (``test_state in ('passed', 'skipped')``) — the
    "approved + test-passed" row the issue asks the queue entry to track.
    Falls back to the most-recently-dispatched row overall when none has
    passed yet (the branch is still mid-cycle; it should still enqueue —
    blocked on the smoke gate — rather than vanish). Ties on
    ``dispatched_at`` (including everything being ``None``, e.g. rows from
    tests or pre-#821 data) resolve to the last one in *work_assignments*
    (typically ``board.completed`` insertion order, i.e. the most recently
    seen row), same tie-break convention as :func:`resolve_entry_key`.
    """
    def _dispatched_at(a) -> float:
        return getattr(a, "dispatched_at", None) or 0

    passed = [
        a for a in work_assignments
        if getattr(a, "test_state", None) in ("passed", "skipped")
    ]
    pool = passed if passed else work_assignments
    winner = pool[0]
    for a in pool[1:]:
        if _dispatched_at(a) >= _dispatched_at(winner):
            winner = a
    return winner


def group_branch_candidates(completed: Iterable) -> list[tuple["Assignment", list]]:
    """Group every done :data:`~coord.models.WORK_LIKE_TYPES` assignment in
    *completed* by ``(repo_name, branch)`` and resolve each group to a
    single winner (#1490).

    Returns one ``(winner, superseded)`` pair per distinct ``(repo_name,
    branch)`` group, in first-seen order (stable — output doesn't jitter
    run to run). ``superseded`` holds the group's other rows (``[]`` when
    there was only one); callers must log them and never enqueue them —
    see :func:`_select_winning_work_assignment` for how the winner is
    chosen.

    Rows missing ``branch``/``assignment_id``, not in ``WORK_LIKE_TYPES``,
    or not ``status == "done"`` are dropped from consideration entirely —
    the same ad-hoc filter both call sites (`enqueue_approved_work`, the
    ``coord merge`` auto-enqueue scan) applied before this was extracted.
    """
    order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], list] = {}
    for a in completed:
        if getattr(a, "type", None) not in WORK_LIKE_TYPES:
            continue
        if getattr(a, "status", None) != "done":
            continue
        branch = getattr(a, "branch", None)
        aid = getattr(a, "assignment_id", None)
        if not branch or not aid:
            continue
        key = (getattr(a, "repo_name", None), branch)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(a)

    result: list[tuple["Assignment", list]] = []
    for key in order:
        rows = groups[key]
        winner = rows[0] if len(rows) == 1 else _select_winning_work_assignment(rows)
        superseded = [r for r in rows if r is not winner]
        result.append((winner, superseded))
    return result


def _log_superseded(row) -> None:
    """One clear line per row a branch-winner scan skipped (#1490) — so
    "three rows, one queue entry" reads as expected coalescing rather than
    "two got lost"."""
    _log.info(
        "merge-queue: %s#%s assignment %s (branch %s) superseded on this "
        "branch — not enqueued",
        getattr(row, "repo_name", None),
        getattr(row, "issue_number", None),
        getattr(row, "assignment_id", None),
        getattr(row, "branch", None),
    )


# ── Review gate (#253) ──────────────────────────────────────────────────────

def requires_review(entry: "QueuedMerge", config) -> bool:
    """True when *entry* must have an approved review before merging.

    Honours ``config.reviews.enabled`` (the master switch for the
    adversarial review feature) and the *effective* gate list: ``entry``'s
    own ``required_gates`` when set, falling back to
    ``config.pipeline.default_gates`` otherwise (#1213).  ``entry`` is
    duck-typed — both ``QueuedMerge`` (``required_gates`` snapshotted at
    :func:`enqueue` time, commit-bound) and ``Assignment`` (``required_gates``
    resolved from ``config.pipeline.labels`` at dispatch time, see
    :func:`coord.brain.resolve_required_gates`) carry the attribute, so
    ``coord.merge_queue.plan`` can pass either.  Untagged work — the entry
    has no override — behaves exactly as before this change: the default
    policy applies. Explicit-only overrides (``--skip-review``) remain
    available as a manual escape hatch on top of this.
    """
    if not getattr(config, "reviews", None) or not config.reviews.enabled:
        return False
    pipeline = getattr(config, "pipeline", None)
    if pipeline is None:
        return True
    gates = getattr(entry, "required_gates", None) or (pipeline.default_gates or [])
    return "review" in gates


def _backfill_branch_patch_id(entry: "QueuedMerge", gh_ops: "GhOps | None") -> str | None:
    """Return ``entry.branch_patch_id``, computing and persisting it via
    *gh_ops* when null, or ``None`` when it can't be determined.

    #1506: ``entry.branch_patch_id`` is normally populated by :func:`process`
    before the review/smoke gates run, but any entry that reaches
    :func:`has_approved_review` / :func:`find_scoped_review_candidate`
    without having gone through that backfill first — most notably every
    queue row whose approved review predates #1475, which never got a
    chance to backfill it — has ``branch_patch_id: None`` forever, and a
    null there previously meant "cannot prove identical", voiding an
    approval for a diff that had not changed by one byte.

    The base passed is *entry.target_branch* — a branch **name**, resolved
    by GitHub's three-dot compare API (:func:`coord.github_ops.
    get_branch_patch_id`) to the true merge-base of the two refs — never the
    PR's recorded ``baseRefOid``. Using ``baseRefOid`` produces a false
    mismatch once the base branch has advanced past the PR's original fork
    point (#1506's investigation hit exactly this).

    ``gh_ops=None`` (no client available) or a missing repo/base/branch on
    *entry* returns ``None`` without any I/O — callers fail closed exactly as
    before. A successful computation is written back onto *entry* so the
    ``gh api compare`` round trip happens at most once per entry; the caller
    is responsible for persisting the entry (e.g. ``save_queue``) same as
    the existing ``branch_head_sha``/``branch_patch_id`` backfills in
    :func:`process`.
    """
    if gh_ops is None:
        return None
    repo = getattr(entry, "repo_github", None)
    base = getattr(entry, "target_branch", None)
    branch = getattr(entry, "branch", None)
    if not repo or not base or not branch:
        return None
    try:
        computed = gh_ops.get_branch_patch_id(repo, base, branch)
    except Exception:  # noqa: BLE001 — fail-safe: unknown patch-id is not blocking
        return None
    if computed is not None:
        try:
            entry.branch_patch_id = computed
        except Exception:  # noqa: BLE001 — best effort; a read-only entry just recomputes next time
            pass
    return computed


def has_approved_review(
    entry: "QueuedMerge", board, gh_ops: "GhOps | None" = None
) -> bool:
    """True when a completed review with ``review_verdict='approve'`` exists
    on *board* for the work assignment behind *entry*.

    Scans both active and completed assignments — a review whose findings
    were just posted may still be on ``board.active`` for a tick before
    reconcile moves it to ``completed``.  We accept either, since the
    verdict is what matters.

    #292 (Defect 1): after a review bounce the queue entry may be keyed to
    the *original* work assignment while the approved re-review is linked to
    the *fix* work assignment.  To handle this we collect **all** work
    assignment IDs connected to the entry — by shared branch, or (#567) by
    the ``review_of_assignment_id`` chain, which also catches fix workers
    dispatched with ``branch=NULL`` — and accept any approved review that
    points to any of them.

    #1475: a SHA mismatch alone no longer voids the approval outright. When
    the branch's current content-addressed patch-id (``branch_patch_id``)
    matches the patch-id captured at review time (``review_patch_id``), the
    SHA moved but the diff didn't — e.g. a conflict-fix rebase that resolved
    cleanly — so the approval still covers this content. Missing either
    patch-id fails closed to the pre-#1475 behaviour (stale, re-review) —
    UNLESS *gh_ops* is supplied, in which case a null ``branch_patch_id`` is
    computed on demand (#1506) rather than treated as an unrecoverable
    mismatch; see :func:`_backfill_branch_patch_id`.
    """
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])

    branch_work_ids = _chain_work_ids(entry, pool)

    if not branch_work_ids:
        return False

    # #821: commit-bound check.  If the entry has a branch_head_sha (set at
    # process() time from the live branch tip) and the review has a
    # review_head_sha (set when the review assignment ran), an approval only
    # counts when the two SHAs match — i.e. no new commits were pushed after
    # the review completed.  When either SHA is absent (pre-821 rows or SHA
    # tracking unavailable) the check is skipped (backward-compatible).
    current_sha = getattr(entry, "branch_head_sha", None)
    current_patch_id = getattr(entry, "branch_patch_id", None)
    patch_id_attempted = current_patch_id is not None

    for a in pool:
        if getattr(a, "type", None) != "review":
            continue
        if getattr(a, "review_of_assignment_id", None) not in branch_work_ids:
            continue
        if getattr(a, "review_verdict", None) != "approve":
            continue
        review_sha = getattr(a, "review_head_sha", None)
        if review_sha is not None and current_sha is not None and review_sha != current_sha:
            # #1475: the SHA moved — before declaring the approval stale,
            # check whether the underlying content is identical via
            # patch-id. A pure rebase (no conflict) replays the identical
            # diff against a new base and produces the same patch-id even
            # though the commit SHA changed; a conflict resolution or a
            # genuine content change produces a different one. Fail closed
            # when either patch-id is unavailable.
            review_patch_id = getattr(a, "review_patch_id", None)
            if review_patch_id is not None:
                if current_patch_id is None and not patch_id_attempted:
                    # #1506: compute-once, not fail-closed-forever.
                    current_patch_id = _backfill_branch_patch_id(entry, gh_ops)
                    patch_id_attempted = True
                if current_patch_id is not None and review_patch_id == current_patch_id:
                    return True  # content-identical rebase — approval still covers it
            continue  # stale: branch moved past the commit the review covered
        return True
    return False


def find_scoped_review_candidate(
    entry: "QueuedMerge", board, gh_ops: "GhOps | None" = None
) -> Assignment | None:
    """Return the previously-approved review whose approval was voided
    ONLY by a content-changing rebase (#1476), or ``None``.

    Mirrors :func:`has_approved_review`'s SHA/patch-id staleness walk but
    returns the review :class:`~coord.models.Assignment` itself (not a
    bool) — a scoped re-review needs the prior review's ``review_head_sha``
    (the base to diff the resolution from) and ``briefing``/findings as
    established context, not just a yes/no.

    Returns ``None`` — meaning "not this path, fall back to a full review"
    — when:

    - No approved review exists for *entry*'s work chain at all.
    - The branch's current SHA isn't known (can't confirm anything changed),
      or the current patch-id isn't known and can't be computed (#1506: when
      *gh_ops* is supplied, a null ``branch_patch_id`` is backfilled on
      demand via :func:`_backfill_branch_patch_id` instead of failing
      immediately).
    - The most-recently-matched approved review's SHA still matches the
      current one (nothing changed — not stale at all).
    - Its patch-id still matches the current one (content-identical
      rebase — :func:`has_approved_review` already carries this forward,
      there is no delta to scope a review around).
    - Either patch-id is missing (fail closed, same posture as
      ``has_approved_review``: an unconfirmable diff gets a full review,
      never a guessed-at scoped one).
    """
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    branch_work_ids = _chain_work_ids(entry, pool)
    if not branch_work_ids:
        return None

    current_sha = getattr(entry, "branch_head_sha", None)
    current_patch_id = getattr(entry, "branch_patch_id", None)
    if current_patch_id is None:
        current_patch_id = _backfill_branch_patch_id(entry, gh_ops)
    if current_sha is None or current_patch_id is None:
        return None

    # Walk most-recently-dispatched first so a branch that's been through
    # more than one review-then-rebase cycle picks its latest approval as
    # the diff base, not an older one — a stale pick still produces a safe
    # (over-inclusive, never under-inclusive) delta, but a needlessly large
    # one. ``pool`` is otherwise unordered (completed + active concatenated).
    ordered = sorted(pool, key=lambda a: getattr(a, "dispatched_at", None) or 0, reverse=True)

    for a in ordered:
        if getattr(a, "type", None) != "review":
            continue
        if getattr(a, "review_of_assignment_id", None) not in branch_work_ids:
            continue
        if getattr(a, "review_verdict", None) != "approve":
            continue
        review_sha = getattr(a, "review_head_sha", None)
        if review_sha is None or review_sha == current_sha:
            continue  # not stale, or SHA tracking unavailable — not this path
        review_patch_id = getattr(a, "review_patch_id", None)
        if review_patch_id is None:
            continue  # fail closed — cannot confirm scope, full review
        if review_patch_id == current_patch_id:
            continue  # content-identical — has_approved_review already covers it
        return a  # approval voided ONLY by a content-changing rebase
    return None


def intervening_work_since_review(
    entry: "QueuedMerge", board, review: Assignment
) -> list[Assignment]:
    """Return the :data:`~coord.models.WORK_LIKE_TYPES` assignments in
    *entry*'s branch chain that were **dispatched after** *review* was — i.e.
    genuine new commits (a bounce/fix round, a fresh work dispatch), not a
    mechanical rebase.

    Extracted from :func:`only_conflict_fix_since_review` so callers that need
    to distinguish its two distinct "False" reasons can do so: a non-empty
    list means "another commit landed after the approval" (never reaffirmable
    without a re-review), whereas an empty list plus a ``False`` from
    ``only_conflict_fix_since_review`` merely means "no coord-tracked
    conflict-fix explains the delta" (e.g. the operator rebased by hand) —
    unattributable, but not evidence of new logic. ``#1488``'s
    ``coord review-reaffirm`` hard-refuses the former and warns loudly on the
    latter; the automated dispatcher (``#1476``) declines both.

    Dispatch order, not completion order, is compared — see
    :func:`only_conflict_fix_since_review` for why.
    """
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    branch_work_ids = _chain_work_ids(entry, pool)
    review_dispatched_at = getattr(review, "dispatched_at", None)
    if review_dispatched_at is None:
        return []

    out: list[Assignment] = []
    for a in pool:
        if getattr(a, "type", None) not in WORK_LIKE_TYPES:
            continue
        if getattr(a, "assignment_id", None) not in branch_work_ids:
            continue
        a_dispatched_at = getattr(a, "dispatched_at", None)
        if a_dispatched_at is not None and a_dispatched_at > review_dispatched_at:
            out.append(a)
    return out


def only_conflict_fix_since_review(entry: "QueuedMerge", board, review: Assignment) -> bool:
    """True when the sole thing that changed *entry*'s branch since *review*
    approved it was one or more successful conflict-fix rebases (#1476's
    scoping guardrail) — i.e. a scoped review is safe to dispatch.

    False (⇒ the caller must fall back to a full review) when:

    - No successful (``status="done"``) conflict-fix for this merge entry is
      found at all — there is nothing to attribute the content change to,
      and guessing would be unsound.
    - Any other :data:`~coord.models.WORK_LIKE_TYPES` assignment in the
      branch's work chain (a fix/bounce round, a fresh work dispatch — i.e.
      a genuine new commit, not a rebase) was dispatched after *review* ran.

    Dispatch order, not completion order, is what's compared against
    *review*'s own dispatch time — a fix round that was *in flight* when the
    review was dispatched (and so is exactly what the review covered) must
    not itself disqualify the scoped path; only a fix/work round that
    started **after** the approval counts as "another commit".
    """
    if intervening_work_since_review(entry, board, review):
        return False  # a new work/fix round happened — not conflict-fix-only

    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    review_dispatched_at = getattr(review, "dispatched_at", None)

    for a in pool:
        if getattr(a, "type", None) != "conflict-fix":
            continue
        if getattr(a, "review_of_assignment_id", None) != entry.assignment_id:
            continue
        if getattr(a, "status", None) != "done":
            continue
        a_dispatched_at = getattr(a, "dispatched_at", None)
        if (
            review_dispatched_at is not None
            and a_dispatched_at is not None
            and a_dispatched_at < review_dispatched_at
        ):
            continue  # a conflict-fix from BEFORE this review isn't relevant
        return True
    return False


# ── Smoke gate (#465) ──────────────────────────────────────────────────────

def requires_smoke(entry: "QueuedMerge", config) -> bool:
    """True when *entry* must have an interactive smoke verdict before merging.

    Honours the *effective* gate list — ``entry``'s own ``required_gates``
    when set, falling back to ``config.pipeline.default_gates`` otherwise
    (#1213; see :func:`requires_review` for the duck-typing/fallback
    contract shared by both gates).  When ``"test"`` is in the resolved
    gate list the user must record ``coord test --passed`` (or ``--skip``)
    before ``coord merge`` proceeds.  ``"test"`` absent → gate disabled.
    """
    pipeline = getattr(config, "pipeline", None)
    if pipeline is None:
        return False
    gates = getattr(entry, "required_gates", None) or (pipeline.default_gates or [])
    return "test" in gates


# ── Gate-bypass auditing (#1213) ────────────────────────────────────────────

def _bypassed_gates(entry: "QueuedMerge", config) -> list[str]:
    """Which of the default pipeline's gates *entry*'s resolved gate list
    drops.

    Returns ``[]`` when ``entry`` carries no override (``required_gates``
    empty/absent — falls back to ``config.pipeline.default_gates``, nothing
    to bypass) or when its resolved gates already match the default list.
    Only ``"review"`` and ``"test"`` are reported — ``"merge"`` is the
    terminal action being gated, not a checkpoint that can be "bypassed".

    ``"review"`` is reported only when ``config.reviews.enabled`` is truthy
    — mirroring the guard :func:`requires_review` applies first. When review
    is globally disabled, dropping ``"review"`` from a label's resolved gate
    list changes nothing (the gate was already off), so it isn't a real
    bypass and reporting it would produce a misleading audit row / CLI note
    (#1213 review finding 1).
    """
    gates = getattr(entry, "required_gates", None)
    if not gates:
        return []
    pipeline = getattr(config, "pipeline", None) if config is not None else None
    default_gates = list(getattr(pipeline, "default_gates", None) or []) if pipeline else []
    reviews_enabled = bool(getattr(config, "reviews", None)) and bool(
        getattr(config.reviews, "enabled", True)
    )
    candidates = [g for g in ("review", "test") if g in default_gates and g not in gates]
    if not reviews_enabled:
        candidates = [g for g in candidates if g != "review"]
    return candidates


def _bypass_label(entry: "QueuedMerge", config) -> str | None:
    """Best-effort reverse lookup of the ``pipeline.labels`` key that
    produced *entry*'s resolved ``required_gates``, for a readable audit
    row / CLI message.

    Returns ``None`` when no exact match is found (the label was renamed or
    removed from config after enqueue time, or ``pipeline.labels`` is
    empty) — the audit event and CLI note still fire without a name in that
    case, since the gate list itself is the durable evidence.  Ambiguous
    when two labels resolve to the same gate list — the first match (dict
    iteration order) wins; this is display-only and never affects gate
    enforcement.
    """
    pipeline = getattr(config, "pipeline", None) if config is not None else None
    labels = getattr(pipeline, "labels", None) if pipeline else None
    gates = getattr(entry, "required_gates", None)
    if not labels or not gates:
        return None
    for label, label_gates in labels.items():
        if list(label_gates) == list(gates):
            return label
    return None


def _bypass_note(entry: "QueuedMerge", config) -> str:
    """Human-readable suffix naming any bypassed gate, or ``""`` when none.

    Appended to the ``coord merge`` "merged" event message (real and
    dry-run) so a bypass is never silent (#1213).  Side-effect free — the
    audit row itself is written separately, only on a real (non-dry-run)
    merge, by the caller in :func:`process`.
    """
    bypassed = _bypassed_gates(entry, config)
    if not bypassed:
        return ""
    label = _bypass_label(entry, config)
    label_desc = f"label {label!r}" if label else "an issue-label override"
    return f" [gate bypass via {label_desc}: {', '.join(bypassed)} skipped]"


def _record_gate_bypass_audit(entry: "QueuedMerge", config) -> list[str]:
    """Emit one ``gate_bypassed`` business-tier audit row per bypassed gate
    set, and return the bypassed gate names (``[]`` if none).

    Called once per real merge success in :func:`process` — never in
    dry-run, so previews never write phantom audit rows.  ``record_audit``
    is itself best-effort (never raises), matching every other write
    choke point in :mod:`coord.state`.
    """
    bypassed = _bypassed_gates(entry, config)
    if not bypassed:
        return []
    label = _bypass_label(entry, config)
    label_desc = f"label {label!r}" if label else "an issue-label override"
    record_audit(
        tier="business",
        category="gate",
        event_type="gate_bypassed",
        actor="user",
        summary=(
            f"Gate bypass via {label_desc}: {', '.join(bypassed)} skipped "
            f"for {entry.repo_name}#{entry.issue_number}"
        ),
        repo=entry.repo_name,
        issue=entry.issue_number,
        assignment_id=entry.assignment_id,
        details={
            "label": label,
            "resolved_gates": list(getattr(entry, "required_gates", None) or []),
            "bypassed_gates": bypassed,
        },
    )
    return bypassed


def passes_merge_gates(a, config, board, gh_ops: "GhOps | None" = None) -> bool:
    """True when *a* (a work ``Assignment`` or ``QueuedMerge`` entry) has
    satisfied every gate required before it may enter the merge queue.

    Shared predicate (#946) so untested/unreviewed work can never enter the
    queue through *any* enqueue path — previously each of the three enqueue
    call sites (the daemon's :func:`enqueue_approved_work`, the ``coord
    merge`` auto-enqueue loop, and the raw :func:`enqueue` helper) re-derived
    this logic and drifted: only the daemon path actually gated, so
    untested/unreviewed work could sneak into the queue via ``coord merge``.

    Duck-typed on ``entry.assignment_id`` / ``entry.branch`` (both
    ``Assignment`` and ``QueuedMerge`` have them), matching
    :func:`requires_review` / :func:`has_approved_review` / :func:`requires_smoke`
    / :func:`has_smoke_verdict`, which this composes.

    *gh_ops* (optional, #1601) is forwarded to both gates so a live SHA/
    patch-id lookup can back a fresh ``QueuedMerge`` entry that hasn't been
    through :func:`process` yet — see :func:`has_smoke_verdict`'s docstring.
    """
    if requires_review(a, config) and not has_approved_review(a, board, gh_ops):
        return False
    if requires_smoke(a, config) and not has_smoke_verdict(a, board, gh_ops):
        return False
    return True


def has_smoke_verdict(
    entry: "QueuedMerge", board, gh_ops: "GhOps | None" = None
) -> bool:
    """True when the smoke requirement for *entry* is satisfied.

    The gate **fails open**: if no work assignment can be found on the board
    for the entry's branch (e.g. board was cleared, manual queue entry, or
    the assignment pre-dates board persistence), this returns ``True`` so that
    the merge is not silently blocked without evidence.

    The gate **fails closed** (returns ``False``) only when we can positively
    identify the work assignment(s) on the branch and none of them carries a
    *fresh* ``test_state in ('passed', 'skipped')`` verdict.

    Collects all work assignment IDs connected to the entry — by shared
    branch, or (#567) by the ``review_of_assignment_id`` chain, which also
    catches fix workers dispatched with ``branch=NULL`` (the #557 remote-
    interactive-rework gap) — to handle bounce/fix-work chains.

    #1479: unlike the pre-existing behaviour, a terminal verdict is not
    trusted unconditionally — it is checked against the branch/base state it
    was recorded against (``test_head_sha``/``test_patch_id``/
    ``test_base_sha``, stamped by ``coord.state._record_test_verdict_local``)
    the same way ``has_approved_review`` checks ``review_head_sha``/
    ``review_patch_id``, **plus** one condition the review gate deliberately
    doesn't have: the *merge base itself* moving. A rebase onto a moved base
    can break tests without the branch's own diff changing at all (a
    semantic conflict — upstream renamed something the branch calls), so
    that combination no longer having been tested must re-block the gate
    even when ``branch_patch_id`` is unchanged. Content changing (new
    commits on the branch) also re-blocks, mirroring the review gate. Either
    anchor missing on either side skips that half of the check (fail open —
    #821/#1475's existing convention), so rows/entries predating this
    feature behave exactly as before.

    #1601: *gh_ops* (optional, mirroring :func:`has_approved_review`) fetches
    the branch's/base's *live* SHA (and, via :func:`_backfill_branch_patch_id`,
    the live patch-id) on demand when *entry* doesn't already carry them.
    Without it, an entry that has never been through a live :func:`process`
    pass has ``branch_head_sha``/``target_branch_head_sha``/``branch_patch_id``
    all ``None``, which makes every staleness check above a no-op — so
    ``coord merge --plan`` (which calls this via :func:`_entry_gate_status`
    on a freshly-enqueued entry) could show READY for a verdict that
    ``coord merge --only`` (whose :func:`process` DOES backfill these before
    checking) then correctly refuses as stale. Passing *gh_ops* through
    closes that "plan says ready, only refuses" disagreement — the #1566
    incident's reader 3 vs. reader 4 split.
    """
    pool = list(getattr(board, "completed", []) or []) + list(
        getattr(board, "active", []) or []
    )

    branch_work_ids = _chain_work_ids(entry, pool)

    # Collect work assignments that are explicitly present on the board.
    branch_work = [
        a for a in pool
        if getattr(a, "assignment_id", None) in branch_work_ids
        and getattr(a, "type", None) in WORK_LIKE_TYPES
    ]
    # Fail open: no work assignment found → can't block without evidence.
    if not branch_work:
        return True

    current_base_sha = getattr(entry, "target_branch_head_sha", None)
    current_branch_sha = getattr(entry, "branch_head_sha", None)
    current_patch_id = getattr(entry, "branch_patch_id", None)
    repo_github = getattr(entry, "repo_github", None)
    entry_branch = getattr(entry, "branch", None)
    target_branch = getattr(entry, "target_branch", None)
    base_sha_attempted = current_base_sha is not None
    branch_sha_attempted = current_branch_sha is not None
    patch_id_attempted = current_patch_id is not None

    # Work found — check whether any carries a fresh terminal smoke verdict.
    for a in branch_work:
        if getattr(a, "test_state", None) not in ("passed", "skipped"):
            continue

        # Merge base moved: the tested combination (this branch + that base)
        # no longer exists, even if the branch's own diff is unchanged.
        test_base_sha = getattr(a, "test_base_sha", None)
        if (
            test_base_sha is not None
            and current_base_sha is None
            and not base_sha_attempted
            and gh_ops is not None
            and repo_github
            and target_branch
        ):
            try:
                current_base_sha = gh_ops.get_branch_sha(repo_github, target_branch)
            except Exception:  # noqa: BLE001 — fail-safe: unknown SHA is not blocking
                current_base_sha = None
            base_sha_attempted = True
        if (
            test_base_sha is not None
            and current_base_sha is not None
            and test_base_sha != current_base_sha
        ):
            continue  # stale: re-verify against the new base

        # Branch content changed since the test ran. Same SHA-then-patch-id
        # fallback as has_approved_review: a content-identical rebase (SHA
        # moved, patch-id didn't) does not invalidate the verdict.
        test_head_sha = getattr(a, "test_head_sha", None)
        if (
            test_head_sha is not None
            and current_branch_sha is None
            and not branch_sha_attempted
            and gh_ops is not None
            and repo_github
            and entry_branch
        ):
            try:
                current_branch_sha = gh_ops.get_branch_sha(repo_github, entry_branch)
            except Exception:  # noqa: BLE001 — fail-safe: unknown SHA is not blocking
                current_branch_sha = None
            branch_sha_attempted = True
        if (
            test_head_sha is not None
            and current_branch_sha is not None
            and test_head_sha != current_branch_sha
        ):
            test_patch_id = getattr(a, "test_patch_id", None)
            if (
                test_patch_id is not None
                and current_patch_id is None
                and not patch_id_attempted
                and gh_ops is not None
            ):
                current_patch_id = _backfill_branch_patch_id(entry, gh_ops)
                patch_id_attempted = True
            if not (
                test_patch_id is not None
                and current_patch_id is not None
                and test_patch_id == current_patch_id
            ):
                continue  # stale: branch content changed since the test ran

        return True

    return False


# Stored error strings that only reflect the gate state *at the moment a
# merge attempt ran* (`process()`) — nothing clears them when the approval or
# verdict they're waiting on lands outside of a merge attempt (a normal
# interactive review, no `coord merge`/auto-loop tick in between). See #420.
_STALE_GATE_ERRORS = frozenset({
    "review required but not approved",
    "review required but board unavailable to confirm approval",
    "smoke test required but no verdict recorded",
    "smoke test required but board unavailable to confirm verdict",
})


def display_error(entry: "QueuedMerge", board, config) -> str | None:
    """Return the error to show for *entry* in a read-only display (``coord
    status``, dashboards) — recomputing the review/smoke gates live instead
    of trusting the stored ``entry.error`` string verbatim.

    #420: ``entry.error`` is only refreshed by :func:`process` (a real merge
    attempt) or ``refresh_entry_assignment``. When a review approves — or a
    smoke verdict is recorded — through the normal path (no ``coord merge``
    run, no auto-loop tick in between), nothing clears the stored string, so
    a mergeable entry can keep showing e.g. "review required but not
    approved" indefinitely. Left unchecked this invites operators to bounce
    already-approved work back for another round (the #410 real-world case).

    Only the two gate messages known to go stale this way are recomputed
    here, and recomputation is pure board/config lookups — no I/O. Every
    other stored error (merge conflicts, CI check results) reflects the
    outcome of the *last actual attempt* and is left untouched; re-checking
    CI on every ``coord status`` would mean a live ``gh`` call per queue
    entry just to render a status line.
    """
    if entry.error not in _STALE_GATE_ERRORS:
        return entry.error
    if board is None or config is None:
        # Can't recompute without both — fall back to the stored string.
        return entry.error
    if entry.error.startswith("review"):
        if requires_review(entry, config) and not has_approved_review(entry, board):
            return entry.error
        return None
    if entry.error.startswith("smoke"):
        if requires_smoke(entry, config) and not has_smoke_verdict(entry, board):
            return entry.error
        return None
    return entry.error  # pragma: no cover — unreachable, kept for safety


@dataclass
class QueuedMerge:
    assignment_id: str
    repo_name: str
    repo_github: str
    branch: str
    target_branch: str
    issue_number: int
    issue_title: str
    state: str = PENDING
    pr_number: int | None = None
    pr_url: str | None = None
    size: int | None = None
    last_attempt: float | None = None
    error: str | None = None
    enqueued_at: float | None = None
    # #821: current branch HEAD SHA, populated at process() time from GitHub.
    # When set, `has_approved_review` checks it against the review assignment's
    # `review_head_sha` to detect stale approvals (commits pushed after review).
    # None means SHA tracking is not available for this entry.
    branch_head_sha: str | None = None
    # #1475: current content-addressed patch-id for the branch's diff against
    # `target_branch`, populated at process() time alongside branch_head_sha.
    # `has_approved_review` falls back to comparing this against the review's
    # `review_patch_id` when the SHAs differ (e.g. a conflict-fix rebase) —
    # identical patch-id means the rebase changed no content, so the approval
    # still covers it. None means patch-id tracking is not available (fails
    # closed to the pre-#1475 SHA-only staleness check).
    branch_patch_id: str | None = None
    # #1479: current HEAD SHA of `target_branch` itself, populated at
    # process() time alongside branch_head_sha/branch_patch_id.
    # `has_smoke_verdict` compares this against the test verdict's recorded
    # `test_base_sha` to detect a merge base that moved since the test ran —
    # a condition `branch_patch_id` (the branch's own content fingerprint)
    # cannot see, since a rebase replays the identical diff onto a new base
    # without changing it. None means base-SHA tracking is not available for
    # this entry (transient, like branch_head_sha/branch_patch_id — never
    # persisted to the queue DB).
    target_branch_head_sha: str | None = None
    # #1077: the originating assignment's `type` (e.g. "work", "mock-author"),
    # captured at enqueue time. Drives both the PR-body "Closes #N" vs
    # "Refs #N" keyword (`_briefing_body`) and whether `process()` closes
    # `issue_number` deterministically after merge — see
    # `coord.models.CLOSES_ISSUE_TYPES`. Defaults to "work" for entries
    # created before this field existed (preserves prior close-on-merge
    # behavior for old rows).
    assignment_type: str = "work"
    # #1213: snapshot of the originating assignment's resolved
    # required_gates (from config.pipeline.labels via a matching GitHub
    # issue label, or [] for "no override"), captured at enqueue() time.
    # requires_review/requires_smoke read this — falling back to
    # config.pipeline.default_gates when empty — instead of re-resolving
    # from the live board at merge time, so the effective gate policy for
    # an entry is commit-bound to when it was enqueued. [] (the default)
    # means "no override" for both fresh entries and rows predating this
    # column (NULL decodes to []) — both fall back identically.
    required_gates: list[str] = field(default_factory=list)


class GhOps(Protocol):
    """Minimal interface the queue needs from github_ops. Tests pass a stub."""

    def create_pr(
        self, repo: str, *, base: str, head: str, title: str, body: str
    ) -> dict: ...

    def get_pr_size(self, repo: str, number: int) -> int: ...

    def merge_pr(self, repo: str, number: int, method: str = "rebase") -> tuple[bool, str]: ...

    def close_issue(self, repo: str, issue_number: int) -> None: ...

    def get_pr_body(self, repo: str, number: int) -> str:
        """Return PR *number*'s current body text (#1196, PR-body lint)."""
        ...

    def edit_pr_body(self, repo: str, number: int, body: str) -> None:
        """Overwrite PR *number*'s body text (#1196, PR-body lint)."""
        ...

    def has_open_children(self, repo: str, issue_number: int) -> bool:
        """True when *issue_number* has an open child (#1196)."""
        ...

    def is_epic_issue(self, repo: str, issue_number: int) -> bool:
        """True when *issue_number* carries the tracking/epic label (#1318)."""
        ...

    def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
        """Return every commit message on PR *number* (#1318, epic guard)."""
        ...

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        """Return the current HEAD SHA for *branch*, or None on failure.

        Used to populate ``QueuedMerge.branch_head_sha`` at process() time so
        ``has_approved_review`` can reject stale approvals (#821).  Returning
        ``None`` (on any network/auth failure) is safe — the staleness check
        is skipped for rows without a SHA, preserving backward compatibility.
        """
        ...

    def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
        """Return the content-addressed patch-id for *branch*'s diff against
        *base*, or None on failure.

        Used to populate ``QueuedMerge.branch_patch_id`` at process() time so
        ``has_approved_review`` can carry an approval forward across a pure
        rebase (#1475) even though the branch's HEAD SHA changed. Returning
        ``None`` is safe — the gate falls back to the pre-#1475 SHA-only
        staleness check.
        """
        ...

    def check_pr_mergeable(self, repo: str, number: int) -> bool | None:
        """Return GitHub's current mergeability verdict for PR *number*.

        ``True`` when cleanly mergeable, ``False`` when conflicting, ``None``
        when unknown (still computing, or the check itself failed). Used by
        :func:`reconcile_conflict_entries` (#1477) to re-test a parked
        ``CONFLICT`` entry rather than trusting the cached verdict from
        whenever the queue last attempted it.
        """
        ...

    def branch_has_merge_commit(self, repo: str, number: int) -> bool | None:
        """True when any commit on PR *number* has more than one parent.

        ``True``/``False`` when determined, ``None`` when it can't be (any
        ``gh`` error, or a malformed response) — an inconclusive read, same
        fail-closed contract as :meth:`check_pr_mergeable`. Used by
        :func:`process` (#1467) to fall back from ``--rebase`` to
        ``--squash`` before attempting a merge GitHub would otherwise refuse
        with "This branch can't be rebased", and by
        :func:`reconcile_conflict_entries` to avoid unparking an entry whose
        rebase-refusal will deterministically recur.

        Optional on stub ``GhOps`` implementations: callers detect support
        via ``getattr(gh_ops, "branch_has_merge_commit", None)`` and treat a
        missing method the same as an inconclusive (``None``) result, so
        existing test stubs that predate #1467 keep working unmodified.
        """
        ...

    def find_pr_for_branch(self, repo: str, branch: str) -> dict | None:
        """Return the open PR whose head ref is *branch*, or ``None``.

        Used by :func:`process` (#1624) to resolve an entry's real PR in the
        ``dry_run`` path — mirroring what ``create_pr`` already does
        internally on the real path — so a branch with an already-open PR is
        reported as ``PR #N (existed)`` instead of ``would open PR``, and so
        the CI gate below has a real PR number to evaluate against instead of
        silently skipping.

        Optional on stub ``GhOps`` implementations, same contract as
        :meth:`branch_has_merge_commit`: callers detect support via
        ``getattr(gh_ops, "find_pr_for_branch", None)`` and treat a missing
        method (or a lookup failure) the same as "no PR found" — fail closed,
        never assume a PR exists that couldn't be confirmed.
        """
        ...


# ── Persistence ──────────────────────────────────────────────────────────

def load_queue() -> list[QueuedMerge]:
    """Load all merge queue entries from the database."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM merge_queue ORDER BY id"
    ).fetchall()
    return [
        QueuedMerge(
            assignment_id=row["assignment_id"],
            repo_name=row["repo_name"],
            repo_github=row["repo_github"],
            branch=row["branch"],
            target_branch=row["target_branch"],
            issue_number=row["issue_number"],
            issue_title=row["issue_title"],
            state=row["state"],
            pr_number=row["pr_number"],
            pr_url=row["pr_url"],
            size=row["size"],
            last_attempt=row["last_attempt"],
            error=row["error"],
            enqueued_at=row["enqueued_at"],
            # #1077: column added via migration; rows written before it
            # existed read back as NULL, so fall back to "work" (the
            # pre-existing close-on-merge behavior for those entries).
            assignment_type=row["assignment_type"] or "work",
            # #1213: column added via migration; NULL (pre-migration rows)
            # and '[]' (explicit "no override") both decode to [] — the
            # gate falls back to config.pipeline.default_gates for either.
            required_gates=json.loads(row["required_gates"]) if row["required_gates"] else [],
        )
        for row in rows
    ]


def save_queue(items: list[QueuedMerge]) -> None:
    """Replace the entire merge queue in the database."""
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM merge_queue")
        for item in items:
            conn.execute(
                """INSERT INTO merge_queue (
                    assignment_id, repo_name, repo_github, branch,
                    target_branch, issue_number, issue_title, state,
                    pr_number, pr_url, size, last_attempt, error, enqueued_at,
                    assignment_type, required_gates
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.assignment_id, item.repo_name, item.repo_github,
                    item.branch, item.target_branch, item.issue_number,
                    item.issue_title, item.state, item.pr_number, item.pr_url,
                    item.size, item.last_attempt, item.error, item.enqueued_at,
                    item.assignment_type, json.dumps(list(item.required_gates or [])),
                ),
            )


# ── Enqueue ──────────────────────────────────────────────────────────────

def enqueue(
    assignment: Assignment,
    repo_github: str,
    target_branch: str,
    config=None,
    board=None,
) -> QueuedMerge | None:
    """Add a completed assignment to the queue if it isn't already there.

    Returns the new entry, or None if it was already queued, has no branch,
    or (#946) *config* was supplied and ``passes_merge_gates`` rejects it —
    i.e. review/smoke are required but not yet satisfied.  ``config`` (and
    ``board``) are optional and default to ``None`` for backward
    compatibility with existing callers (notably tests that seed the queue
    directly); passing ``None`` skips the gate check entirely rather than
    failing closed, since without a config there's no way to know which
    gates apply.

    Dedup is by ``(repo_github, branch)`` — the queue's natural key is the
    branch we'd merge, not the assignment_id.  Multiple work assignments
    routinely target the same branch (original + fix-1 in the auto-loop,
    original + PR-creator from ``coord pr``); they should not produce
    duplicate rows. (#274)
    """
    if not assignment.branch:
        return None
    if config is not None and not passes_merge_gates(assignment, config, board):
        return None
    items = load_queue()
    if any(
        x.assignment_id == assignment.assignment_id
        or (x.repo_github == repo_github and x.branch == assignment.branch)
        for x in items
    ):
        return None
    # #776: populate size eagerly at enqueue time via the compare API so the
    # displayed order matches the merge order without waiting for a PR to be
    # opened.  Fail-open: size=None keeps the entry at the back of the queue.
    from coord import github_ops as _gho  # noqa: PLC0415
    try:
        diff_size: int | None = _gho.get_branch_diff_size(
            repo_github, target_branch, assignment.branch
        ) or None
    except Exception:  # noqa: BLE001
        diff_size = None

    entry = QueuedMerge(
        assignment_id=assignment.assignment_id or "",
        repo_name=assignment.repo_name,
        repo_github=repo_github,
        branch=assignment.branch,
        target_branch=target_branch,
        issue_number=assignment.issue_number,
        issue_title=assignment.issue_title,
        size=diff_size,
        enqueued_at=time.time(),
        assignment_type=assignment.type,
        # #1213: snapshot the resolved gate list at enqueue time (commit-
        # bound) rather than leaving requires_review/requires_smoke to
        # re-resolve it from the live board later.
        required_gates=list(assignment.required_gates or []),
    )
    items.append(entry)
    save_queue(items)
    return entry


def enqueue_approved_work(config, board=None) -> list[str]:
    """Enqueue / re-key merge-queue entries for all approved + tested done work.

    Scans ``board.completed`` for done assignments whose ``type`` is in
    :data:`coord.models.WORK_LIKE_TYPES` (``"work"`` or ``"mock-author"``,
    #930) and, for each that satisfies ALL three conditions:

    1. Review gate OK — ``requires_review(a, config)`` is False, **or** an
       approved review exists on the board (``has_approved_review``).
    2. Smoke gate OK — ``requires_smoke(a, config)`` is False, **or** the
       work assignment carries a ``test_state in ('passed', 'skipped')``
       verdict (``has_smoke_verdict``).
    3. Not terminal on GitHub — ``work_is_terminal`` returns False (issue still
       open, or the PR that merged is not this branch's *current* commit —
       #1150: a historical merge on a reused branch, e.g. from ``--fix-of``
       continuing on the same branch, must not block enqueue of new commits
       pushed on top of it). This is checked directly against GitHub per
       assignment rather than via a queue-derived "already merged" shortcut,
       since a MERGED entry for the same ``(repo, issue)`` pair may belong to
       an entirely different branch/commit than the one being considered here.

    …calls :func:`refresh_entry_assignment` so the entry is **created** (when
    the work was never enqueued) or **re-keyed** to the latest fix assignment
    (the #292 bounce fix).  :func:`enqueue` is *not* used because it cannot
    update an existing entry's ``assignment_id``; ``refresh_entry_assignment``
    handles both cases.

    Idempotent: a second call with the same board produces no further changes
    (``refresh_entry_assignment`` is a no-op when the entry already exists and
    is keyed correctly).

    #1490: a fix/bounce cycle piles up more than one ``WORK_LIKE_TYPES`` row
    on the *same* branch (the original dispatch plus every retry), and each
    stays in ``board.completed`` forever. Processing every such row
    independently — the pre-#1490 behaviour — re-keyed the branch's one
    queue entry once per row, every single tick, because the review/smoke
    gates are resolved over the whole branch chain (so even a *failed*-test
    row passes them) and there was nothing to stop each row's turn from
    winning the re-key. :func:`group_branch_candidates` now resolves every
    branch to a single winner up front (the most-recently-dispatched row
    with a passed/skipped verdict — falling back to the most recent row
    overall when none has passed yet); every other row on that branch is
    logged (:func:`_log_superseded`) and never touches the queue.

    Returns a list of assignment IDs for which an entry was created or updated.
    Call sites use this list for diagnostic logging; callers that don't need it
    can discard the return value.

    Called from the daemon passive tick (:func:`coord.serve_app._passive_tick`)
    on every interval so approved work enters the queue without requiring a
    manual ``coord merge`` run (#736 / #217 invisible limbo).
    """
    from coord import github_ops as _gho  # noqa: PLC0415

    if board is None:
        from coord.state import build_board as _build_board  # noqa: PLC0415
        board = _build_board()

    changed: list[str] = []
    terminal_cache: dict = {}
    milestone_cache: dict = {}

    completed = list(getattr(board, "completed", []) or [])
    existing_queue = load_queue()

    for a, superseded in group_branch_candidates(completed):
        for row in superseded:
            _log_superseded(row)

        branch = a.branch
        aid = a.assignment_id
        repo_name = a.repo_name
        repo_cfg = config.repo(repo_name)
        if repo_cfg is None:
            continue

        # Skip if the assignment is already in the queue under its own ID.
        # refresh_entry_assignment would create a second entry when no entry
        # exists with a matching branch, even if one exists with the same
        # assignment_id (e.g. seeded with a different branch in the queue).
        # This guard prevents double-entries; re-keying is still handled
        # because for fix-work the new aid is NOT yet in the queue.
        if any(x.assignment_id == aid for x in existing_queue):
            continue

        # Gates 1+2: review + smoke, via the shared predicate (#946) so this
        # path stays in lockstep with the `coord merge` auto-enqueue loop and
        # the raw `enqueue()` helper.  Only blocks when a gate is configured
        # AND not satisfied — passes_merge_gates itself no-ops a disabled gate.
        if not passes_merge_gates(a, config, board):
            continue

        # Gate 3: not already terminal on GitHub (merged / closed).  Fail OPEN
        # on transient gh errors so a network blip never blocks a real enqueue.
        if _gho.work_is_terminal(
            repo_cfg.github,
            getattr(a, "issue_number", 0),
            branch,
            cache=terminal_cache,
        ):
            continue

        # #934: target the milestone's `feature/ms-NN` branch, not
        # `default_branch`, when this issue belongs to a milestone and the
        # repo opted into the develop + feature-branch-per-milestone git
        # model. The milestone lookup itself is skipped entirely (no `gh`
        # call) when the repo hasn't opted in — fails open to
        # `default_branch`, today's behavior, unchanged.
        from coord.branch_model import resolve_base_branch_for_issue_number  # noqa: PLC0415

        target_branch = resolve_base_branch_for_issue_number(
            repo_cfg,
            repo_cfg.github,
            getattr(a, "issue_number", 0),
            cache=milestone_cache,
        )

        if refresh_entry_assignment(
            a,
            repo_github=repo_cfg.github,
            target_branch=target_branch,
        ):
            changed.append(aid)

    return changed


def refresh_entry_assignment(
    assignment: Assignment,
    repo_github: str,
    target_branch: str,
) -> bool:
    """Ensure a PENDING queue entry exists for *assignment*'s branch and
    is keyed to *assignment*.

    #292 (Defect 2): after a review bounce the entry was created during an
    earlier ``coord merge`` run and is keyed to the *original* work
    assignment.  When the fix work gets approved, the entry's
    ``assignment_id`` must be updated so ``has_approved_review`` (and the
    matching TUI check) can find the approval.

    - If no entry exists for the branch, one is created (same as
      ``enqueue``).
    - If an entry already exists keyed to a different assignment on the
      same branch and its state is ``PENDING``, its ``assignment_id`` is
      updated and any stale ``"review required"`` error is cleared.
    - If the entry is in a terminal state (MERGED, CONFLICT, etc.) it is
      left untouched.

    Returns ``True`` when a change was made (entry created or updated).
    """
    from coord import github_ops as _gho  # noqa: PLC0415

    if not assignment.branch or not assignment.assignment_id:
        return False
    items = load_queue()
    # Match by (repo_github, branch) first; also accept a match by
    # assignment_id alone so that a queue entry with a different branch but
    # the same assignment_id (e.g. a test-seeded entry or a manually-created
    # entry) is treated as "already present" rather than spawning a second row.
    existing = next(
        (
            x for x in items
            if (x.repo_github == repo_github and x.branch == assignment.branch)
            or x.assignment_id == assignment.assignment_id
        ),
        None,
    )
    if existing is None:
        # #776: populate size eagerly (same as enqueue()) and record enqueued_at.
        try:
            diff_size: int | None = _gho.get_branch_diff_size(
                repo_github, target_branch, assignment.branch
            ) or None
        except Exception:  # noqa: BLE001
            diff_size = None

        entry = QueuedMerge(
            assignment_id=assignment.assignment_id,
            repo_name=assignment.repo_name,
            repo_github=repo_github,
            branch=assignment.branch,
            target_branch=target_branch,
            issue_number=assignment.issue_number,
            issue_title=assignment.issue_title,
            size=diff_size,
            enqueued_at=time.time(),
            assignment_type=assignment.type,
            # #1213: snapshot the resolved gate list, same as enqueue().
            required_gates=list(assignment.required_gates or []),
        )
        items.append(entry)
        save_queue(items)
        return True
    if existing.assignment_id == assignment.assignment_id:
        return False  # already correct
    if existing.state != PENDING:
        return False  # don't touch terminal entries (MERGED, CONFLICT, etc.)
    existing.assignment_id = assignment.assignment_id
    # #1077 (review round 1): do NOT overwrite existing.assignment_type here.
    # assignment_type is a structural property of the branch/issue pairing,
    # fixed once at enqueue() time -- not something to refresh from whatever
    # assignment last touched the branch. A review-bounce fix worker is
    # unconditionally dispatched with type="work" (auto_loop.py's
    # _dispatch_fix_for_review), regardless of the original assignment's
    # type, so re-keying assignment_type here would clobber a "mock-author"
    # entry back to "work" on every ordinary request-changes round trip --
    # silently re-enabling the close-on-merge behavior this issue fixed.
    # assignment_id legitimately needs to track the latest fix (for
    # approval-lookup purposes via has_approved_review), but assignment_type
    # does not -- a bounce/fix iteration is conceptually still "fixing the
    # same PR", so the type set at enqueue() stays authoritative.
    # Clear a stale "review required" error now that a fresh approval arrived.
    if existing.error == "review required but not approved":
        existing.error = None
    save_queue(items)
    return True


# ── Stale-conflict reconciliation (#1477) ───────────────────────────────────

def reconcile_conflict_entries(gh_ops: "GhOps") -> list["MergeEvent"]:
    """Re-test every ``CONFLICT`` entry's mergeability and clear stale verdicts.

    A ``CONFLICT`` entry caches the ``gh pr merge`` failure message from
    whenever the queue last attempted it, and ``process()`` never looks at it
    again — it only ever iterates ``PENDING`` entries. When a conflict-fix
    worker (#241) lands a rebase, or a human pushes a fix by hand, the branch
    becomes clean but the entry sits parked on the old verdict forever,
    requiring the three-step manual incantation described in #1477
    (``--drop`` → a bare re-enqueue → ``--only``) to notice.

    This re-tests GitHub's own mergeability computation for every
    ``CONFLICT`` entry that has an open PR and, when it now reports clean,
    returns the entry to ``PENDING`` and clears the stored error so it
    re-enters the ordinary merge flow on this tick — no manual archaeology.

    Fail-closed by design: an entry with no PR yet, or whose mergeability
    can't be determined (``gh`` error, or GitHub still computing it — both
    surface as ``None`` from :meth:`GhOps.check_pr_mergeable`), is left
    untouched. Only an explicit ``True`` unparks it — never speculative.

    #1467: a ``MERGEABLE`` verdict only reflects *content* conflicts — it
    says nothing about whether a ``--rebase`` merge specifically will
    succeed, because GitHub reports a branch carrying a merge commit as
    ``MERGEABLE`` even though it flatly refuses to rebase-merge it. An
    entry parked on that particular refusal (:func:`is_rebase_refusal`)
    would otherwise unpark here, hit the exact same wall in :func:`process`,
    and re-park — an infinite loop once auto-drain is on (#1491). For those
    entries specifically, this also confirms via
    :meth:`GhOps.branch_has_merge_commit` that the branch has actually gone
    linear before unparking; an inconclusive read (``None``, or a ``gh_ops``
    that doesn't support the probe) leaves the entry parked rather than
    guessing — the same fail-closed posture as the mergeability check above.
    A plain content conflict (no rebase-refusal wording) is unaffected and
    keeps the original mergeable-only behaviour.

    Loads and saves the queue directly (same shape as
    :func:`enqueue_approved_work`), so this is safe to call unconditionally,
    even under ``--dry-run``: it corrects previously-cached state rather than
    taking a merge action, mirroring the auto-enqueue scan that already runs
    regardless of ``--dry-run`` in ``coord merge``.

    Returns the list of :class:`MergeEvent` for entries that were cleared, so
    callers can echo them the same way they echo ``process()`` events.
    """
    items = load_queue()
    events: list[MergeEvent] = []
    changed = False
    for entry in items:
        if entry.state != CONFLICT or not entry.pr_number:
            continue
        try:
            mergeable = gh_ops.check_pr_mergeable(entry.repo_github, entry.pr_number)
        except Exception:  # noqa: BLE001 — never let a gh hiccup wedge the tick
            mergeable = None
        if mergeable is not True:
            continue
        if is_rebase_refusal(entry.error):
            probe = getattr(gh_ops, "branch_has_merge_commit", None)
            if probe is None:
                continue  # can't confirm linearity — stay parked (#1467)
            try:
                has_merge_commit = probe(entry.repo_github, entry.pr_number)
            except Exception:  # noqa: BLE001
                has_merge_commit = None
            if has_merge_commit is not False:
                # Still has a merge commit, or the probe was inconclusive —
                # unparking now would just reproduce the same refusal.
                continue
        entry.state = PENDING
        entry.error = None
        changed = True
        events.append(MergeEvent(
            entry, "reopened",
            f"conflict cleared — PR #{entry.pr_number} ({entry.branch}) is "
            "mergeable again, returned to pending",
        ))
    if changed:
        save_queue(items)
    return events


def resolve_entry_key(items: list["QueuedMerge"], key: str) -> "QueuedMerge | None":
    """Resolve *key* to a queue entry by whatever identifier the read path
    printed — ``assignment_id``, the durable ``repo#issue`` form, a bare
    issue number, or the branch name (#1477, #1490).

    ``assignment_id`` is volatile across a drop + re-enqueue cycle: a fresh
    row mints whatever assignment id the board currently shows for that
    issue, which is not guaranteed to match the id an operator last saw in
    ``coord status`` (#1477). #1490 sharpens this further: even *without* a
    drop, a queue entry can legitimately be re-keyed between the moment the
    board is read and the moment ``--only`` is invoked (a concurrent
    auto-enqueue tick re-keying the branch's one entry to a newer fix
    assignment) — so an id that was 100% correct when printed can already
    be stale by the time it's passed here. Every fallback below resolves by
    something that does *not* change out from under the operator for the
    life of the entry.

    Resolution order (first match wins):

    1. Exact ``assignment_id`` — unchanged, most specific.
    2. ``repo#issue`` (or ``repo_github#issue``) — only tried when *key*
       contains ``#`` (plain ids/branches never do, so this can never
       accidentally shadow one). A parse failure after ``#`` is a hard
       miss — no fallthrough to the forms below.
    3. A bare issue number — *key* parses as an ``int`` with no ``#``.
       Matches ``entry.issue_number`` across every repo in *items*;
       ambiguous only when the same issue number is queued for more than
       one repo, in which case (like form 2) the most recently added match
       wins.
    4. The entry's own ``branch`` name (#1490) — the most stable identifier
       there is: it's set once at enqueue time and never changes for the
       life of the entry, unlike ``assignment_id`` which re-keys on every
       fix/bounce round. This is the fallback the issue calls out
       explicitly: "if an ID is genuinely re-keyed between passes, resolve
       by branch".

    When more than one entry matches forms 2-4, the most recently added
    match wins (``load_queue()`` returns rows in insertion order) — the
    #1477 tie-break, applied uniformly.

    Returns ``None`` when nothing matches any form — callers must treat
    that as an explicit error, never a silent no-op (#1477).
    """
    for entry in items:
        if entry.assignment_id == key:
            return entry
    if "#" in key:
        repo_part, _, issue_part = key.rpartition("#")
        try:
            issue_number = int(issue_part)
        except ValueError:
            return None
        matches = [
            e for e in items
            if e.issue_number == issue_number and repo_part in (e.repo_name, e.repo_github)
        ]
        if matches:
            return matches[-1]
        return None
    try:
        bare_issue_number = int(key)
    except ValueError:
        bare_issue_number = None
    if bare_issue_number is not None:
        matches = [e for e in items if e.issue_number == bare_issue_number]
        if matches:
            return matches[-1]
    branch_matches = [e for e in items if e.branch == key]
    if branch_matches:
        return branch_matches[-1]
    return None


# ── Plan-status constants (#776) ─────────────────────────────────────────────

# Computed status values for PlannedMerge.status — not stored in the DB.
PLAN_READY = "READY"
PLAN_BLOCKED = "BLOCKED"
PLAN_MERGING = "MERGING"
PLAN_MERGED = "MERGED"
PLAN_NEEDS_ATTENTION = "NEEDS_ATTENTION"


# ── Gate evaluation (#776) ──────────────────────────────────────────────────

def _entry_gate_status(
    entry: "QueuedMerge",
    board,
    config,
    ci_store: "CiStore | None" = None,
    gh_ops: "GhOps | None" = None,
) -> tuple[str, str | None]:
    """Return *(status, reason)* for a single PENDING merge-queue entry.

    Evaluates gates in the same order as :func:`process` — review → smoke →
    CI → epic-closing-keyword-in-commit — so the plan shown to the operator
    is byte-for-byte what merge would do. Both :func:`plan` and :func:`process`
    delegate to this helper so they can never diverge.

    Returns ``(PLAN_READY, None)`` when all gates pass.
    Returns ``(PLAN_BLOCKED, reason)`` when any gate blocks.

    The *ci_store* gate is only evaluated when both *ci_store* is provided
    **and** the entry has a ``pr_number`` (CI is checked per-PR, not per-branch).
    This mirrors the live-merge behaviour: a ``PENDING`` entry with no PR yet
    opened is not blocked on CI — the PR hasn't been created yet.

    The *gh_ops* epic-closing-keyword-in-commit gate (#1318) is likewise only
    evaluated when both *gh_ops* is provided **and** the entry has a
    ``pr_number`` — mirroring the CI gate's guard, since commit messages can
    only be read once a PR exists. This gate is never bypassable via
    ``force_merge`` here (unlike :func:`process`'s live override) — the plan
    view has no such flag; an operator who wants to see the override outcome
    reads the ``coord merge --force-merge`` output itself.
    """
    if config is not None and board is not None:
        # #1506: pass gh_ops through so a null branch_patch_id (e.g. an entry
        # whose approved review predates #1475) is computed on demand rather
        # than displaying a stale "review not approved" the plan can't fix.
        if requires_review(entry, config) and not has_approved_review(entry, board, gh_ops):
            return PLAN_BLOCKED, "review not approved"
        if requires_smoke(entry, config) and not has_smoke_verdict(entry, board, gh_ops):
            return PLAN_BLOCKED, "test verdict missing"
    if ci_store is not None and ci_store.is_available and entry.pr_number:
        checks = ci_store.list_checks_for_pr(entry.repo_github, entry.pr_number)
        failed = failed_checks(checks)
        if failed:
            summary = ", ".join(f"{c.name} ({c.conclusion})" for c in failed)
            return PLAN_BLOCKED, f"CI failed: {summary}"
        pending = in_flight_checks(checks)
        if pending:
            summary = ", ".join(c.name for c in pending)
            return PLAN_BLOCKED, f"CI running: {summary}"
    if gh_ops is not None and entry.pr_number:
        try:
            commit_messages = gh_ops.get_pr_commit_messages(
                entry.repo_github, entry.pr_number
            )
        except Exception:  # noqa: BLE001
            commit_messages = []
        commit_referenced: set[int] = set()
        for message in commit_messages:
            commit_referenced.update(find_closing_references(message))
        commit_epic_hits: list[int] = []
        for n in sorted(commit_referenced):
            try:
                if gh_ops.is_epic_issue(entry.repo_github, n):
                    commit_epic_hits.append(n)
            except Exception:  # noqa: BLE001
                pass
        if commit_epic_hits:
            numbers_str = ", ".join(f"#{n}" for n in commit_epic_hits)
            return (
                PLAN_BLOCKED,
                f"commit message contains closing keyword for epic {numbers_str} (#1318)",
            )
    return PLAN_READY, None


# ── Merge plan (#776) ────────────────────────────────────────────────────────

@dataclass
class PlannedMerge:
    """One entry in the server-side merge plan.

    The plan is the single source of truth for ordering and gate-status — it
    is what the TUI panel, the CLI ``--plan`` flag, and auto-drain all consume.
    Unlike ``QueuedMerge``, which is the raw DB row, ``PlannedMerge`` carries
    computed fields (``rank``, ``status``, ``reason``, ``milestone``) that are
    always fresh and never stale.
    """

    assignment_id: str
    repo_name: str
    repo_github: str
    branch: str
    target_branch: str
    issue_number: int
    issue_title: str
    rank: int                    # 1-based, ordered by true merge sequence
    size: int | None             # diff lines (populated at enqueue; None = unknown)
    status: str                  # READY | BLOCKED | MERGING | MERGED | NEEDS_ATTENTION
    reason: str | None           # why it is blocked (None when READY / terminal)
    enqueued_at: float | None    # unix timestamp when the entry was created
    last_attempt: float | None   # unix timestamp of the last merge attempt
    milestone: str | None        # issue milestone title, or None
    # #1344: structured CI rollup so the TUI can render "2✓ 1✗" badges straight
    # from `/board` instead of shelling out to `gh pr checks` itself. `None`
    # when no PR is open yet, or `ci_store` has no checks for this PR.
    pr_number: int | None = None
    ci_summary: "CiCheckSummary | None" = None


def _load_milestones_for_queue(
    items: "list[QueuedMerge]",
) -> "dict[tuple[str, int], str | None]":
    """Load milestone titles for each (repo_name, issue_number) in *items*.

    Queries the ``issues`` table in bulk and returns a dict keyed by
    ``(repo_name, issue_number)``.  Missing rows (issue not yet synced) map
    to ``None``.  Any DB error returns an empty dict so the plan degrades
    gracefully.
    """
    if not items:
        return {}
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT repo_name, number, milestone_title FROM issues"
        ).fetchall()
        return {
            (r["repo_name"], r["number"]): r["milestone_title"]
            for r in rows
        }
    except Exception:  # noqa: BLE001
        return {}


def _state_to_plan_status(state: str) -> str:
    """Map a ``QueuedMerge.state`` to a ``PlannedMerge.status`` constant."""
    if state == PENDING:
        return PLAN_READY      # will be overridden by gate check if blocked
    if state == MERGING:
        return PLAN_MERGING
    if state == MERGED:
        return PLAN_MERGED
    # CONFLICT, HUMAN_REQUIRED, SKIPPED → surface for operator attention.
    return PLAN_NEEDS_ATTENTION


def plan(
    board,
    config,
    ci_store: "CiStore | None" = None,
    gh_ops: "GhOps | None" = None,
) -> "list[PlannedMerge]":
    """Return the **ordered merge plan** — one :class:`PlannedMerge` per queue entry.

    This is the single source of truth for ordering and gate-status consumed by
    the TUI panel (#B), the CLI ``--plan`` flag (#D), and auto-drain (#E).

    Algorithm
    ---------
    1. Load the queue from the DB.
    2. Group entries by ``(repo_github, target_branch)``.
    3. Within each group, order PENDING entries by ``sequence()`` (size-ascending
       with unknown-size last), then append non-PENDING entries in original DB
       order.
    4. Assign a 1-based ``rank`` globally across all groups (i.e. the first
       PENDING entry across all repos is rank=1 regardless of repo).
    5. For each entry:
       - Derive ``status`` from the raw ``state`` value.
       - For PENDING entries, override with :func:`_entry_gate_status` which
         evaluates review / smoke / CI / epic-closing-keyword-in-commit gates
         live against *board* + *config* + *ci_store* + *gh_ops*.
       - Look up the issue's milestone from the ``issues`` table.

    The function is intentionally **read-only** — no side effects, no DB writes.
    Pass ``board=None`` and/or ``config=None`` to skip the review/smoke gates,
    ``ci_store=None`` to skip the CI gate, and ``gh_ops=None`` to skip the
    epic-closing-keyword-in-commit gate (useful in test scenarios that only
    care about ordering).
    """
    items = load_queue()
    milestones = _load_milestones_for_queue(items)

    # ── Group by (repo_github, target_branch) ──────────────────────────────
    group_order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], list[QueuedMerge]] = {}
    for entry in items:
        key = (entry.repo_github, entry.target_branch)
        if key not in groups:
            group_order.append(key)
            groups[key] = []
        groups[key].append(entry)

    # ── Build the ranked plan ───────────────────────────────────────────────
    result: list[PlannedMerge] = []
    rank = 0

    for key in group_order:
        group = groups[key]
        # PENDING entries sorted by sequence(); all others in DB insertion order.
        pending = [e for e in group if e.state == PENDING]
        non_pending = [e for e in group if e.state != PENDING]
        ordered = sequence(pending) + non_pending

        for entry in ordered:
            rank += 1
            base_status = _state_to_plan_status(entry.state)
            reason: str | None = None

            ci_summary = None
            if entry.state == PENDING:
                base_status, reason = _entry_gate_status(
                    entry, board, config, ci_store, gh_ops
                )

                # #1344: structured CI rollup for the TUI's badges. Deliberately
                # scoped to PENDING entries only — the same scope as the gate
                # check above — because `ci_store` is not always the cheap,
                # tick-refreshed `GateSnapshot` (a dict lookup). Two other
                # callers pass a freshly-built *live* `CiStore`
                # (`ci_github.GitHubCi`) instead: `_auto_drain_tick`
                # (serve_app.py, every ~30s when `merge.auto_drain` is on) and
                # `coord merge --plan` (commands/merge.py). `merge_queue`
                # never prunes MERGED entries (see
                # `prune_stale_queue_entries`), so on a long-lived project the
                # queue table accumulates unbounded merged history — widening
                # this to "any entry with a pr_number" would fire one live
                # `gh pr checks` subprocess per historical merged PR on every
                # auto-drain tick, reintroducing the exact unbounded-`gh`-
                # polling failure class #1344 removed from the TUI, just
                # relocated to the daemon. It also wouldn't gain anything on
                # the safe /board path: `GateSnapshotRefresher.refresh` itself
                # only ever populates checks for entries that are PENDING at
                # refresh time, so a MERGING/MERGED row never had real
                # snapshot data to render in the first place.
                if ci_store is not None and ci_store.is_available and entry.pr_number:
                    checks = ci_store.list_checks_for_pr(entry.repo_github, entry.pr_number)
                    if checks:
                        ci_summary = summarize_counts(checks)

            result.append(PlannedMerge(
                assignment_id=entry.assignment_id,
                repo_name=entry.repo_name,
                repo_github=entry.repo_github,
                branch=entry.branch,
                target_branch=entry.target_branch,
                issue_number=entry.issue_number,
                issue_title=entry.issue_title,
                rank=rank,
                size=entry.size,
                status=base_status,
                reason=reason,
                pr_number=entry.pr_number,
                ci_summary=ci_summary,
                enqueued_at=entry.enqueued_at,
                last_attempt=entry.last_attempt,
                milestone=milestones.get((entry.repo_name, entry.issue_number)),
            ))

    return result


# ── Sequencing ───────────────────────────────────────────────────────────

def sequence(items: Iterable[QueuedMerge]) -> list[QueuedMerge]:
    """Order pending entries. Smaller diffs first; unknown sizes go last."""
    pending = [x for x in items if x.state == PENDING]
    return sorted(
        pending,
        key=lambda x: (x.size if x.size is not None else 10**9, x.assignment_id),
    )


def reorder(items: list[QueuedMerge], order: list[str]) -> list[QueuedMerge]:
    """Return `items` reordered so that assignment_ids in `order` come first
    in the given sequence. Unknown IDs are dropped from the override."""
    by_id = {x.assignment_id: x for x in items}
    head = [by_id[aid] for aid in order if aid in by_id]
    tail = [x for x in items if x.assignment_id not in set(order)]
    return head + tail


# ── Sibling overlap warnings (#920) ─────────────────────────────────────────
#
# The 2026-07-02 mess (docs referenced in #915) was triggered by late
# merging of overlapping sibling branches: #769/#645/#770 (+#768) were a
# milestone chain all editing the same new files, approved but left sitting
# while main moved, so every rebase collided with its siblings' additions.
# Nothing warned that these would conflict if merged out of order or late.
#
# `find_sibling_overlaps` is a pure, read-only heuristic over the merge
# queue: it groups PENDING (i.e. approved — see `enqueue_approved_work`'s
# review+smoke gate) entries by `(repo_github, target_branch)`, clusters
# same-group entries whose originating assignment's `files_allowed`
# (the brain's inferred "files likely touched" — the same signal
# `compute_do_not_touch` uses pre-dispatch, see `coord.dispatch`) overlap,
# and reports a warning once the oldest member of a ≥2-entry cluster has
# been sitting in the queue at least `config.merge.sibling_overlap_aging_hours`.


@dataclass(frozen=True)
class SiblingOverlapWarning:
    """≥2 approved, aging queue entries whose branches touch the same files.

    `issue_numbers` is already in the suggested merge order — oldest
    ``enqueued_at`` first, since that entry has drifted furthest from a
    moving main and merging it first shrinks the others' eventual rebase.
    """

    repo_name: str
    target_branch: str
    issue_numbers: tuple[int, ...] = field(default_factory=tuple)
    overlapping_files: tuple[str, ...] = field(default_factory=tuple)
    oldest_age_hours: float = 0.0


def find_sibling_overlaps(
    board,
    config,
    *,
    now: float | None = None,
) -> list[SiblingOverlapWarning]:
    """Detect approved, aging, file-overlapping sibling branches in the queue.

    Pure/read-only: loads the queue via :func:`load_queue`, reads
    ``files_allowed`` off the matching assignments on *board*
    (``board.completed`` + ``board.active``), does no GitHub/subprocess
    calls. ``config.merge.sibling_overlap_aging_hours`` (default 24h) gates
    how long the oldest entry in an overlapping cluster must have waited
    before it's worth surfacing — a value of ``0`` (or a missing
    ``merge`` config) disables the warning entirely.

    Only ``PENDING`` entries are considered: by the time an assignment has a
    queue entry, :func:`enqueue`/:func:`enqueue_approved_work` have already
    applied the review+smoke gate, so a PENDING entry is "approved" in the
    sense #920 means. Entries without a recorded ``enqueued_at`` (pre-#274
    rows) are skipped — there's no age to measure.
    """
    aging_hours = getattr(getattr(config, "merge", None), "sibling_overlap_aging_hours", 24.0)
    if not aging_hours or aging_hours <= 0:
        return []
    if now is None:
        now = time.time()

    entries = [e for e in load_queue() if e.state == PENDING and e.enqueued_at is not None]
    if len(entries) < 2:
        return []

    pool = (
        list(getattr(board, "completed", []) or [])
        + list(getattr(board, "active", []) or [])
    )
    files_by_aid: dict[str, set[str]] = {}
    for a in pool:
        aid = getattr(a, "assignment_id", None)
        if aid:
            files_by_aid[aid] = set(getattr(a, "files_allowed", None) or [])

    groups: dict[tuple[str, str], list[QueuedMerge]] = {}
    for e in entries:
        groups.setdefault((e.repo_github, e.target_branch), []).append(e)

    warnings: list[SiblingOverlapWarning] = []
    for (repo_github, target_branch), group in groups.items():
        if len(group) < 2:
            continue

        # Union-find: cluster entries transitively sharing >=1 file.
        parent = {e.assignment_id: e.assignment_id for e in group}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(group)):
            files_i = files_by_aid.get(group[i].assignment_id, set())
            if not files_i:
                continue
            for j in range(i + 1, len(group)):
                files_j = files_by_aid.get(group[j].assignment_id, set())
                if files_i & files_j:
                    union(group[i].assignment_id, group[j].assignment_id)

        clusters: dict[str, list[QueuedMerge]] = {}
        for e in group:
            clusters.setdefault(find(e.assignment_id), []).append(e)

        for members in clusters.values():
            if len(members) < 2:
                continue
            oldest_enqueued = min(m.enqueued_at for m in members)
            age_hours = (now - oldest_enqueued) / 3600.0
            if age_hours < aging_hours:
                continue

            ordered = sorted(members, key=lambda m: (m.enqueued_at, m.assignment_id))
            overlap_files: set[str] = set()
            for i in range(len(ordered)):
                files_i = files_by_aid.get(ordered[i].assignment_id, set())
                for j in range(i + 1, len(ordered)):
                    files_j = files_by_aid.get(ordered[j].assignment_id, set())
                    overlap_files |= files_i & files_j

            warnings.append(SiblingOverlapWarning(
                repo_name=members[0].repo_name,
                target_branch=target_branch,
                issue_numbers=tuple(m.issue_number for m in ordered),
                overlapping_files=tuple(sorted(overlap_files)),
                oldest_age_hours=round(age_hours, 1),
            ))

    warnings.sort(key=lambda w: (-w.oldest_age_hours, w.repo_name, w.target_branch))
    return warnings


# ── Staging section (#778) ────────────────────────────────────────────────────

# Status values for StagingItem.status — never stored in the DB.
STAGING_READY = "ready"      # all gates pass; will be enqueued on the next tick
STAGING_BLOCKED = "blocked"  # at least one non-review gate is failing


@dataclass
class StagingItem:
    """One entry in the 'approved but not yet queued' staging section.

    Populated by :func:`staging_items` which scans the board for completed
    work assignments that have an approved review (or don't need one) but
    have not yet been admitted to the merge queue.  Exposed on ``/board`` so
    thin clients (TUI, phone webapp) can answer "did my PR make it in?" without
    a manual ``coord merge --dry-run``.
    """

    assignment_id: str
    repo_name: str
    issue_number: int
    issue_title: str
    branch: str
    status: str          # STAGING_READY | STAGING_BLOCKED
    reason: str | None   # None when ready; human-readable gate failure when blocked


def _work_has_approved_review_a(a, board) -> bool:
    """True when *a* (a work Assignment) has an approved review on *board*.

    Mirrors :func:`has_approved_review` but accepts a raw Assignment rather
    than a QueuedMerge entry, since staging items are not yet in the queue.
    Delegates to the shared :func:`_chain_work_ids` fixed-point expansion
    (#567 follow-up) rather than a standalone branch-only expansion: any work
    assignment on the same branch, or connected via the
    ``review_of_assignment_id`` chain — which also catches fix workers
    dispatched with ``branch=NULL`` (the #557 gap) — counts. ``_chain_work_ids``
    is duck-typed on ``.assignment_id``/``.branch``, both present on a raw
    ``Assignment``, so it accepts *a* directly.
    """
    pool = (
        list(getattr(board, "completed", []) or [])
        + list(getattr(board, "active", []) or [])
    )

    branch_work_ids = _chain_work_ids(a, pool)

    if not branch_work_ids:
        return False

    for x in pool:
        if getattr(x, "type", None) != "review":
            continue
        if getattr(x, "review_of_assignment_id", None) not in branch_work_ids:
            continue
        if getattr(x, "review_verdict", None) == "approve":
            return True
    return False


def staging_items(board, config) -> list[StagingItem]:
    """Return work assignments that are done+approved but not yet in the queue.

    Scans ``board.completed`` for ``status=done`` assignments whose ``type``
    is in :data:`coord.models.WORK_LIKE_TYPES` (``"work"`` or
    ``"mock-author"``, #930) and returns one :class:`StagingItem` per
    candidate that has an approved review
    (or doesn't need one) but hasn't yet been admitted to the merge queue.
    Each item is classified:

    * ``STAGING_READY``   — all gates pass; will be enqueued on the next daemon
      tick (typically within 30 s of approval).
    * ``STAGING_BLOCKED`` — the smoke / test gate is failing; the item cannot
      enter the queue until the operator records a verdict (``coord test
      --passed`` / ``--skipped``).

    Items that have NOT received an approved review are silently excluded so
    that the staging section only shows work the pipeline has already green-lit.

    The function is intentionally **read-only**: no DB writes, no GitHub API
    calls.  Pass ``board=None`` or ``config=None`` to skip gate evaluation
    (useful in tests that only care about filtering logic).
    """
    existing_queue = load_queue()

    # Fast-lookup: assignment IDs already in the queue (any state).
    queued_aids: set[str] = {x.assignment_id for x in existing_queue}

    # Fast-lookup: branches already in the queue (any state).  A fix worker
    # dispatched after the original work was enqueued will have a different
    # assignment_id but share the same branch — so dedup by branch too.
    queued_branches: set[str] = {x.branch for x in existing_queue if x.branch}

    # Fast-lookup: (repo_name, issue_number) pairs already MERGED so we skip
    # issues whose prior attempt was already shipped.
    already_merged: set[tuple[str, int]] = {
        (x.repo_name, x.issue_number)
        for x in existing_queue
        if x.state == MERGED
    }

    result: list[StagingItem] = []
    completed = list(getattr(board, "completed", []) or [])

    for a in completed:
        if getattr(a, "type", None) not in WORK_LIKE_TYPES:
            continue
        if getattr(a, "status", None) != "done":
            continue

        aid = getattr(a, "assignment_id", None)
        branch = getattr(a, "branch", None)
        if not aid or not branch:
            continue

        repo_name = getattr(a, "repo_name", None) or ""
        issue_number = int(getattr(a, "issue_number", 0) or 0)
        issue_title = getattr(a, "issue_title", None) or ""

        # Skip items already tracked in the queue (by assignment_id or branch).
        # Branch-level dedup catches fix workers that share a branch with an
        # already-queued original work assignment (#778).
        if aid in queued_aids or branch in queued_branches:
            continue

        # Skip if the issue has already been merged via a prior work attempt.
        if (repo_name, issue_number) in already_merged:
            continue

        # Gate: review.  Skip entirely when review is required but NOT yet
        # approved — the item isn't "approved" yet and should not appear in the
        # staging section (it belongs to the pipeline, not the merge staging).
        if config is not None and board is not None:
            if requires_review(a, config) and not _work_has_approved_review_a(a, board):
                continue

        # Gate: smoke.  When the test gate is enabled and no verdict exists,
        # the item appears as BLOCKED rather than being silently excluded.
        status = STAGING_READY
        reason: str | None = None
        if config is not None and board is not None:
            if requires_smoke(a, config) and getattr(a, "test_state", None) not in (
                "passed",
                "skipped",
            ):
                status = STAGING_BLOCKED
                reason = "test verdict missing"

        result.append(StagingItem(
            assignment_id=aid,
            repo_name=repo_name,
            issue_number=issue_number,
            issue_title=issue_title,
            branch=branch,
            status=status,
            reason=reason,
        ))

    return result


# ── Processing ───────────────────────────────────────────────────────────

@dataclass
class MergeEvent:
    entry: QueuedMerge
    kind: str  # "opened" | "sized" | "merged" | "conflict" | "skipped" | "error" | "reopened"
    message: str = ""


def _briefing_body(entry: QueuedMerge) -> str:
    # `Closes #N` makes GitHub auto-close the linked issue when the PR
    # merges — without it the issue stays stranded open and the TUI's
    # lifecycle ledger shows the row as In-flight forever (the brain
    # keeps re-synching it as state=open).  Quadraui #239/#240/#242 hit
    # this in 2026-05; closing the issues was a manual cleanup.
    #
    # #1077: only emit the closing keyword when this entry's issue_number is
    # actually resolved by the PR (`CLOSES_ISSUE_TYPES`). A "mock-author"
    # entry's issue_number is the milestone's tracking issue — closing it on
    # merge is wrong (the epic reads "done" while its sub-issues are still
    # open), so it gets the non-closing `Refs #N` instead.
    keyword = "Closes" if entry.assignment_type in CLOSES_ISSUE_TYPES else "Refs"
    return (
        f"{keyword} #{entry.issue_number}\n\n"
        f"Automated merge from the coordinator for assignment "
        f"{entry.assignment_id} on issue #{entry.issue_number}.\n\n"
        f"Worker branch: `{entry.branch}` → `{entry.target_branch}`."
    )


def process(
    items: list[QueuedMerge],
    gh_ops: GhOps,
    *,
    method: str = "rebase",
    dry_run: bool = False,
    presorted: bool = False,
    ci_store: CiStore | None = None,
    force_merge: bool = False,
    config=None,
    board=None,
    skip_review: bool = False,
    skip_smoke: bool = False,
) -> list[MergeEvent]:
    """Open PRs, size them, then merge each pending item.

    Items are grouped by (repo_github, target_branch); a **merge conflict**
    parks the conflicting entry (``CONFLICT`` state; the caller in
    ``cli.py`` promotes it to ``HUMAN_REQUIRED``) and **continues** with
    the remaining siblings in that group — each entry's ``gh pr merge`` is
    independent, so a failed merge does not dirty the target branch for
    siblings (#735).  Within a group, items are merged in input order —
    call `sequence(group)` first if you want size-based ordering.
    Set `presorted=True` to make that explicit at call sites.

    When ``ci_store`` is provided and available, each PR is checked against
    its CI status before merge.  A failed check produces a ``checks_failed``
    event; a still-running check produces ``checks_pending``.  In both cases
    the entry is **skipped** (``continue``) rather than halting the group, so
    a ready sibling can still merge.  ``force_merge=True`` skips this gate.

    #253/#821: When *config* says review is required (``reviews.enabled`` and
    ``"review"`` in ``pipeline.default_gates``) the gate **fails closed**: if
    *board* is ``None`` the approval cannot be confirmed so the entry is
    blocked (``review_required`` event, skip — never merge).  When *config*
    is ``None`` the gate is not applicable (no review policy → no block).
    ``skip_review=True`` bypasses the gate for explicit local-only overrides.
    The daemon ``/merge`` endpoint always passes ``skip_review=False`` and
    ignores any ``skip_review`` flag from the client (#821).

    #465/#821: Same fail-closed semantics for the smoke gate: when *config*
    says ``"test"`` is in ``pipeline.default_gates`` but *board* is ``None``,
    the verdict cannot be confirmed → block (``smoke_required`` event).
    ``skip_smoke=True`` bypasses the gate.

    Dry-run applies the review and smoke gates, and — #1624 — resolves each
    entry's real PR via ``find_pr_for_branch`` (the same lookup ``create_pr``
    does internally) and applies the CI gate against it too, so output
    reflects what a real run would do. CI genuinely cannot be checked for an
    entry with no PR yet (nothing exists to query); that case is reported as
    an explicit ``gate: unknown (no PR yet)`` note rather than silently
    treated as passing.

    #1318: before each merge, both the PR body (#1196) and every commit
    message on the branch are scanned for a GitHub closing keyword
    (``Closes``/``Fixes``/``Resolves #N``) targeting an epic-labelled issue
    — free-text prose in a commit message (even a quote explaining the bug)
    is enough for GitHub's own scanner to auto-close it once the commit
    lands on the base branch, and no PR-body edit can undo that. A body hit
    is downgraded to ``Refs #N`` in place, same as #1196. A commit-message
    hit can't be rewritten here (no local git checkout in this ``gh``-only
    wire layer) so it **blocks** the merge (``epic_closing_keyword_in_commit``
    event) unless ``force_merge=True``, in which case the merge proceeds but
    an ``epic_closing_keyword_in_commit_forced`` warning event is still
    emitted — the override is never silent.

    Mutates `items` in place; the caller saves the queue after.
    """
    events: list[MergeEvent] = []
    ci: CiStore = ci_store if ci_store is not None else NoOpCi()

    groups: dict[tuple[str, str], list[QueuedMerge]] = {}
    for entry in items:
        if entry.state != PENDING:
            continue
        groups.setdefault((entry.repo_github, entry.target_branch), []).append(entry)

    _unset = object()

    for group in groups.values():
        # #1479-review: every entry in a group shares the same target_branch
        # (that's the grouping key), so target_branch_head_sha is the same
        # value for all of them — fetch it once per group instead of once
        # per entry to avoid N redundant `gh api` calls for an N-entry group.
        _group_target_branch_head_sha: str | None | object = _unset

        if dry_run:
            # #1624: resolve each entry's real PR the same way the non-dry
            # path does (`create_pr` internally calls `find_pr_for_branch`
            # before ever calling `gh pr create`) instead of unconditionally
            # announcing "would open PR". A branch can already have an open
            # PR — from an earlier real attempt that opened one and then
            # stalled on a gate, or created out-of-band — and the CI gate
            # below needs a real PR number to evaluate against; without this,
            # the gate was silently skipped and the entry reported mergeable
            # even with failing checks (#1624). `find_pr_for_branch` is
            # optional on GhOps (older test stubs predate #1624): a missing
            # probe or a lookup failure leaves the PR unresolved, same
            # fail-closed contract as `branch_has_merge_commit` (#1467).
            _find_pr = getattr(gh_ops, "find_pr_for_branch", None)
            for entry in group:
                if entry.pr_number is not None:
                    events.append(MergeEvent(
                        entry, "opened",
                        f"PR #{entry.pr_number} (existed) for {entry.branch}",
                    ))
                    continue
                existing = None
                if _find_pr is not None:
                    try:
                        existing = _find_pr(entry.repo_github, entry.branch)
                    except Exception:  # noqa: BLE001
                        existing = None
                if existing is not None:
                    entry.pr_number = existing.get("number")
                    entry.pr_url = existing.get("url")
                    events.append(MergeEvent(
                        entry, "opened",
                        f"PR #{entry.pr_number} (existed) for {entry.branch}",
                    ))
                else:
                    events.append(MergeEvent(
                        entry, "opened",
                        f"(dry run) would open PR for {entry.branch}",
                    ))
            ordered = group if presorted else sequence(group)
            for entry in ordered:
                # #821: populate branch_head_sha for the commit-bound approval
                # staleness check in has_approved_review.  Only when the board
                # is live (board=None blocks unconditionally; no SHA needed).
                if board is not None and entry.branch_head_sha is None:
                    entry.branch_head_sha = gh_ops.get_branch_sha(
                        entry.repo_github, entry.branch
                    )
                # #1475/#1479: populate branch_patch_id alongside
                # branch_head_sha so has_approved_review / has_smoke_verdict
                # can carry a verdict forward across a content-identical
                # rebase instead of re-blocking on SHA alone. Only fetch it
                # when review or smoke is actually required for this entry —
                # neither gate consults branch_patch_id otherwise, so
                # skipping here saves a `gh api compare` round trip per entry
                # per process() tick for the common gate-disabled case.
                if (
                    board is not None
                    and entry.branch_patch_id is None
                    and config is not None
                    and (
                        (not skip_review and requires_review(entry, config))
                        or (not skip_smoke and requires_smoke(entry, config))
                    )
                ):
                    entry.branch_patch_id = gh_ops.get_branch_patch_id(
                        entry.repo_github, entry.target_branch, entry.branch
                    )
                # #1479: populate target_branch_head_sha so has_smoke_verdict
                # can detect a merge base that moved since the test verdict
                # was recorded — a condition branch_patch_id can't see, since
                # a rebase replays the identical diff onto a new base without
                # changing it. Only fetched when smoke is actually required,
                # same cost-avoidance as branch_patch_id above.
                if (
                    board is not None
                    and entry.target_branch_head_sha is None
                    and not skip_smoke
                    and config is not None
                    and requires_smoke(entry, config)
                ):
                    if _group_target_branch_head_sha is _unset:
                        _group_target_branch_head_sha = gh_ops.get_branch_sha(
                            entry.repo_github, entry.target_branch
                        )
                    entry.target_branch_head_sha = _group_target_branch_head_sha
                # #292 (Defect 4): apply the review gate in dry-run so output
                # reflects real behaviour.  CI cannot be checked in dry-run
                # (no PR exists yet), so review and smoke gates are evaluated.
                # #821: fail closed — if review is required but board is None
                # (approval cannot be confirmed) block the entry.
                if (
                    not skip_review
                    and config is not None
                    and requires_review(entry, config)
                    and (board is None or not has_approved_review(entry, board, gh_ops))
                ):
                    _why = (
                        "board unavailable to confirm review approval"
                        if board is None
                        else "review required but not approved"
                    )
                    events.append(MergeEvent(
                        entry, "review_required",
                        f"(dry run) would be blocked: {_why} for {entry.branch}",
                    ))
                    continue
                # #465/#821: smoke gate in dry-run — same fail-closed logic.
                if (
                    not skip_smoke
                    and config is not None
                    and requires_smoke(entry, config)
                    and (board is None or not has_smoke_verdict(entry, board, gh_ops))
                ):
                    _why = (
                        "board unavailable to confirm smoke verdict"
                        if board is None
                        else "smoke test required but no verdict"
                    )
                    events.append(MergeEvent(
                        entry, "smoke_required",
                        f"(dry run) would be blocked: {_why} for {entry.branch}",
                    ))
                    continue
                # CI gate (#240) preview, added by #1624: same check the real
                # path runs, evaluated here so a dry run can't claim
                # "would merge" for a PR whose checks are already failing.
                # Only evaluable when a real PR number is known — either
                # persisted from an earlier attempt or just resolved above
                # via `find_pr_for_branch` — since CI is checked per-PR, not
                # per-branch. A brand-new entry with no PR yet genuinely
                # cannot be checked; say so explicitly in the "merged"
                # preview below rather than silently treating "not
                # evaluated" as "would merge" (#1624). `force_merge` skips
                # the gate here exactly as it does in the real path.
                _ci_note = ""
                if not force_merge and ci.is_available:
                    if entry.pr_number is not None:
                        checks = ci.list_checks_for_pr(entry.repo_github, entry.pr_number)
                        failed = failed_checks(checks)
                        if failed:
                            summary = ", ".join(
                                f"{c.name} ({c.conclusion})" for c in failed
                            )
                            events.append(MergeEvent(
                                entry, "checks_failed",
                                f"(dry run) would be blocked: checks failed: {summary}",
                            ))
                            continue
                        pending = in_flight_checks(checks)
                        if pending:
                            summary = ", ".join(c.name for c in pending)
                            events.append(MergeEvent(
                                entry, "checks_pending",
                                f"(dry run) would be blocked: checks still running: {summary}",
                            ))
                            continue
                    else:
                        _ci_note = " [gate: unknown (no PR yet) — CI cannot be evaluated]"
                # #1467-review: preview the rebase→squash fallback in
                # dry-run too. Only reachable when this entry already has a
                # pr_number — from an earlier (non-dry-run) attempt, or just
                # resolved above via `find_pr_for_branch` (#1624) — since the
                # probe needs one to query. A first-time dry-run preview of a
                # brand-new entry still can't foresee the fallback. Same
                # fail-closed contract as the real merge path: an
                # inconclusive probe leaves the previewed method unchanged.
                _preview_method = method
                if method == "rebase" and entry.pr_number is not None:
                    _probe = getattr(gh_ops, "branch_has_merge_commit", None)
                    if _probe is not None:
                        try:
                            _has_merge_commit = _probe(
                                entry.repo_github, entry.pr_number
                            )
                        except Exception:  # noqa: BLE001
                            _has_merge_commit = None
                        if _has_merge_commit is True:
                            _preview_method = "squash"
                            events.append(MergeEvent(
                                entry, "method_fallback",
                                f"(dry run) PR #{entry.pr_number} ({entry.branch}) "
                                "contains a merge commit and cannot be "
                                "rebase-merged — would fall back to --squash "
                                "(#1467)",
                            ))
                events.append(MergeEvent(
                    entry, "merged",
                    f"(dry run) would merge {entry.branch} → {entry.target_branch} "
                    f"via --{_preview_method}"
                    f"{_bypass_note(entry, config)}"
                    f"{_ci_note}",
                ))
            continue

        # Open PRs first so every entry has a pr_number when we sort & merge.
        for entry in group:
            if entry.pr_number is None:
                try:
                    pr = gh_ops.create_pr(
                        entry.repo_github,
                        base=entry.target_branch,
                        head=entry.branch,
                        title=f"#{entry.issue_number}: {entry.issue_title}",
                        body=_briefing_body(entry),
                    )
                except Exception as e:  # noqa: BLE001 — surface gh failure as event
                    events.append(MergeEvent(entry, "error", f"create_pr failed: {e}"))
                    continue
                entry.pr_number = pr.get("number")
                entry.pr_url = pr.get("url")
                events.append(MergeEvent(
                    entry, "opened",
                    f"PR #{entry.pr_number} ({'existed' if pr.get('existed') else 'created'}) for {entry.branch}",
                ))
            if entry.pr_number and entry.size is None:
                entry.size = gh_ops.get_pr_size(entry.repo_github, entry.pr_number)
                events.append(MergeEvent(entry, "sized", f"size={entry.size}"))

        ordered = group if presorted else sequence(group)
        for entry in ordered:
            if entry.pr_number is None:
                continue
            # #821: populate branch_head_sha for the commit-bound approval
            # staleness check in has_approved_review.  Only when the board
            # is live (board=None blocks unconditionally; no SHA needed).
            if board is not None and entry.branch_head_sha is None:
                entry.branch_head_sha = gh_ops.get_branch_sha(
                    entry.repo_github, entry.branch
                )
            # #1475/#1479: populate branch_patch_id alongside branch_head_sha
            # so has_approved_review / has_smoke_verdict can carry a verdict
            # forward across a content-identical rebase instead of
            # re-blocking on SHA alone. Only fetch it when review or smoke is
            # actually required for this entry — neither gate consults
            # branch_patch_id otherwise, so skipping here saves a `gh api
            # compare` round trip per entry per process() tick for the
            # common gate-disabled case.
            if (
                board is not None
                and entry.branch_patch_id is None
                and config is not None
                and (
                    (not skip_review and requires_review(entry, config))
                    or (not skip_smoke and requires_smoke(entry, config))
                )
            ):
                entry.branch_patch_id = gh_ops.get_branch_patch_id(
                    entry.repo_github, entry.target_branch, entry.branch
                )
            # #1479: populate target_branch_head_sha so has_smoke_verdict can
            # detect a merge base that moved since the test verdict was
            # recorded — a condition branch_patch_id can't see, since a
            # rebase replays the identical diff onto a new base without
            # changing it. Only fetched when smoke is actually required,
            # same cost-avoidance as branch_patch_id above.
            if (
                board is not None
                and entry.target_branch_head_sha is None
                and not skip_smoke
                and config is not None
                and requires_smoke(entry, config)
            ):
                if _group_target_branch_head_sha is _unset:
                    _group_target_branch_head_sha = gh_ops.get_branch_sha(
                        entry.repo_github, entry.target_branch
                    )
                entry.target_branch_head_sha = _group_target_branch_head_sha
            # Review gate (#253/#821): refuse to merge when a review is required
            # by the pipeline policy but no approved review is on the board.
            # --skip-review bypasses for trivial/docs-only merges where the
            # user has consciously decided review isn't needed.
            # #292 (Defect 3): skip this entry and try the next one in the
            # group rather than halting the whole group.  An un-reviewed entry
            # should not prevent a fully-approved sibling from merging.
            # #821: fail closed — when review is required but board is None
            # the approval cannot be confirmed; block rather than silently merge.
            if (
                not skip_review
                and config is not None
                and requires_review(entry, config)
                and (board is None or not has_approved_review(entry, board, gh_ops))
            ):
                msg = (
                    "review required but board unavailable to confirm approval"
                    if board is None
                    else "review required but not approved"
                )
                entry.error = msg
                events.append(MergeEvent(entry, "review_required", msg))
                continue  # #292: skip this entry; try the next in the group
            # Smoke gate (#465/#821): refuse to merge when the interactive smoke
            # is required by the pipeline policy but no passing/skipped verdict
            # is recorded on the work assignment.  Same skip-not-halt semantics
            # as the review gate above.
            # #821: fail closed — when smoke is required but board is None
            # the verdict cannot be confirmed; block rather than silently merge.
            if (
                not skip_smoke
                and config is not None
                and requires_smoke(entry, config)
                and (board is None or not has_smoke_verdict(entry, board, gh_ops))
            ):
                msg = (
                    "smoke test required but board unavailable to confirm verdict"
                    if board is None
                    else "smoke test required but no verdict recorded"
                )
                entry.error = msg
                events.append(MergeEvent(entry, "smoke_required", msg))
                continue  # skip this entry; try the next in the group
            # CI gate (#240): refuse to merge when checks are failed or
            # still running.  --force-merge overrides for the case where the
            # user has seen the failures and wants to merge anyway.
            # #292 (Defect 3): skip-and-proceed for CI gates too, same logic
            # as the review gate — a pending/failing CI entry should not
            # block an approved sibling in the same (repo, target) group.
            if not force_merge and ci.is_available:
                checks = ci.list_checks_for_pr(entry.repo_github, entry.pr_number)
                failed = failed_checks(checks)
                if failed:
                    summary = ", ".join(
                        f"{c.name} ({c.conclusion})" for c in failed
                    )
                    msg = f"checks failed: {summary}"
                    entry.error = msg
                    events.append(MergeEvent(entry, "checks_failed", msg))
                    continue  # #292: skip, don't halt the group
                pending = in_flight_checks(checks)
                if pending:
                    summary = ", ".join(c.name for c in pending)
                    msg = f"checks still running: {summary}"
                    entry.error = msg
                    events.append(MergeEvent(entry, "checks_pending", msg))
                    continue  # #292: skip, don't halt the group
            # #1318: cache is_epic_issue lookups for this entry — the same
            # referenced number can show up in both the PR body and one or
            # more commit messages below, and each lookup is a `gh` round
            # trip. Best-effort like every check in this block: a lookup
            # failure just means "not known to be an epic", never a block.
            _epic_cache: dict[int, bool] = {}

            def _is_epic(n: int) -> bool:
                if n not in _epic_cache:
                    try:
                        _epic_cache[n] = gh_ops.is_epic_issue(entry.repo_github, n)
                    except Exception:  # noqa: BLE001
                        _epic_cache[n] = False
                return _epic_cache[n]

            # #1196 hole 2 / #1318: GitHub's own closing-keyword magic reads
            # the PR body directly at merge time and never calls
            # `github_ops.close_issue` — that chokepoint's open-children
            # guard can't stop it. Scan the body for `Closes #N`/`Fixes
            # #N`/`Resolves #N` and downgrade to `Refs #N` for any N that
            # either currently has open children (#1196) or carries the
            # epic/tracking label (#1318 — an epic can have zero open
            # children today and still be the wrong thing to auto-close),
            # before the merge lands. Best effort throughout: a lint
            # failure must never block a merge.
            try:
                pr_body = gh_ops.get_pr_body(entry.repo_github, entry.pr_number)
            except Exception:  # noqa: BLE001
                pr_body = ""
            if pr_body:
                referenced = find_closing_references(pr_body)
                blocking: set[int] = set()
                for n in referenced:
                    try:
                        if gh_ops.has_open_children(entry.repo_github, n):
                            blocking.add(n)
                    except Exception:  # noqa: BLE001
                        pass
                    if _is_epic(n):
                        blocking.add(n)
                if blocking:
                    new_body, downgraded = downgrade_closing_keywords(pr_body, blocking)
                    if downgraded:
                        try:
                            gh_ops.edit_pr_body(entry.repo_github, entry.pr_number, new_body)
                            events.append(MergeEvent(
                                entry, "pr_body_downgraded",
                                "downgraded closing keyword to Refs for "
                                + ", ".join(f"#{n}" for n in downgraded)
                                + " (open children / epic — #1196/#1318)",
                            ))
                        except Exception as e:  # noqa: BLE001
                            events.append(MergeEvent(
                                entry, "pr_body_downgrade_failed",
                                f"could not downgrade PR #{entry.pr_number} body "
                                f"for {', '.join(f'#{n}' for n in downgraded)}: {e}",
                            ))

            # #1318: the PR-body scan above can't help with commit messages
            # — GitHub's closing-keyword scanner reads those too once they
            # land on the base branch (every original commit, unchanged, for
            # `--rebase`/`--merge`; and depending on repo settings, squash's
            # default commit body can pull the same text). There's no local
            # git checkout in this `gh`-only wire layer to amend and
            # force-push a rewritten message, so a hit here **blocks** the
            # merge rather than silently rewriting history. `force_merge`
            # overrides (same flag `--force-merge` already uses to skip the
            # CI gate) but the override is never silent — a warning event
            # still fires so it shows up in `coord merge` output and the
            # audit trail.
            try:
                commit_messages = gh_ops.get_pr_commit_messages(
                    entry.repo_github, entry.pr_number
                )
            except Exception:  # noqa: BLE001
                commit_messages = []
            commit_referenced: set[int] = set()
            for message in commit_messages:
                commit_referenced.update(find_closing_references(message))
            commit_epic_hits = sorted(n for n in commit_referenced if _is_epic(n))
            if commit_epic_hits:
                numbers_str = ", ".join(f"#{n}" for n in commit_epic_hits)
                msg = (
                    f"a commit message on this branch contains a closing keyword "
                    f"(Closes/Fixes/Resolves) for {numbers_str}, which carries the "
                    f"'epic' label — GitHub auto-closes it on merge regardless of "
                    f"the PR body (#1318). Reword the commit message(s) to "
                    f"'refs #N' / 'epic #N' and push, or pass --force-merge to "
                    f"merge anyway (the epic WILL still auto-close)."
                )
                if force_merge:
                    events.append(MergeEvent(
                        entry, "epic_closing_keyword_in_commit_forced", msg,
                    ))
                else:
                    entry.error = msg
                    events.append(MergeEvent(
                        entry, "epic_closing_keyword_in_commit", msg,
                    ))
                    continue  # #1318: refuse — never merge a branch that will
                    # auto-close an epic via a commit message we can't rewrite.

            # #1467: pre-flight linearity check. GitHub refuses to
            # rebase-merge any branch containing a merge commit ("This
            # branch can't be rebased") — a distinct failure from a content
            # conflict, and one GitHub's own `mergeable` field can't predict
            # (a branch with a merge commit still reads MERGEABLE). Detect
            # it via the PR's commit list — no local checkout is guaranteed
            # on the daemon host, so `git rev-list --merges` is the wrong
            # instrument here — and fall back to squash, which is always
            # valid and keeps the target branch linear.
            #
            # Fail-closed: `branch_has_merge_commit` is optional on `gh_ops`
            # (older stubs in tests predate #1467) and returns `None` on any
            # `gh` error or ambiguous response; either case leaves `method`
            # unchanged rather than guessing.
            merge_method = method
            if method == "rebase":
                _probe = getattr(gh_ops, "branch_has_merge_commit", None)
                if _probe is not None:
                    try:
                        _has_merge_commit = _probe(entry.repo_github, entry.pr_number)
                    except Exception:  # noqa: BLE001
                        _has_merge_commit = None
                    if _has_merge_commit is True:
                        merge_method = "squash"
                        events.append(MergeEvent(
                            entry, "method_fallback",
                            f"PR #{entry.pr_number} ({entry.branch}) contains a "
                            "merge commit and cannot be rebase-merged — "
                            "falling back to --squash (#1467)",
                        ))

            entry.last_attempt = time.time()
            entry.state = MERGING
            ok, msg = gh_ops.merge_pr(entry.repo_github, entry.pr_number, method=merge_method)
            if ok:
                entry.state = MERGED
                entry.error = None
                # #1213: audit any gate bypassed by a per-issue label override
                # BEFORE announcing the merge, so the "merged" event message
                # already carries the bypass note — a bypass is never silent.
                # Only fires on a real merge (never dry-run, handled above via
                # the side-effect-free _bypass_note) so previews can't write
                # phantom audit rows.
                _record_gate_bypass_audit(entry, config)
                bypass_note = _bypass_note(entry, config)
                # Deterministically close the linked issue.  GitHub's `Closes #N`
                # auto-close only fires when the PR *body* carries the keyword
                # AND it merges into the default branch; the worker-created-PR
                # path only asks the LLM for it and `fix(#N):` subjects aren't
                # closing keywords, so issues got stranded open (#806).
                # Best-effort — a close failure must not undo a successful merge.
                # Closing on GitHub keeps the daemon the sole DB writer: the next
                # reconcile/sync flips the cached row to closed (state.py).
                #
                # #1077: only for entries whose issue_number is actually
                # resolved by this PR (CLOSES_ISSUE_TYPES). A "mock-author"
                # entry's issue_number is the milestone's tracking issue —
                # closing it here would be the exact #1077 bug regardless of
                # what the PR body says.
                if entry.assignment_type in CLOSES_ISSUE_TYPES:
                    try:
                        gh_ops.close_issue(entry.repo_github, entry.issue_number)
                        events.append(MergeEvent(
                            entry, "merged",
                            f"merged PR #{entry.pr_number}; closed issue #{entry.issue_number}"
                            f"{bypass_note}",
                        ))
                    except Exception as e:  # noqa: BLE001 — never fail a merge on close
                        events.append(MergeEvent(
                            entry, "merged",
                            f"merged PR #{entry.pr_number} (warning: could not "
                            f"close issue #{entry.issue_number}: {e}){bypass_note}",
                        ))
                else:
                    events.append(MergeEvent(
                        entry, "merged",
                        f"merged PR #{entry.pr_number}; issue #{entry.issue_number} "
                        f"left open (assignment type {entry.assignment_type!r} "
                        f"does not close its tracking issue, #1077){bypass_note}",
                    ))
                continue
            entry.state = CONFLICT
            entry.error = msg
            events.append(MergeEvent(entry, "conflict", msg))
            continue  # #735: park this entry; siblings in same group still merge

    return events


# ── Drop / prune (#732) ──────────────────────────────────────────────────

def drop_entry(assignment_id: str) -> bool:
    """Remove exactly the merge_queue row keyed to *assignment_id*.

    Returns ``True`` when a row was deleted, ``False`` when no matching row
    was found.  This is the surgical mutation that ``coord merge --drop`` and
    the TUI "drop" action use; it never touches other rows.

    Because the queue lives on the daemon host, callers on thin clients must
    route through the daemon (``/merge`` endpoint with ``"drop": aid`` in the
    body) rather than calling this directly — the daemon guard pattern is the
    same as ``coord merge`` (#584).

    #1477: *assignment_id* is resolved via :func:`resolve_entry_key`, so the
    durable ``repo#issue`` form works here too — not just a raw assignment
    id, which can go stale across a drop + re-enqueue cycle.
    """
    conn = get_connection()
    entry = resolve_entry_key(load_queue(), assignment_id)
    if entry is None:
        return False
    with conn:
        cursor = conn.execute(
            "DELETE FROM merge_queue WHERE assignment_id = ?", (entry.assignment_id,)
        )
    return cursor.rowcount > 0


def prune_stale_queue_entries(dry_run: bool = False) -> list["QueuedMerge"]:
    """Remove merge_queue entries whose issue is closed or PR is already merged.

    Returns the list of pruned entries so callers can surface them in output.

    Only non-``MERGED`` entries are inspected — entries already recorded as
    ``MERGED`` are correct history and are left untouched.

    Uses :func:`coord.github_ops.issue_is_closed` and
    :func:`coord.github_ops.pr_is_merged`, both of which **fail-open**
    (return ``False`` on any ``gh`` error) so a transient GitHub/CLI failure
    never silently prunes a live entry.
    """
    from coord import github_ops  # noqa: PLC0415

    entries = load_queue()
    stale: list[QueuedMerge] = []
    surviving: list[QueuedMerge] = []

    for entry in entries:
        if entry.state == MERGED:
            surviving.append(entry)
            continue

        is_stale = False
        if github_ops.issue_is_closed(entry.repo_github, entry.issue_number):
            is_stale = True
        elif entry.branch and github_ops.pr_is_merged(entry.repo_github, entry.branch):
            is_stale = True

        if is_stale:
            stale.append(entry)
        else:
            surviving.append(entry)

    if not dry_run and stale:
        save_queue(surviving)

    return stale


# ── Convenience ──────────────────────────────────────────────────────────

def pending_summary(items: list[QueuedMerge]) -> dict[str, list[QueuedMerge]]:
    """Group items for display in `coord status`. Returns {repo_name: [entries]}."""
    out: dict[str, list[QueuedMerge]] = {}
    for entry in items:
        if entry.state in (MERGED, SKIPPED):
            continue
        out.setdefault(entry.repo_name, []).append(entry)
    return out
