"""Pre-merge PR-body lint (#1196, hole 2).

GitHub's own closing-keyword magic (``Closes #N`` / ``Fixes #N`` / ``Resolves
#N`` in a merged PR's body) auto-closes the referenced issue on merge — and
it never calls :func:`coord.github_ops.close_issue`, so that function's
open-children guard (#1196) cannot stop it. A worker-authored PR is free to
ask an LLM to write ``Closes #<epic>`` in its body; nothing before this
scanned, linted, or rejected it (the gap #1196 calls out explicitly:
``merge_queue.py`` only controls the *keyword the coordinator itself
writes* via ``_briefing_body``/``CLOSES_ISSUE_TYPES``, not what a worker's
own ``gh pr create`` puts in the body).

This module is pure (no ``gh``/network calls) so the regex/rewrite logic is
unit-testable in isolation; :func:`coord.merge_queue.process` does the
network side (fetch body, check each referenced issue for open children via
``github_ops.has_open_children``, call this, write back via
``github_ops.edit_pr_body`` when anything changed).
"""

from __future__ import annotations

import re

__all__ = ["find_closing_references", "downgrade_closing_keywords"]

# The GitHub closing-keyword vocabulary (case-insensitive): close/closes/
# closed, fix/fixes/fixed, resolve/resolves/resolved. Deliberately matches
# only the simple "<keyword> #<N>" shape actually used in this codebase's
# own PR bodies (`_briefing_body`, `_find_or_open_pr`) — GitHub also accepts
# comma-lists ("Closes #1, #2") and cross-repo refs, but every body this
# coordinator or its workers produce uses one keyword per issue reference.
_CLOSING_RE = re.compile(
    r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(\s+)#(\d+)",
    re.IGNORECASE,
)


def find_closing_references(body: str) -> list[int]:
    """Return every issue number referenced by a GitHub closing keyword in
    *body*, in first-seen order (duplicates removed)."""
    seen: list[int] = []
    for m in _CLOSING_RE.finditer(body or ""):
        n = int(m.group(3))
        if n not in seen:
            seen.append(n)
    return seen


def downgrade_closing_keywords(body: str, numbers: set[int]) -> tuple[str, list[int]]:
    """Rewrite ``<keyword> #N`` -> ``Refs #N`` for every ``N`` in *numbers*.

    Returns ``(possibly-rewritten body, sorted list of numbers actually
    downgraded)``. References to issue numbers not in *numbers* are left
    exactly as written (including the original keyword's casing) — this
    keeps the common case (a regular issue with no children) untouched.
    """
    downgraded: list[int] = []

    def _sub(m: re.Match) -> str:
        n = int(m.group(3))
        if n not in numbers:
            return m.group(0)
        downgraded.append(n)
        return f"Refs{m.group(2)}#{n}"

    new_body = _CLOSING_RE.sub(_sub, body or "")
    return new_body, sorted(set(downgraded))
