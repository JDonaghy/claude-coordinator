"""Issue claim detection — prevent two agents picking up the same issue.

Without this, two coordinator runs (e.g. two operators, or a manual dispatch
racing an auto-dispatch) can both kick off work on the same issue because
neither side notices the other. The fix is a simple pre-dispatch check:

1. Is there an active board assignment for `(issue_number, repo_name)`?
2. Does the remote already have a branch matching `issue-{N}-*` for this
   repo? (Workers create branches in that shape — its existence is treated
   as a claim signal even if our board doesn't know about it yet.)

If either is true, the dispatch site refuses with a clear message. Reviews
and smoke tests run *after* a worker has pushed a branch, so they don't
participate in this check — they have their own dedupe (no two reviews of
the same completed assignment).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from coord.models import Board


# A branch_lookup takes (repo_github, issue_number) and returns the matching
# branch names. Injected so tests don't hit GitHub.
BranchLookup = Callable[[str, int], list[str]]


@dataclass
class Claim:
    """Evidence that an issue is already being worked on."""

    issue_number: int
    repo_name: str
    source: str  # "board" or "remote_branch"
    machine_name: str | None = None
    branch: str | None = None
    assignment_id: str | None = None


def find_work_claim(
    issue_number: int,
    repo_name: str,
    repo_github: str,
    board: Board,
    *,
    branch_lookup: BranchLookup | None = None,
) -> Claim | None:
    """Return a `Claim` if dispatching new work for this issue would conflict.

    Checks the board first (cheap, local) then the remote (one `gh` call).
    Returns the first hit — callers only need to know *that* it's claimed,
    not enumerate every claim source.
    """
    for a in board.active:
        if a.issue_number == issue_number and a.repo_name == repo_name:
            if a.status == "failed":
                continue
            if a.type in ("plan", "review", "smoke"):
                continue
            return Claim(
                issue_number=issue_number,
                repo_name=repo_name,
                source="board",
                machine_name=a.machine_name,
                branch=a.branch,
                assignment_id=a.assignment_id,
            )

    lookup = branch_lookup or _default_branch_lookup
    branches = lookup(repo_github, issue_number)
    if branches:
        return Claim(
            issue_number=issue_number,
            repo_name=repo_name,
            source="remote_branch",
            branch=branches[0],
        )
    return None


def claim_message(claim: Claim) -> str:
    """Human-readable error for a refused dispatch."""
    if claim.source == "board":
        parts = [
            f"issue #{claim.issue_number} ({claim.repo_name}) already claimed",
        ]
        if claim.machine_name:
            parts.append(f"by {claim.machine_name}")
        if claim.assignment_id:
            parts.append(f"(assignment {claim.assignment_id})")
        if claim.branch:
            parts.append(f"on branch {claim.branch}")
        return " ".join(parts)
    return (
        f"issue #{claim.issue_number} ({claim.repo_name}) already claimed: "
        f"remote branch {claim.branch} exists"
    )


# ── Dedupe for downstream auto-dispatch (review / smoke) ────────────────────


def has_active_followup(
    board: Board,
    *,
    of_assignment_id: str | None,
    assignment_type: str,
) -> bool:
    """True if `board.active` already has a review/smoke of the given work.

    Used by `dispatch_review`/`dispatch_smoke` to skip when one is already in
    flight. The check is by `review_of_assignment_id` rather than `(issue,
    repo)` so that re-dispatching after a worker re-runs the same issue
    isn't accidentally blocked.
    """
    if of_assignment_id is None:
        return False
    for a in board.active:
        if a.type != assignment_type:
            continue
        if a.review_of_assignment_id == of_assignment_id:
            return True
    return False


def has_active_work_followup(
    board: Board,
    *,
    repo_name: str,
    issue_number: int,
) -> bool:
    """True if a work or conflict-fix assignment is actively running for (repo, issue).

    Used before dispatching a review to skip when a coord-bounce fix is
    actively rewriting the branch — dispatching a review against stale code
    produces a verdict on code that's about to change and causes unnecessary
    churn.  The existing ``has_active_followup`` covers duplicate-review
    dedupe; this covers the orthogonal case where a *work* re-run (not a
    review) is live for the same issue.

    Called from both the reconcile review-dispatch loop and ``dispatch_review``
    for defence in depth.
    """
    _WORK_TYPES = frozenset({"work", "conflict-fix"})
    for a in board.active:
        if a.type not in _WORK_TYPES:
            continue
        if a.status == "failed":
            continue
        if a.repo_name == repo_name and a.issue_number == issue_number:
            return True
    return False


# ── Default branch lookup (uses gh) ─────────────────────────────────────────


def _default_branch_lookup(repo_github: str, issue_number: int) -> list[str]:
    """Return remote branches whose name starts with `issue-{N}-`.

    Uses `gh api repos/.../git/matching-refs/heads/issue-{N}-`. Empty result
    on any lookup failure — we'd rather wave through a dispatch than block
    on a transient GH error.
    """
    from coord import github_ops

    try:
        raw = github_ops._gh(
            "api",
            f"repos/{repo_github}/git/matching-refs/heads/issue-{issue_number}-",
        )
    except RuntimeError:
        return []
    try:
        refs = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(refs, list):
        return []
    branches: list[str] = []
    for r in refs:
        if not isinstance(r, dict):
            continue
        ref = r.get("ref", "")
        if isinstance(ref, str) and ref.startswith("refs/heads/"):
            branches.append(ref[len("refs/heads/"):])
    # Drop branches already fully merged into the default branch — a merged
    # branch is finished work, not an active claim. A stale merged branch (e.g. a
    # PR head that wasn't auto-deleted on merge) must not block new work on the
    # issue forever (the chat→work block on a long-merged issue-N-* branch).
    return _drop_merged_branches(repo_github, branches)


def _repo_default_branch(repo_github: str) -> str | None:
    """The repo's default branch via the GH API, or None on any error."""
    from coord import github_ops

    try:
        data = json.loads(github_ops._gh("api", f"repos/{repo_github}"))
    except (RuntimeError, ValueError):
        return None
    val = data.get("default_branch") if isinstance(data, dict) else None
    return val if isinstance(val, str) and val else None


def _drop_merged_branches(repo_github: str, branches: list[str]) -> list[str]:
    """Filter out branches fully merged into the repo's default branch.

    A branch with zero commits ahead of the default branch is finished work,
    not an active claim, so it must not block a fresh dispatch. Conservative on
    every uncertainty — unknown default branch, compare-API error, or a branch
    that IS ahead — keeps the branch as a claim (fail toward blocking duplicate
    work, never toward allowing it).
    """
    if not branches:
        return branches
    from coord import github_ops

    default_branch = _repo_default_branch(repo_github)
    if not default_branch:
        return branches  # can't determine merged-ness → keep all (conservative)
    kept: list[str] = []
    for b in branches:
        if b == default_branch:
            continue
        try:
            cmp = json.loads(
                github_ops._gh(
                    "api", f"repos/{repo_github}/compare/{default_branch}...{b}"
                )
            )
            ahead = cmp.get("ahead_by") if isinstance(cmp, dict) else None
        except (RuntimeError, ValueError):
            ahead = None
        if ahead == 0:
            continue  # fully merged → not an active claim
        kept.append(b)  # ahead > 0, or unknown → keep (conservative)
    return kept
