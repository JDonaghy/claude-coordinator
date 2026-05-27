"""Merge queue: sequence completed assignments into their target branches.

Two-layer design so the logic is testable without hitting `gh`:

- Data + sequencing live here (pure functions over QueuedMerge).
- Wire calls (gh pr create / merge / size) are passed in as `gh_ops` so
  tests can substitute a stub. `coord.cli` wires the real `coord.github_ops`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from coord.ci_store import CiStore, NoOpCi, failed_checks, in_flight_checks, summarize
from coord.db import get_connection
from coord.models import Assignment
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

    Honours both ``config.reviews.enabled`` (the master switch for the
    adversarial review feature) and ``config.pipeline.default_gates`` (which
    pipeline stages are enforced for work assignments).  A label-specific
    pipeline override on the issue is not consulted here — the merge gate
    operates on the *default* policy because the merge_queue entry doesn't
    carry the issue's gate list.  Callers wanting per-issue overrides should
    set ``--skip-review`` for the eligible entry instead.
    """
    if not getattr(config, "reviews", None) or not config.reviews.enabled:
        return False
    pipeline = getattr(config, "pipeline", None)
    if pipeline is None:
        return True
    return "review" in (pipeline.default_gates or [])


def has_approved_review(entry: "QueuedMerge", board) -> bool:
    """True when a completed review with ``review_verdict='approve'`` exists
    on *board* for the work assignment behind *entry*.

    Scans both active and completed assignments — a review whose findings
    were just posted may still be on ``board.active`` for a tick before
    reconcile moves it to ``completed``.  We accept either, since the
    verdict is what matters.
    """
    target_id = entry.assignment_id
    if not target_id:
        return False
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    for a in pool:
        if getattr(a, "type", None) != "review":
            continue
        if getattr(a, "review_of_assignment_id", None) != target_id:
            continue
        if getattr(a, "review_verdict", None) == "approve":
            return True
    return False


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


class GhOps(Protocol):
    """Minimal interface the queue needs from github_ops. Tests pass a stub."""

    def create_pr(
        self, repo: str, *, base: str, head: str, title: str, body: str
    ) -> dict: ...

    def get_pr_size(self, repo: str, number: int) -> int: ...

    def merge_pr(self, repo: str, number: int, method: str = "rebase") -> tuple[bool, str]: ...


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
                    pr_number, pr_url, size, last_attempt, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.assignment_id, item.repo_name, item.repo_github,
                    item.branch, item.target_branch, item.issue_number,
                    item.issue_title, item.state, item.pr_number, item.pr_url,
                    item.size, item.last_attempt, item.error,
                ),
            )


# ── Enqueue ──────────────────────────────────────────────────────────────

def enqueue(
    assignment: Assignment,
    repo_github: str,
    target_branch: str,
) -> QueuedMerge | None:
    """Add a completed assignment to the queue if it isn't already there.

    Returns the new entry, or None if it was already queued or has no branch.

    Dedup is by ``(repo_github, branch)`` — the queue's natural key is the
    branch we'd merge, not the assignment_id.  Multiple work assignments
    routinely target the same branch (original + fix-1 in the auto-loop,
    original + PR-creator from ``coord pr``); they should not produce
    duplicate rows. (#274)
    """
    if not assignment.branch:
        return None
    items = load_queue()
    if any(
        x.assignment_id == assignment.assignment_id
        or (x.repo_github == repo_github and x.branch == assignment.branch)
        for x in items
    ):
        return None
    entry = QueuedMerge(
        assignment_id=assignment.assignment_id or "",
        repo_name=assignment.repo_name,
        repo_github=repo_github,
        branch=assignment.branch,
        target_branch=target_branch,
        issue_number=assignment.issue_number,
        issue_title=assignment.issue_title,
    )
    items.append(entry)
    save_queue(items)
    return entry


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
    return (
        f"Closes #{entry.issue_number}\n\n"
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
) -> list[MergeEvent]:
    """Open PRs, size them, then merge each pending item.

    Items are grouped by (repo_github, target_branch); a conflict in one
    group halts only that group. Within a group, items are merged in input
    order — call `sequence(group)` first if you want size-based ordering.
    Set `presorted=True` to make that explicit at call sites.

    When ``ci_store`` is provided and available, each PR is checked against
    its CI status before merge.  A failed check produces a ``checks_failed``
    event and halts the group; a still-running check produces ``checks_pending``
    and halts the group.  ``force_merge=True`` skips this gate (the user has
    already seen the failures and chosen to merge anyway).

    #253: When both *config* and *board* are supplied and the entry requires
    a review (``reviews.enabled`` and ``"review"`` in ``pipeline.default_gates``)
    but no approved review exists on the board, a ``review_required`` event
    is emitted and the group is halted.  ``skip_review=True`` bypasses this
    gate.  When *config* or *board* is None the gate is silently skipped
    (legacy callers and tests that don't construct a board still work).

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
                events.append(MergeEvent(
                    entry, "merged",
                    f"(dry run) would merge {entry.branch} → {entry.target_branch}",
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
            # Review gate (#253): refuse to merge when a review is required by
            # the pipeline policy but no approved review is on the board.
            # --skip-review bypasses for trivial/docs-only merges where the
            # user has consciously decided review isn't needed.
            if (
                not skip_review
                and config is not None
                and board is not None
                and requires_review(entry, config)
                and not has_approved_review(entry, board)
            ):
                msg = "review required but not approved"
                entry.error = msg
                events.append(MergeEvent(entry, "review_required", msg))
                break  # halt this (repo, target) group
            # CI gate (#240): refuse to merge when checks are failed or
            # still running.  --force-merge overrides for the case where the
            # user has seen the failures and wants to merge anyway.
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
                    break  # halt this (repo, target) group
                pending = in_flight_checks(checks)
                if pending:
                    summary = ", ".join(c.name for c in pending)
                    msg = f"checks still running: {summary}"
                    entry.error = msg
                    events.append(MergeEvent(entry, "checks_pending", msg))
                    break
            entry.last_attempt = time.time()
            entry.state = MERGING
            ok, msg = gh_ops.merge_pr(entry.repo_github, entry.pr_number, method=method)
            if ok:
                entry.state = MERGED
                entry.error = None
                events.append(MergeEvent(entry, "merged", f"merged PR #{entry.pr_number}"))
                continue
            entry.state = CONFLICT
            entry.error = msg
            events.append(MergeEvent(entry, "conflict", msg))
            break  # halt this (repo, target) group on conflict

    return events


# ── Convenience ──────────────────────────────────────────────────────────

def pending_summary(items: list[QueuedMerge]) -> dict[str, list[QueuedMerge]]:
    """Group items for display in `coord status`. Returns {repo_name: [entries]}."""
    out: dict[str, list[QueuedMerge]] = {}
    for entry in items:
        if entry.state in (MERGED, SKIPPED):
            continue
        out.setdefault(entry.repo_name, []).append(entry)
    return out
