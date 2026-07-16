"""GitHub sub-issues backend for :mod:`coord.parentage` (#1195).

Shells out to ``gh api`` via :mod:`coord.github_ops`'s sub-issues wrappers.
Mirrors the :class:`coord.ci_store.CiStore` / :class:`coord.ci_github.GitHubCi`
split: the Protocol + pure fallback adapter live in ``coord.parentage``, and
this module is the live-API adapter, kept separate so a test can import the
Protocol/dataclasses without dragging in ``github_ops``.
"""

from __future__ import annotations

from coord import github_ops
from coord.parentage import Child, ParentRef


class GitHubParentage:
    """Live adapter over the GitHub sub-issues REST API.

    ``body``/``epics`` are accepted for :class:`coord.parentage.
    ParentageStore` shape parity with :class:`coord.parentage.
    MarkdownParentage` but ignored here — every read is a live ``gh api``
    call, so there's nothing cached to fall back to.
    """

    def children(
        self, repo_github: str, issue_number: int, *, body: str = "",
    ) -> list[Child]:
        raw = github_ops.get_sub_issues(repo_github, issue_number)
        children: list[Child] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            number = entry.get("number")
            if number is None:
                continue
            children.append(Child(number=int(number), state=str(entry.get("state") or "open")))
        return children

    def parent(
        self,
        repo_github: str,
        issue_number: int,
        *,
        epics: list[dict] | None = None,
    ) -> ParentRef | None:
        raw = github_ops.get_issue_parent(repo_github, issue_number)
        if not raw:
            return None
        number = raw.get("number")
        if number is None:
            return None
        return ParentRef(number=int(number), state=str(raw.get("state") or "open"))

    def add_child(self, repo_github: str, parent_number: int, child_number: int) -> None:
        github_ops.add_sub_issue(repo_github, parent_number, child_number)

    def remove_child(self, repo_github: str, parent_number: int, child_number: int) -> None:
        github_ops.remove_sub_issue(repo_github, parent_number, child_number)
