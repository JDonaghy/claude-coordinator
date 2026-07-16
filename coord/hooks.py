"""Session lifecycle hooks — triggered at round completion and session end."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from coord import github_ops
from coord.config import Config
from coord.models import CLOSES_ISSUE_TYPES, Board


@dataclass
class HookResult:
    hook: str
    ok: bool
    message: str


def run_hooks(event: str, config: Config, board: Board) -> list[HookResult]:
    """Execute all hooks registered for *event*. Returns results."""
    hook_names = getattr(config.hooks, event, [])
    results: list[HookResult] = []
    for name in hook_names:
        fn = HOOK_REGISTRY.get(name)
        if fn is None:
            results.append(HookResult(hook=name, ok=False, message=f"unknown hook: {name}"))
            continue
        try:
            msg = fn(config, board)
            results.append(HookResult(hook=name, ok=True, message=msg))
        except Exception as e:
            results.append(HookResult(hook=name, ok=False, message=str(e)))
    return results


def is_round_complete(board: Board) -> bool:
    """True when no active assignments remain (all finished or failed)."""
    return len(board.active) == 0 and len(board.completed) > 0


# ── Built-in hooks ──────────────────────────────────────────────────────────


def _close_merged_issues(config: Config, board: Board) -> str:
    """Close issues whose assignments completed successfully.

    #1196 (hole 3): gated on :data:`coord.models.CLOSES_ISSUE_TYPES`, the
    same gate ``merge_queue.py`` and the PR-body keyword already honour. An
    opt-in hook (``KNOWN_HOOKS``, defaults empty — not enabled in this
    repo's own config) must not contradict the #1077 invariant: a
    "mock-author"/"test-author"/"audit" assignment's ``issue_number`` is the
    milestone's *tracking issue* (often an epic), not something it resolved
    — closing it here regardless of type was a loaded gun. Also routes
    through :func:`coord.github_ops.close_issue` — the #1196 open-children
    chokepoint — instead of a raw ``gh issue close`` call, so an epic with
    open children is refused here too, not just on the merge path.
    """
    closed = []
    for a in board.completed:
        if a.status != "done":
            continue
        if a.type not in CLOSES_ISSUE_TYPES:
            continue
        repo = config.repo(a.repo_name)
        if repo is None:
            continue
        try:
            github_ops.close_issue(
                repo.github,
                a.issue_number,
                comment=f"Closed by coordinator: assignment {a.assignment_id} completed.",
            )
            closed.append(f"{repo.github}#{a.issue_number}")
        except RuntimeError:
            pass
    if not closed:
        return "no issues to close"
    return f"closed {len(closed)} issue(s): {', '.join(closed)}"


def _summary_report(config: Config, board: Board) -> str:
    """Generate a session/round summary."""
    done = [a for a in board.completed if a.status == "done"]
    failed = [a for a in board.completed if a.status == "failed"]
    lines = [
        f"Round {board.round_number} summary:",
        f"  completed: {len(done)} assignment(s)",
        f"  failed:    {len(failed)} assignment(s)",
        f"  active:    {len(board.active)} assignment(s) still running",
    ]
    if done:
        lines.append("  done:")
        for a in done:
            lines.append(f"    - {a.repo_name} #{a.issue_number}: {a.issue_title}")
    if failed:
        lines.append("  failed:")
        for a in failed:
            lines.append(f"    - {a.repo_name} #{a.issue_number}: {a.issue_title}")
    return "\n".join(lines)


HOOK_REGISTRY: dict[str, Callable[[Config, Board], str]] = {
    "close_merged_issues": _close_merged_issues,
    "summary_report": _summary_report,
}
