"""Merge queue: sequence completed assignments into their target branches.

Two-layer design so the logic is testable without hitting `gh`:

- Data + sequencing live here (pure functions over QueuedMerge).
- Wire calls (gh pr create / merge / size) are passed in as `gh_ops` so
  tests can substitute a stub. `coord.cli` wires the real `coord.github_ops`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from coord.audit import record_audit
from coord.ci_store import CiStore, NoOpCi, failed_checks, in_flight_checks, summarize
from coord.db import get_connection
from coord.models import CLOSES_ISSUE_TYPES, WORK_LIKE_TYPES, Assignment
from coord.pr_body_lint import downgrade_closing_keywords, find_closing_references
from coord.state import COORD_DIR

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

_REBASEABLE_SIGNALS = (
    "could not be rebased",
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


def has_approved_review(entry: "QueuedMerge", board) -> bool:
    """True when a completed review with ``review_verdict='approve'`` exists
    on *board* for the work assignment behind *entry*.

    Scans both active and completed assignments — a review whose findings
    were just posted may still be on ``board.active`` for a tick before
    reconcile moves it to ``completed``.  We accept either, since the
    verdict is what matters.

    #292 (Defect 1): after a review bounce the queue entry may be keyed to
    the *original* work assignment while the approved re-review is linked to
    the *fix* work assignment.  To handle this we collect **all** work
    assignment IDs that share the same branch (including the entry's own ID)
    and accept any approved review that points to any of them.
    """
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])

    # Seed with the entry's own assignment_id, then expand to any work
    # assignment on the same branch (e.g. fix workers from the auto-loop).
    branch_work_ids: set[str] = set()
    if entry.assignment_id:
        branch_work_ids.add(entry.assignment_id)
    for a in pool:
        if getattr(a, "type", None) not in WORK_LIKE_TYPES:
            continue
        aid = getattr(a, "assignment_id", None)
        branch = getattr(a, "branch", None)
        if aid and branch and branch == entry.branch:
            branch_work_ids.add(aid)

    if not branch_work_ids:
        return False

    # #821: commit-bound check.  If the entry has a branch_head_sha (set at
    # process() time from the live branch tip) and the review has a
    # review_head_sha (set when the review assignment ran), an approval only
    # counts when the two SHAs match — i.e. no new commits were pushed after
    # the review completed.  When either SHA is absent (pre-821 rows or SHA
    # tracking unavailable) the check is skipped (backward-compatible).
    current_sha = getattr(entry, "branch_head_sha", None)

    for a in pool:
        if getattr(a, "type", None) != "review":
            continue
        if getattr(a, "review_of_assignment_id", None) not in branch_work_ids:
            continue
        if getattr(a, "review_verdict", None) != "approve":
            continue
        review_sha = getattr(a, "review_head_sha", None)
        if review_sha is not None and current_sha is not None and review_sha != current_sha:
            continue  # stale: branch moved past the commit the review covered
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


def passes_merge_gates(a, config, board) -> bool:
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
    """
    if requires_review(a, config) and not has_approved_review(a, board):
        return False
    if requires_smoke(a, config) and not has_smoke_verdict(a, board):
        return False
    return True


def has_smoke_verdict(entry: "QueuedMerge", board) -> bool:
    """True when the smoke requirement for *entry* is satisfied.

    The gate **fails open**: if no work assignment can be found on the board
    for the entry's branch (e.g. board was cleared, manual queue entry, or
    the assignment pre-dates board persistence), this returns ``True`` so that
    the merge is not silently blocked without evidence.

    The gate **fails closed** (returns ``False``) only when we can positively
    identify the work assignment(s) on the branch and none of them carries a
    ``test_state in ('passed', 'skipped')`` verdict.

    Collects all work assignment IDs that share the same branch (including
    the entry's own ID) to handle bounce/fix-work chains.
    """
    pool = list(getattr(board, "completed", []) or []) + list(
        getattr(board, "active", []) or []
    )

    # Seed with the entry's own assignment_id, then expand to any work
    # assignment on the same branch (e.g. fix workers from the auto-loop).
    branch_work_ids: set[str] = set()
    if entry.assignment_id:
        branch_work_ids.add(entry.assignment_id)
    for a in pool:
        if getattr(a, "type", None) not in WORK_LIKE_TYPES:
            continue
        aid = getattr(a, "assignment_id", None)
        branch = getattr(a, "branch", None)
        if aid and branch and branch == entry.branch:
            branch_work_ids.add(aid)

    # Collect work assignments that are explicitly present on the board.
    branch_work = [
        a for a in pool
        if getattr(a, "assignment_id", None) in branch_work_ids
        and getattr(a, "type", None) in WORK_LIKE_TYPES
    ]
    # Fail open: no work assignment found → can't block without evidence.
    if not branch_work:
        return True

    # Work found — check whether any carries a terminal smoke verdict.
    return any(
        getattr(a, "test_state", None) in ("passed", "skipped")
        for a in branch_work
    )


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

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        """Return the current HEAD SHA for *branch*, or None on failure.

        Used to populate ``QueuedMerge.branch_head_sha`` at process() time so
        ``has_approved_review`` can reject stale approvals (#821).  Returning
        ``None`` (on any network/auth failure) is safe — the staleness check
        is skipped for rows without a SHA, preserving backward compatibility.
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

    completed = list(getattr(board, "completed", []) or [])
    existing_queue = load_queue()

    for a in completed:
        if getattr(a, "type", None) not in WORK_LIKE_TYPES:
            continue
        if getattr(a, "status", None) != "done":
            continue
        branch = getattr(a, "branch", None)
        aid = getattr(a, "assignment_id", None)
        if not branch or not aid:
            continue

        repo_name = getattr(a, "repo_name", None)
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

        if refresh_entry_assignment(
            a,
            repo_github=repo_cfg.github,
            target_branch=repo_cfg.default_branch,
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
) -> tuple[str, str | None]:
    """Return *(status, reason)* for a single PENDING merge-queue entry.

    Evaluates gates in the same order as :func:`process` — review → smoke →
    CI — so the plan shown to the operator is byte-for-byte what merge would
    do.  Both :func:`plan` and :func:`process` delegate to this helper so they
    can never diverge.

    Returns ``(PLAN_READY, None)`` when all gates pass.
    Returns ``(PLAN_BLOCKED, reason)`` when any gate blocks.

    The *ci_store* gate is only evaluated when both *ci_store* is provided
    **and** the entry has a ``pr_number`` (CI is checked per-PR, not per-branch).
    This mirrors the live-merge behaviour: a ``PENDING`` entry with no PR yet
    opened is not blocked on CI — the PR hasn't been created yet.
    """
    if config is not None and board is not None:
        if requires_review(entry, config) and not has_approved_review(entry, board):
            return PLAN_BLOCKED, "review not approved"
        if requires_smoke(entry, config) and not has_smoke_verdict(entry, board):
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
         evaluates review / smoke / CI gates live against *board* + *config*.
       - Look up the issue's milestone from the ``issues`` table.

    The function is intentionally **read-only** — no side effects, no DB writes.
    Pass ``board=None`` and/or ``config=None`` to skip gate evaluation (useful
    in test scenarios that only care about ordering).
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

            if entry.state == PENDING:
                base_status, reason = _entry_gate_status(
                    entry, board, config, ci_store
                )

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
    Handles bounce/fix chains: any work assignment on the same branch counts.
    """
    pool = (
        list(getattr(board, "completed", []) or [])
        + list(getattr(board, "active", []) or [])
    )
    aid = getattr(a, "assignment_id", None)
    branch = getattr(a, "branch", None)

    # Seed with the entry's own id, then expand to any work assignment sharing
    # the branch (e.g. a fix worker dispatched after a review bounce).
    branch_work_ids: set[str] = set()
    if aid:
        branch_work_ids.add(aid)
    for x in pool:
        if getattr(x, "type", None) not in WORK_LIKE_TYPES:
            continue
        x_aid = getattr(x, "assignment_id", None)
        if x_aid and branch and getattr(x, "branch", None) == branch:
            branch_work_ids.add(x_aid)

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
    kind: str  # "opened" | "sized" | "merged" | "conflict" | "skipped" | "error"
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

    Dry-run applies both the review and smoke gates so output reflects what
    a real run would do.  CI cannot be checked without a real PR number.

    Mutates `items` in place; the caller saves the queue after.
    """
    events: list[MergeEvent] = []
    ci: CiStore = ci_store if ci_store is not None else NoOpCi()

    groups: dict[tuple[str, str], list[QueuedMerge]] = {}
    for entry in items:
        if entry.state != PENDING:
            continue
        groups.setdefault((entry.repo_github, entry.target_branch), []).append(entry)

    for group in groups.values():
        if dry_run:
            for entry in group:
                events.append(MergeEvent(entry, "opened", f"(dry run) would open PR for {entry.branch}"))
            ordered = group if presorted else sequence(group)
            for entry in ordered:
                # #821: populate branch_head_sha for the commit-bound approval
                # staleness check in has_approved_review.  Only when the board
                # is live (board=None blocks unconditionally; no SHA needed).
                if board is not None and entry.branch_head_sha is None:
                    entry.branch_head_sha = gh_ops.get_branch_sha(
                        entry.repo_github, entry.branch
                    )
                # #292 (Defect 4): apply the review gate in dry-run so output
                # reflects real behaviour.  CI cannot be checked in dry-run
                # (no PR exists yet), so review and smoke gates are evaluated.
                # #821: fail closed — if review is required but board is None
                # (approval cannot be confirmed) block the entry.
                if (
                    not skip_review
                    and config is not None
                    and requires_review(entry, config)
                    and (board is None or not has_approved_review(entry, board))
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
                    and (board is None or not has_smoke_verdict(entry, board))
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
                events.append(MergeEvent(
                    entry, "merged",
                    f"(dry run) would merge {entry.branch} → {entry.target_branch}"
                    f"{_bypass_note(entry, config)}",
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
                and (board is None or not has_approved_review(entry, board))
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
                and (board is None or not has_smoke_verdict(entry, board))
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
            # #1196 hole 2: GitHub's own closing-keyword magic reads the PR
            # body directly at merge time and never calls
            # `github_ops.close_issue` — that chokepoint's open-children
            # guard can't stop it. Scan the body for `Closes #N`/`Fixes
            # #N`/`Resolves #N` and downgrade to `Refs #N` for any N that
            # currently has open children, before the merge lands. Best
            # effort throughout: a lint failure must never block a merge.
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
                        continue
                if blocking:
                    new_body, downgraded = downgrade_closing_keywords(pr_body, blocking)
                    if downgraded:
                        try:
                            gh_ops.edit_pr_body(entry.repo_github, entry.pr_number, new_body)
                            events.append(MergeEvent(
                                entry, "pr_body_downgraded",
                                "downgraded closing keyword to Refs for "
                                + ", ".join(f"#{n}" for n in downgraded)
                                + " (open children — #1196)",
                            ))
                        except Exception as e:  # noqa: BLE001
                            events.append(MergeEvent(
                                entry, "pr_body_downgrade_failed",
                                f"could not downgrade PR #{entry.pr_number} body "
                                f"for {', '.join(f'#{n}' for n in downgraded)}: {e}",
                            ))
            entry.last_attempt = time.time()
            entry.state = MERGING
            ok, msg = gh_ops.merge_pr(entry.repo_github, entry.pr_number, method=method)
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
    """
    conn = get_connection()
    with conn:
        cursor = conn.execute(
            "DELETE FROM merge_queue WHERE assignment_id = ?", (assignment_id,)
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
