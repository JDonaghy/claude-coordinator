"""Tests that the agent captures the worker's branch name after completion."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coord.agent import DONE, AgentServer, AssignmentSpec


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_clone(tmp_path: Path) -> Path:
    """A git repo on `main` with one commit. Worker can switch branches inside."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("hi\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_branch_captured_when_worker_creates_feature_branch(
    tmp_path: Path, repo_clone: Path
) -> None:
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c", f"cd '{repo_clone}' && git checkout -b worker/feat-x",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo_clone),
        issue_number=42, issue_title="t", briefing="b",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == DONE
    assert final.branch == "worker/feat-x"

    # /status exposes it too
    status = server.list_assignments()
    completed = status["completed"]
    assert completed[0]["branch"] == "worker/feat-x"
    server.shutdown()


def test_branch_left_on_default_is_not_captured(
    tmp_path: Path, repo_clone: Path
) -> None:
    """If the worker forgot to switch branches, coordinator gets None and can flag."""
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
        branch="main",  # tells the agent the default is 'main'
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == DONE
    assert final.branch is None
    server.shutdown()


def test_branch_not_captured_when_repo_path_missing(tmp_path: Path) -> None:
    """If the repo path disappears between spawn and reap, no crash, branch stays None."""
    repo = tmp_path / "repo"
    repo.mkdir()
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo),
        issue_number=1, issue_title="t", briefing="b",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Not a git repo → git rev-parse fails → branch stays None
    assert final.branch is None
    server.shutdown()
