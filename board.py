"""Work board state management for multi-agent coordination."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class WorkerConfig:
    name: str
    can_gtk: bool = True
    notes: str = ""


@dataclass
class Assignment:
    worker_name: str
    issue_number: int
    issue_title: str
    files_allowed: list[str] = field(default_factory=list)
    files_forbidden: list[str] = field(default_factory=list)
    briefing: str = ""
    session_id: str | None = None
    status: str = "pending"  # pending | running | done | failed
    branch: str | None = None
    pr_url: str | None = None


@dataclass
class Board:
    repo: str
    workers: list[WorkerConfig] = field(default_factory=list)
    active: list[Assignment] = field(default_factory=list)
    completed: list[Assignment] = field(default_factory=list)
    round_number: int = 0

    def idle_workers(self) -> list[WorkerConfig]:
        busy = {a.worker_name for a in self.active if a.status == "running"}
        return [w for w in self.workers if w.name not in busy]

    def active_files(self) -> dict[str, list[str]]:
        """Map of worker_name -> files they're touching."""
        return {
            a.worker_name: a.files_allowed
            for a in self.active
            if a.status == "running"
        }

    def mark_done(self, worker_name: str, branch: str | None = None, pr_url: str | None = None):
        for a in self.active:
            if a.worker_name == worker_name and a.status == "running":
                a.status = "done"
                a.branch = branch
                a.pr_url = pr_url
                self.completed.append(a)
                self.active.remove(a)
                return

    def mark_failed(self, worker_name: str):
        for a in self.active:
            if a.worker_name == worker_name and a.status == "running":
                a.status = "failed"
                self.completed.append(a)
                self.active.remove(a)
                return

    def summary(self) -> str:
        lines = [f"## Board — Round {self.round_number}\n"]
        lines.append("| Worker | Issue | Status | Files |")
        lines.append("|--------|-------|--------|-------|")
        for a in self.active:
            files = ", ".join(a.files_allowed[:3]) or "—"
            lines.append(f"| {a.worker_name} | #{a.issue_number} {a.issue_title[:40]} | {a.status} | {files} |")
        idle = self.idle_workers()
        for w in idle:
            constraint = " (no GTK)" if not w.can_gtk else ""
            lines.append(f"| {w.name}{constraint} | — | idle | — |")
        if self.completed:
            lines.append(f"\nCompleted this session: {len(self.completed)} issues")
        return "\n".join(lines)


def save_board(board: Board, path: str = "board_state.json"):
    Path(path).write_text(json.dumps(asdict(board), indent=2))


def load_board(path: str = "board_state.json") -> Board | None:
    p = Path(path)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    board = Board(repo=data["repo"])
    board.workers = [WorkerConfig(**w) for w in data.get("workers", [])]
    board.active = [Assignment(**a) for a in data.get("active", [])]
    board.completed = [Assignment(**a) for a in data.get("completed", [])]
    board.round_number = data.get("round_number", 0)
    return board
