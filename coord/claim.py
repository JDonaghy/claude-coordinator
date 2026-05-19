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
    return branches
