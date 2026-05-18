"""Dataclasses for the coordinator: repos, machines, assignments, board."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Repo:
    name: str
    github: str
    depends_on: list[str] = field(default_factory=list)
    default_branch: str = "main"
    build_command: str | None = None
    test_command: str | None = None


@dataclass
class Machine:
    name: str
    host: str
    capabilities: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    repo_paths: dict[str, str] = field(default_factory=dict)

    def can_work_on(self, repo_name: str) -> bool:
        return repo_name in self.repos

    def repo_path(self, repo_name: str) -> str | None:
        return self.repo_paths.get(repo_name)


@dataclass
class Assignment:
    machine_name: str
    repo_name: str
    issue_number: int
    issue_title: str
    files_allowed: list[str] = field(default_factory=list)
    files_forbidden: list[str] = field(default_factory=list)
    briefing: str = ""
    assignment_id: str | None = None
    status: str = "pending"  # pending | running | done | failed
    branch: str | None = None
    pr_url: str | None = None
    dispatched_at: float | None = None
    finished_at: float | None = None
    smoke_test: str | None = None  # None | pass | fail
    smoke_test_reason: str | None = None


@dataclass
class Proposal:
    id: int
    machine_name: str
    repo_name: str
    issue_number: int
    issue_title: str
    rationale: str
    files_likely: list[str] = field(default_factory=list)
    briefing: str = ""


@dataclass
class SplitChunk:
    title: str
    scope: str
    files_likely: list[str] = field(default_factory=list)


@dataclass
class SplitProposal:
    id: int
    repo_name: str
    issue_number: int
    issue_title: str
    rationale: str
    chunks: list[SplitChunk] = field(default_factory=list)


@dataclass
class Board:
    repos: list[Repo] = field(default_factory=list)
    machines: list[Machine] = field(default_factory=list)
    active: list[Assignment] = field(default_factory=list)
    completed: list[Assignment] = field(default_factory=list)
    round_number: int = 0

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)

    def machine(self, name: str) -> Machine | None:
        return next((m for m in self.machines if m.name == name), None)

    def idle_machines(self) -> list[Machine]:
        busy = {a.machine_name for a in self.active if a.status == "running"}
        return [m for m in self.machines if m.name not in busy]

    def active_files_by_repo(self) -> dict[str, list[str]]:
        """Map of repo_name -> files currently being touched by running assignments."""
        result: dict[str, list[str]] = {}
        for a in self.active:
            if a.status != "running":
                continue
            result.setdefault(a.repo_name, []).extend(a.files_allowed)
        return result

    def mark_done(
        self,
        machine_name: str,
        branch: str | None = None,
        pr_url: str | None = None,
    ) -> Assignment | None:
        for a in self.active:
            if a.machine_name == machine_name and a.status == "running":
                a.status = "done"
                a.branch = branch
                a.pr_url = pr_url
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def mark_failed(self, machine_name: str) -> Assignment | None:
        for a in self.active:
            if a.machine_name == machine_name and a.status == "running":
                a.status = "failed"
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def find_by_id(self, assignment_id: str) -> Assignment | None:
        for a in self.active:
            if a.assignment_id == assignment_id:
                return a
        for a in self.completed:
            if a.assignment_id == assignment_id:
                return a
        return None

    def mark_done_by_id(
        self,
        assignment_id: str,
        branch: str | None = None,
        pr_url: str | None = None,
        finished_at: float | None = None,
    ) -> Assignment | None:
        for a in self.active:
            if a.assignment_id == assignment_id:
                a.status = "done"
                if branch is not None:
                    a.branch = branch
                if pr_url is not None:
                    a.pr_url = pr_url
                a.finished_at = finished_at
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def mark_failed_by_id(
        self,
        assignment_id: str,
        finished_at: float | None = None,
    ) -> Assignment | None:
        for a in self.active:
            if a.assignment_id == assignment_id:
                a.status = "failed"
                a.finished_at = finished_at
                self.completed.append(a)
                self.active.remove(a)
                return a
        return None

    def gc(self, keep: int = 50) -> int:
        """Remove oldest completed assignments beyond *keep*. Returns count removed."""
        if len(self.completed) <= keep:
            return 0
        by_time = sorted(self.completed, key=lambda a: a.finished_at or 0)
        to_remove = len(self.completed) - keep
        self.completed = by_time[to_remove:]
        return to_remove
