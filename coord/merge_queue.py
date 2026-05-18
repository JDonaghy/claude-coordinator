"""Merge queue: sequence completed assignments into their target branches.

Two-layer design so the logic is testable without hitting `gh`:

- Data + sequencing live here (pure functions over QueuedMerge).
- Wire calls (gh pr create / merge / size) are passed in as `gh_ops` so
  tests can substitute a stub. `coord.cli` wires the real `coord.github_ops`.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

from coord.models import Assignment
from coord.state import COORD_DIR

QUEUE_FILE = COORD_DIR / "merge_queue.json"

# States
PENDING = "pending"
MERGING = "merging"
MERGED = "merged"
CONFLICT = "conflict"
SKIPPED = "skipped"


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

def load_queue(path: Path | None = None) -> list[QueuedMerge]:
    p = path or QUEUE_FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [QueuedMerge(**entry) for entry in data]


def save_queue(items: list[QueuedMerge], path: Path | None = None) -> None:
    p = path or QUEUE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([asdict(x) for x in items], indent=2) + "\n")


# ── Enqueue ──────────────────────────────────────────────────────────────

def enqueue(
    assignment: Assignment,
    repo_github: str,
    target_branch: str,
    path: Path | None = None,
) -> QueuedMerge | None:
    """Add a completed assignment to the queue if it isn't already there.

    Returns the new entry, or None if it was already queued or has no branch.
    """
    if not assignment.branch:
        return None
    items = load_queue(path)
    if any(x.assignment_id == assignment.assignment_id for x in items):
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
    save_queue(items, path)
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
    return (
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
) -> list[MergeEvent]:
    """Open PRs, size them, then merge each pending item.

    Items are grouped by (repo_github, target_branch); a conflict in one
    group halts only that group. Within a group, items are merged in input
    order — call `sequence(group)` first if you want size-based ordering.
    Set `presorted=True` to make that explicit at call sites.

    Mutates `items` in place; the caller saves the queue after.
    """
    events: list[MergeEvent] = []

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
