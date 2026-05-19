"""Tests for AgentServer.list_repos() and the pull_repos auto-pull flow."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coord.agent import (
    DONE,
    FAILED,
    AgentServer,
    AssignmentSpec,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def two_repos(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create an upstream `remote` repo and a `local` clone of it.

    Returns (remote_path, local_path, work_path) where work_path is a third
    directory the worker can run inside.
    """
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")

    # Make an initial commit in a working clone we'll push from
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@t.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("hi\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    local = tmp_path / "local"
    _git(tmp_path, "clone", str(remote), str(local))
    _git(local, "config", "user.email", "t@t.com")
    _git(local, "config", "user.name", "Test")

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("work repo\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    return remote, local, work


def _push_new_commit(remote: Path, tmp_path: Path) -> str:
    """Push a new commit to `remote` from a fresh scratch clone. Returns the new SHA."""
    scratch = tmp_path / "scratch"
    if scratch.exists():
        import shutil
        shutil.rmtree(scratch)
    _git(tmp_path, "clone", str(remote), str(scratch))
    _git(scratch, "config", "user.email", "t@t.com")
    _git(scratch, "config", "user.name", "Test")
    (scratch / "new-file").write_text("update\n")
    _git(scratch, "add", "new-file")
    _git(scratch, "commit", "-m", "update")
    _git(scratch, "push", "origin", "main")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(scratch), capture_output=True, text=True, check=True,
    ).stdout.strip()
    return sha


class TestListRepos:
    def test_reports_local_head_and_branch(
        self, tmp_path: Path, two_repos: tuple[Path, Path, Path]
    ) -> None:
        _remote, local, _work = two_repos
        server = AgentServer(
            machine_name="t", repos=["api"],
            repo_paths={"api": str(local)},
            state_dir=tmp_path / "agent-state",
        )
        info = server.list_repos()
        assert "api" in info
        assert "error" not in info["api"]
        assert len(info["api"]["sha"]) == 40
        assert info["api"]["branch"] == "main"
        assert info["api"]["dirty"] is False

    def test_dirty_flag_set_when_uncommitted_changes(
        self, tmp_path: Path, two_repos: tuple[Path, Path, Path]
    ) -> None:
        _remote, local, _work = two_repos
        (local / "dirty.txt").write_text("uncommitted")
        server = AgentServer(
            machine_name="t", repos=["api"],
            repo_paths={"api": str(local)},
            state_dir=tmp_path / "state",
        )
        info = server.list_repos()
        assert info["api"]["dirty"] is True

    def test_missing_repo_path_returns_error(self, tmp_path: Path) -> None:
        server = AgentServer(
            machine_name="t", repos=["api"],
            repo_paths={},  # no path for api
            state_dir=tmp_path / "state",
        )
        info = server.list_repos()
        assert "error" in info["api"]

    def test_non_git_directory_returns_error(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "not-git"
        not_a_repo.mkdir()
        server = AgentServer(
            machine_name="t", repos=["api"],
            repo_paths={"api": str(not_a_repo)},
            state_dir=tmp_path / "state",
        )
        info = server.list_repos()
        assert "error" in info["api"]


class TestPullRepos:
    def test_pull_runs_before_worker_and_advances_head(
        self, tmp_path: Path, two_repos: tuple[Path, Path, Path]
    ) -> None:
        remote, local, work = two_repos

        original = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(local), capture_output=True, text=True, check=True,
        ).stdout.strip()
        new_sha = _push_new_commit(remote, tmp_path)
        assert new_sha != original

        server = AgentServer(
            machine_name="t", repos=["api", "dep"],
            repo_paths={"api": str(work), "dep": str(local)},
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "echo worker ran"],
        )
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(work),
            issue_number=1,
            issue_title="t",
            briefing="b",
            pull_repos=["dep"],
        )
        a = server.assign(spec)
        final = server.wait_for(a.id, timeout=15)
        assert final.status == DONE

        updated = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(local), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert updated == new_sha

        log = Path(final.log_path).read_text()
        assert "pulling dependencies" in log
        assert "all pulls succeeded" in log
        assert "worker ran" in log
        server.shutdown()

    def test_failing_pull_fails_assignment_and_skips_worker(
        self, tmp_path: Path, two_repos: tuple[Path, Path, Path]
    ) -> None:
        _remote, local, work = two_repos

        # Make the local clone divergent from remote so --ff-only fails
        _git(local, "config", "user.email", "t@t.com")
        _git(local, "config", "user.name", "Test")
        (local / "diverge").write_text("local-only\n")
        _git(local, "add", "diverge")
        _git(local, "commit", "-m", "local divergence")

        # Push a different commit upstream
        _push_new_commit(_remote, tmp_path)

        canary = tmp_path / "canary.txt"
        server = AgentServer(
            machine_name="t", repos=["api", "dep"],
            repo_paths={"api": str(work), "dep": str(local)},
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", f"touch {canary}"],
        )
        spec = AssignmentSpec(
            repo_name="api", repo_path=str(work), issue_number=1, issue_title="t",
            briefing="b", pull_repos=["dep"],
        )
        a = server.assign(spec)
        for _ in range(100):
            if server.get(a.id).status == FAILED:
                break
            time.sleep(0.05)
        assert server.get(a.id).status == FAILED
        assert not canary.exists(), "worker should not have started after pull failure"
        log = Path(server.get(a.id).log_path).read_text()
        assert "pull failed" in log
        server.shutdown()

    def test_unknown_pull_repo_rejected_at_assign(self, tmp_path: Path) -> None:
        server = AgentServer(
            machine_name="t", repos=["api"],
            repo_paths={"api": str(tmp_path)},
            state_dir=tmp_path / "state",
        )
        spec = AssignmentSpec(
            repo_name="api", repo_path=str(tmp_path), issue_number=1, issue_title="t",
            briefing="b", pull_repos=["ghost"],
        )
        with pytest.raises(ValueError, match="pull_repos"):
            server.assign(spec)
