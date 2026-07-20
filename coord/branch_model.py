"""Pipeline v2 Phase 4 git model (#934, docs/PIPELINE_V2.md "Git model").

Resolves the *base branch* a milestone issue's work should branch off of,
a PR should diff/merge against, and a review should compare to — instead of
the flat ``repo.default_branch`` every dispatch/review/merge/reconcile call
site assumed before #934.

The model: a repo that opts in (sets ``Repo.develop_branch`` in
``coordinator.yml``) gets a ``develop`` integration branch; each milestone
gets its own ``feature/ms-NN`` branch off ``develop``; issues that belong to
that milestone (its GitHub Milestone, per ``coord.github_ops.get_issue``'s
``milestone`` field) branch off ``feature/ms-NN`` and merge back into it.
``feature/ms-NN`` itself only merges to ``develop`` via ``coord milestone
ship`` (Gate D, ``coord/commands/milestone.py``), gated on Gate B + Gate C.

**Opt-in, not a rewrite.** A repo that never sets ``develop_branch`` — or an
issue with no GitHub Milestone — resolves to exactly today's behavior
(``repo.default_branch or "main"``). This is deliberate: it's the guard
against breaking in-flight work on ``main`` the moment one repo adopts the
new model (#934's own "migration story" requirement). Every call site also
skips the milestone-number lookup entirely (no `gh` call at all) when the
repo hasn't set ``develop_branch`` — a repo that hasn't opted in pays zero
extra cost, network or otherwise.

Wired into the 5 call sites named in #934's scope: ``coord/dispatch.py``
(worker branch-from), ``coord/review.py`` (PR base + diff base),
``coord/merge_queue.py`` (merge target), ``coord/reconcile.py`` (retry /
stale-PR default-branch comparisons). Deliberately NOT wired (yet) into
``coord/commands/dispatch_workers.py``'s interactive dispatch surface
(``--review-of``/``--smoke-of``/``--fix-of``/``--merge-of``/etc.) — same
mechanical swap, much larger surface, left for a follow-up issue per #934's
own "expect to decompose further when picked up" guidance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from coord import github_ops

if TYPE_CHECKING:
    from coord.models import Repo

__all__ = [
    "feature_branch_name",
    "resolve_base_branch",
    "resolve_base_branch_for_issue",
    "ensure_feature_branch_exists",
    "fetch_issue_milestone_number",
]

# (repo_github, branch) -> exists on remote. Mirrors github_ops.branch_exists_on_remote.
BranchExistsFetch = Callable[[str, str], bool]
# (repo_github, branch) -> tip SHA. Mirrors github_ops.get_default_branch_head.
BranchShaFetch = Callable[[str, str], str]
# (repo_github, branch, sha) -> True on success. Mirrors a new github_ops.create_remote_branch.
BranchCreate = Callable[[str, str, str], bool]


def feature_branch_name(milestone_number: int) -> str:
    """The per-milestone feature branch name: ``feature/ms-NN``."""
    return f"feature/ms-{milestone_number}"


def resolve_base_branch(repo: "Repo", milestone_number: int | None) -> str:
    """The branch a milestone issue's work/review/merge should target.

    Returns ``feature_branch_name(milestone_number)`` only when *repo* has
    opted into the git model (``develop_branch`` set) **and** the issue
    belongs to a known milestone. Otherwise falls back to
    ``repo.default_branch or "main"`` — today's behavior, unchanged.

    Reads both attributes via ``getattr`` (default ``None``) rather than
    direct attribute access — some test doubles construct a minimal
    ``Repo``-shaped stand-in that predates ``develop_branch``, and a
    fail-open default (today's flat ``default_branch`` behavior) is exactly
    what a repo without the field should get anyway.
    """
    develop_branch = getattr(repo, "develop_branch", None)
    if develop_branch and milestone_number is not None:
        return feature_branch_name(milestone_number)
    return getattr(repo, "default_branch", None) or "main"


def fetch_issue_milestone_number(
    repo_github: str, issue_number: int, *, cache: dict | None = None,
) -> int | None:
    """Best-effort fetch of an issue's GitHub Milestone number.

    ``None`` when the issue has no milestone OR the fetch fails — **fail
    open**, mirroring ``coord.github_ops.work_is_terminal``: a transient
    ``gh`` error must never block a legitimate dispatch/review/merge/
    reconcile pass, it just falls back to :func:`resolve_base_branch`'s
    ``default_branch`` behavior for that one call.

    *cache* — optional ``dict`` keyed by ``(repo_github, issue_number)``,
    shared across a batch (e.g. one ``enqueue_approved_work`` pass), so a
    burst of entries for the same repo/issue costs one ``gh`` round-trip.
    Shared-module-level fetchers (``coord.review._fetch_issue_milestone_
    number`` and friends) delegate here rather than duplicating the ``gh``
    call, so every call site fails open the same way.

    Callers should only invoke this when the repo has opted into the git
    model (``repo.develop_branch`` set) — see each call site's own guard —
    so a repo that hasn't opted in never pays for this ``gh`` round-trip at
    all, not even a fail-open one.
    """
    key = (repo_github, issue_number)
    if cache is not None and key in cache:
        return cache[key]
    try:
        issue_data = github_ops.get_issue(repo_github, issue_number)
        milestone = issue_data.get("milestone") or {}
        result = milestone.get("number") if isinstance(milestone, dict) else None
    except RuntimeError:
        result = None
    if cache is not None:
        cache[key] = result
    return result


def resolve_base_branch_for_issue(repo: "Repo", issue_data: dict) -> str:
    """Like :func:`resolve_base_branch`, deriving the milestone from a
    ``coord.github_ops.get_issue()``-shaped dict (its ``milestone`` field —
    ``None`` or ``{"number": ..., "title": ...}``).

    Callers that already fetched the issue (nearly every call site in
    ``dispatch.py``/``review.py``/``merge_queue.py``/``reconcile.py`` does,
    for the title/body) get milestone-awareness for free — no extra GitHub
    call, no new field threaded through ``Assignment``/DB persistence.
    """
    milestone = issue_data.get("milestone") or {}
    milestone_number = milestone.get("number") if isinstance(milestone, dict) else None
    return resolve_base_branch(repo, milestone_number)


def ensure_feature_branch_exists(
    repo: "Repo",
    milestone_number: int,
    *,
    exists: BranchExistsFetch = github_ops.branch_exists_on_remote,
    get_sha: BranchShaFetch = github_ops.get_default_branch_head,
    create: BranchCreate = github_ops.create_remote_branch,
) -> str:
    """Idempotently create ``feature/ms-NN`` off ``repo.develop_branch`` if
    it doesn't already exist on the remote. Returns the branch name.

    Raises ``ValueError`` if *repo* hasn't opted into the git model
    (``develop_branch`` unset) — callers should only invoke this after
    confirming ``repo.develop_branch`` is set (mirrors the guard in
    :func:`resolve_base_branch`).
    """
    if not repo.develop_branch:
        raise ValueError(
            f"repo {repo.name!r} has no develop_branch configured — "
            "cannot create a feature branch for it"
        )
    branch = feature_branch_name(milestone_number)
    if exists(repo.github, branch):
        return branch
    sha = get_sha(repo.github, repo.develop_branch)
    create(repo.github, branch, sha)
    return branch
