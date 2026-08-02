"""#1720: dispatch-time file-overlap fence, derived from live branch
footprints instead of the prompt-only heuristic in ``coord/brain.py``.

``coord/brain.py``'s ``SYSTEM_PROMPT`` tells the planning LLM "if two issues
would touch overlapping files in the same repo, do NOT assign them
simultaneously" — but that only applies to ``coord plan``, and it works by
GUESSING an issue's future footprint from its body text before any code
exists. ``coord assign``/``coord drive`` (and the auto-loop's fix dispatch)
have nothing: the only thing standing between two concurrent workers and a
collision has been an operator hand-writing a `coord context add --pin` note.

This module makes the rule factual and puts it on the paths that actually
dispatch. At dispatch time each *currently running* work-like assignment in
the target repo already has a branch — its real footprint is one GitHub
compare call away (``git diff --name-only`` semantics via the compare API,
no local checkout required — see :func:`coord.github_ops.get_compare_files`).
That is measured, not inferred, and — critically — recomputed on every call,
never pinned: a hand-written pin note keeps warning about files long after
the other work merged and freed them; this has no such staleness because it
is thrown away and rebuilt from live board state on every dispatch.

Advisory only. This module never raises to its caller and never blocks a
dispatch — see :func:`compute_overlap_fence`'s docstring.
"""

from __future__ import annotations

from typing import Callable

from coord.models import WORK_LIKE_TYPES, Assignment, Board

# Type of the optional fetcher callers may inject (tests; also the seam a
# future local-checkout fallback could use). Returns the changed file paths
# for `base...head`, or None if the diff couldn't be computed.
DiffFilesFetcher = Callable[[str, str, str], "list[str] | None"]


def _default_fetcher(repo_github: str, base: str, head: str) -> list[str] | None:
    from coord import github_ops  # noqa: PLC0415

    return github_ops.get_compare_files(repo_github, base, head)


def _running_work_like(
    board: Board, repo_name: str, *, exclude_issue_number: int | None,
) -> list[Assignment]:
    """Running WORK_LIKE_TYPES assignments in *repo_name* with a pushed
    branch, excluding *exclude_issue_number* (the issue about to be
    dispatched — relevant on a redispatch of an issue that already has a
    running row, so it never fences against itself)."""
    return [
        a
        for a in board.active
        if a.status == "running"
        and a.repo_name == repo_name
        and a.type in WORK_LIKE_TYPES
        and a.branch
        and a.issue_number != exclude_issue_number
    ]


def compute_overlap_fence(
    repo_name: str,
    repo_github: str,
    base_branch: str,
    *,
    exclude_issue_number: int | None = None,
    board: Board | None = None,
    diff_files_fetcher: DiffFilesFetcher | None = None,
) -> str:
    """Return a markdown fence block naming files other *running* work-like
    assignments in *repo_name* are touching, or ``""`` when there is nothing
    to report (no running assignments, none with a pushed branch, or every
    branch diff failed) — the caller prepends this with no special-casing,
    same as the #603 issue-context digest.

    Advisory, not a gate: this NEVER refuses or blocks — it only informs the
    new worker so it can stop and report a genuine collision itself, mirroring
    the wording an operator already hand-writes via
    ``coord context add --pin`` (see docs/OPERATING_GOTCHAS.md).

    Fails open at every layer, by construction:
    - the whole board read is wrapped in `except Exception` — an unreachable
      daemon/board must not block a dispatch;
    - each per-assignment diff fetch is wrapped individually — one
      unreadable/deleted/force-pushed branch is skipped, not fatal to the
      others (fulfils "a branch whose diff cannot be computed is skipped,
      dispatch proceeds");
    - a branch with no pushed commits yet resolves to an empty (or ``None``)
      file list either way and contributes nothing, never an error.

    Never persisted — recomputed from live board state on every call, so
    redispatching the same issue twice can't stack fences (there is nothing
    to stack; the caller just gets a fresh string each time).
    """
    try:
        if board is None:
            from coord.board_service import read_board  # noqa: PLC0415

            board = read_board()
        running = _running_work_like(
            board, repo_name, exclude_issue_number=exclude_issue_number
        )
    except Exception:  # noqa: BLE001 — advisory only; never block a dispatch
        return ""
    if not running:
        return ""

    fetcher = diff_files_fetcher or _default_fetcher
    lines: list[str] = []
    for a in running:
        try:
            files = fetcher(repo_github, base_branch, a.branch)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 — one bad branch must not sink the rest
            files = None
        if not files:
            continue
        file_list = ", ".join(f"`{f}`" for f in sorted(files))
        lines.append(f"- Issue #{a.issue_number} (branch `{a.branch}`): {file_list}")

    if not lines:
        return ""

    return (
        "## ⚠️ Live file-overlap fence (#1720)\n"
        "Other work is RUNNING RIGHT NOW in this repo and touching the files "
        "listed below. This is informational, not a rule against parallel "
        "work — but if your own changes would touch the SAME file(s), STOP "
        "and report the conflict instead of editing around it or racing to "
        "commit first:\n"
        + "\n".join(lines)
        + "\n"
    )
