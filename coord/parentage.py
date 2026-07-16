"""Parentage seam (#1195) — backend-agnostic parent/child API over the
issue-store seam.

Every gap in the "Epic as a first-class parent" milestone is downstream of
parentage being **plain text** today: the epic->child relation is the ``##
Work order`` / ``## Sub-issues`` markdown checklist
:mod:`coord.milestone_order` parses (:func:`coord.milestone_order.
parse_sub_issues`). GitHub's REST sub-issues API is live on this repo but
used nowhere — there is no structural parent link for anything to reconcile
against.

This module is the narrow contract the future :issue:`183` pluggable
``IssueStore`` refactor will slot a real backend-selection policy into. Until
then it ships two concrete adapters behind the identical
:class:`ParentageStore` shape:

* :class:`coord.parentage_github.GitHubParentage` — the live GitHub
  sub-issues REST API (``GET``/``POST``/``DELETE .../sub_issues``).
* :class:`MarkdownParentage` — the ``## Sub-issues`` checklist fallback for
  GitLab / SQLite / no-API backends (and, today, the cheap path the
  board-payload publish step uses — see ``coord/serve_app.py``'s ``board()``
  handler — since a live API call per pipeline row per poll isn't
  affordable).

Callers reach either backend through the same method names/signatures so
neither the board-payload builder nor a future ``coord`` command has to
branch on which one is live — that's precisely what keeps #183 (pluggable
issue store) viable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from coord.milestone_order import parse_sub_issues


__all__ = [
    "Child",
    "ParentRef",
    "ParentageStore",
    "MarkdownParentage",
    "build_parentage_store",
]


@dataclass(frozen=True)
class Child:
    """One child of an issue: its number and open/closed state."""

    number: int
    state: str  # "open" | "closed"


@dataclass(frozen=True)
class ParentRef:
    """The parent of an issue: its number and open/closed state."""

    number: int
    state: str  # "open" | "closed"


@runtime_checkable
class ParentageStore(Protocol):
    """Read/write parent-child relations for one issue-tracker backend.

    ``body``/``epics`` are backend-specific extra context a caller may
    already have on hand (e.g. from the same cached ``issues`` row the
    board-payload builder already fetched) — a backend that doesn't need
    them (GitHub, which asks the live API) simply ignores them. Every
    adapter accepts the same keyword names so a caller can pass everything
    it might need without knowing which backend answers.
    """

    def children(
        self, repo_github: str, issue_number: int, *, body: str = "",
    ) -> list[Child]: ...

    def parent(
        self,
        repo_github: str,
        issue_number: int,
        *,
        epics: list[dict] | None = None,
    ) -> ParentRef | None: ...

    def add_child(self, repo_github: str, parent_number: int, child_number: int) -> None: ...

    def remove_child(self, repo_github: str, parent_number: int, child_number: int) -> None: ...


class MarkdownParentage:
    """Fallback adapter (#1195): the ``## Sub-issues`` checklist convention
    #1008 already established for backends with no native sub-issues API
    (GitLab / SQLite / no-API).

    ``children()`` parses *body* — the tracking issue's OWN body — with
    :func:`coord.milestone_order.parse_sub_issues`. No network call, so this
    is also what the board-payload publish step uses even on a GitHub-backed
    repo: a live sub-issues API call per epic per poll isn't affordable, and
    (until EP-2 backfills existing epics) the markdown checklist is the only
    populated source of truth anyway.

    ``parent()`` has no reverse pointer to walk — the checklist lives on the
    PARENT's body, not the child's — so callers must supply *epics*: every
    candidate tracking issue as ``{"number": int, "state": str, "body":
    str}``. The first whose ``## Sub-issues`` checklist names *issue_number*
    is the parent; ``None`` when no candidate does (or none were supplied).

    The checklist's ``- [x]`` / ``- [ ]`` box is the only completion signal
    this convention carries, so it doubles as the child's reported ``state``
    here (``"closed"`` for checked, ``"open"`` otherwise) — an approximation
    of the real GitHub issue state, good enough for nesting/display, not a
    live lookup.
    """

    def children(
        self, repo_github: str, issue_number: int, *, body: str = "",
    ) -> list[Child]:
        work_order = parse_sub_issues(body)
        return [
            Child(number=n.issue_number, state="closed" if n.checked else "open")
            for n in work_order.nodes
        ]

    def parent(
        self,
        repo_github: str,
        issue_number: int,
        *,
        epics: list[dict] | None = None,
    ) -> ParentRef | None:
        for epic in epics or []:
            try:
                work_order = parse_sub_issues(epic.get("body") or "")
            except Exception:  # noqa: BLE001 — a malformed epic body: skip it
                continue
            if work_order.node(issue_number) is not None:
                number = epic.get("number")
                if number is None:
                    continue
                return ParentRef(number=int(number), state=str(epic.get("state") or "open"))
        return None

    def add_child(self, repo_github: str, parent_number: int, child_number: int) -> None:
        raise NotImplementedError(
            "MarkdownParentage is read-only from this seam — the `## "
            "Sub-issues` checklist is mutated via `coord milestone "
            "add-child` (#1008), the existing write path for this "
            "convention (splices the section idempotently, unlike a plain "
            "body overwrite)."
        )

    def remove_child(self, repo_github: str, parent_number: int, child_number: int) -> None:
        raise NotImplementedError(
            "MarkdownParentage is read-only from this seam — remove a "
            "sub-issue via `coord milestone add-child ... --remove` (#1008)."
        )


def build_parentage_store(backend: str) -> ParentageStore:
    """Construct the :class:`ParentageStore` named by *backend*.

    Centralised here (mirrors :func:`coord.ci_store.build_ci_store`) so
    callers don't branch on the config value themselves. Unknown values fall
    back to :class:`MarkdownParentage` — the always-available, no-network
    adapter — so a typo in config can't crash a caller the way a KeyError
    would.
    """
    if backend == "github":
        from coord.parentage_github import GitHubParentage  # noqa: PLC0415

        return GitHubParentage()
    return MarkdownParentage()
