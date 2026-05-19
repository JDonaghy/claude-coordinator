"""Tests that the agent captures the worker's branch name after completion."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coord.agent import DONE, FAILED, AgentServer, AssignmentSpec


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_clone(tmp_path: Path) -> Path:
    """A git repo on `main` with one commit. Worker runs in a worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("hi\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_branch_captured_from_worktree(
    tmp_path: Path, repo_clone: Path
) -> None:
    """The worktree is auto-created on issue-<N>-<slug>; branch is captured from it."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=42, issue_title="add feature X", briefing="b",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == DONE
    assert final.branch == "issue-42-add-feature-x"

    # /status exposes it too
    status = server.list_assignments()
    completed = status["completed"]
    assert completed[0]["branch"] == "issue-42-add-feature-x"
    server.shutdown()


def test_worktree_path_stored_on_assignment(
    tmp_path: Path, repo_clone: Path
) -> None:
    """The worktree path is set on the assignment for inspection."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=1, issue_title="t", briefing="b",
    )
    a = server.assign(spec)
    assert a.worktree_path is not None
    assert "worktrees" in a.worktree_path
    final = server.wait_for(a.id, timeout=10)
    assert final.status == DONE
    server.shutdown()


def test_main_repo_untouched_by_worker(
    tmp_path: Path, repo_clone: Path
) -> None:
    """The main repo clone should stay on its default branch after worker completes."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=1, issue_title="t", briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    server.wait_for(a.id, timeout=10)

    # Main repo should still be on main
    main_branch = _git(repo_clone, "rev-parse", "--abbrev-ref", "HEAD")
    assert main_branch == "main"
    server.shutdown()


def test_worktree_setup_failure_marks_assignment_failed(tmp_path: Path) -> None:
    """If worktree setup fails (e.g. not a git repo), the assignment goes to FAILED."""
    not_a_repo = tmp_path / "not-git"
    not_a_repo.mkdir()
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(not_a_repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(not_a_repo),
        issue_number=1, issue_title="t", briefing="b",
    )
    a = server.assign(spec)
    assert a.status == FAILED
    assert "worktree setup failed" in a.error
    server.shutdown()
