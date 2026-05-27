"""Tests for git worktree isolation in the agent server."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coord.agent import (
    DONE,
    FAILED,
    RUNNING,
    AgentServer,
    AssignmentSpec,
    _slugify,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal git repo on `main` with one commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    (r / "README").write_text("init\n")
    _git(r, "add", "README")
    _git(r, "commit", "-m", "initial")
    return r


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A git repo with a bare remote. Returns (local, remote)."""
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "t@t.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "README").write_text("init\n")
    _git(seed, "add", "README")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    local = tmp_path / "local"
    _git(tmp_path, "clone", str(remote), str(local))
    _git(local, "config", "user.email", "t@t.com")
    _git(local, "config", "user.name", "Test")
    return local, remote


def _server(tmp_path: Path, repo_path: Path, *, argv: list[str] | None = None) -> AgentServer:
    if argv is None:
        argv = ["/bin/true"]
    return AgentServer(
        machine_name="t",
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv,
        repo_paths={"api": str(repo_path)},
    )


def _spec(repo_path: Path, **overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path=str(repo_path),
        issue_number=1,
        issue_title="fix the bug",
        briefing="b",
        branch="main",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


# ── _slugify tests ────────────────────────────────────────────────────────


class TestSlugify:
    def test_basic(self):
        assert _slugify("Fix the Bug") == "fix-the-bug"

    def test_special_chars(self):
        assert _slugify("Add feature: X & Y!") == "add-feature-x-y"

    def test_truncation(self):
        long_title = "a" * 60
        result = _slugify(long_title)
        assert len(result) <= 40

    def test_trailing_dash_stripped(self):
        # After truncation, trailing dashes should be removed
        result = _slugify("a" * 39 + "-b")
        assert not result.endswith("-")


# ── Worktree lifecycle ────────────────────────────────────────────────────


class TestWorktreeCreation:
    def test_worktree_created_before_spawn(self, tmp_path: Path, repo: Path) -> None:
        """Worktree should exist when the worker command runs."""
        canary = tmp_path / "canary.txt"
        server = _server(
            tmp_path, repo,
            # Worker checks it's in a worktree (not the main repo) and writes canary
            argv=["/bin/sh", "-c",
                  f"test -f README && git rev-parse --abbrev-ref HEAD > {canary}"],
        )
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id, timeout=10)
        assert final.status == DONE
        assert canary.exists()
        branch = canary.read_text().strip()
        assert branch.startswith("issue-1-")
        server.shutdown()

    def test_worktree_path_under_state_dir(self, tmp_path: Path, repo: Path) -> None:
        server = _server(tmp_path, repo)
        a = server.assign(_spec(repo))
        assert a.worktree_path is not None
        assert str(tmp_path / "state" / "worktrees") in a.worktree_path
        server.wait_for(a.id, timeout=10)
        server.shutdown()

    def test_worktree_branch_name_includes_issue_number(
        self, tmp_path: Path, repo: Path
    ) -> None:
        server = _server(tmp_path, repo)
        a = server.assign(_spec(repo, issue_number=42, issue_title="Add widget"))
        final = server.wait_for(a.id, timeout=10)
        assert final.status == DONE
        assert final.branch == "issue-42-add-widget"
        server.shutdown()

    def test_worker_runs_in_worktree_not_main_repo(
        self, tmp_path: Path, repo: Path
    ) -> None:
        """Worker cwd should be the worktree, not the main repo."""
        cwd_file = tmp_path / "cwd.txt"
        server = _server(
            tmp_path, repo,
            argv=["/bin/sh", "-c", f"pwd > {cwd_file}"],
        )
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id, timeout=10)
        assert final.status == DONE
        worker_cwd = cwd_file.read_text().strip()
        # Worker should NOT be in the main repo
        assert worker_cwd != str(repo)
        # Worker should be in the worktree path
        assert a.worktree_path is not None
        assert worker_cwd == a.worktree_path
        server.shutdown()


class TestWorktreeCleanup:
    def test_worktree_removed_after_success(self, tmp_path: Path, repo: Path) -> None:
        server = _server(tmp_path, repo)
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id, timeout=10)
        assert final.status == DONE
        # Worktree should be cleaned up
        assert not Path(final.worktree_path).exists()
        server.shutdown()

    def test_worktree_removed_after_failure(self, tmp_path: Path, repo: Path) -> None:
        server = _server(tmp_path, repo, argv=["/bin/sh", "-c", "exit 1"])
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id, timeout=10)
        assert final.status == FAILED
        # Worktree should still be cleaned up even on failure
        assert not Path(final.worktree_path).exists()
        server.shutdown()

    def test_worktree_removed_on_cancel(self, tmp_path: Path, repo: Path) -> None:
        import time
        server = _server(tmp_path, repo, argv=["/bin/sh", "-c", "sleep 30"])
        a = server.assign(_spec(repo))
        # Wait until running
        for _ in range(50):
            if server.get(a.id).status == RUNNING:
                break
            time.sleep(0.02)
        wt_path = a.worktree_path
        server.cancel(a.id)
        assert not Path(wt_path).exists()
        server.shutdown()

    def test_main_repo_stays_on_default_branch(
        self, tmp_path: Path, repo: Path
    ) -> None:
        server = _server(tmp_path, repo)
        a = server.assign(_spec(repo, branch="main"))
        server.wait_for(a.id, timeout=10)
        # Main repo should remain on main
        main_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        assert main_branch == "main"
        server.shutdown()


class TestWorktreeWithRemote:
    def test_push_on_success(
        self, tmp_path: Path, repo_with_remote: tuple[Path, Path]
    ) -> None:
        """On success, _reap pushes the branch to origin."""
        local, remote = repo_with_remote
        server = _server(
            tmp_path, local,
            argv=["/bin/sh", "-c",
                  "echo change >> README && git add README && git commit -m 'work'"],
        )
        a = server.assign(_spec(local, issue_number=5, issue_title="test push"))
        final = server.wait_for(a.id, timeout=10)
        assert final.status == DONE
        # The branch should exist on the remote
        refs = _git(remote, "branch", "--list")
        assert "issue-5-test-push" in refs
        server.shutdown()

    def test_no_push_on_failure(
        self, tmp_path: Path, repo_with_remote: tuple[Path, Path]
    ) -> None:
        """On failure, _reap should NOT push the branch."""
        local, remote = repo_with_remote
        server = _server(
            tmp_path, local,
            argv=["/bin/sh", "-c", "exit 1"],
        )
        a = server.assign(_spec(local, issue_number=6, issue_title="fail no push"))
        final = server.wait_for(a.id, timeout=10)
        assert final.status == FAILED
        # The branch should NOT exist on the remote
        refs = _git(remote, "branch", "--list")
        assert "issue-6-fail-no-push" not in refs
        server.shutdown()

    def test_push_timeout_does_not_block_status_update(
        self, tmp_path: Path, repo_with_remote: tuple[Path, Path]
    ) -> None:
        """If the reap-time push times out, the assignment must still reach DONE.

        This is the regression test for the hang described in issue #204: a
        subprocess.TimeoutExpired raised by _git was not caught by the
        ``except _GitError`` handler, killing the reap thread before the status
        update ran and leaving the assignment permanently stuck in 'running'.
        """
        import unittest.mock as mock
        from coord import agent as agent_mod

        local, _remote = repo_with_remote
        original_git = agent_mod._git

        def _git_push_timeout(cwd: Path, *args: str, **kwargs) -> str:
            if "push" in args:
                raise subprocess.TimeoutExpired(["git", "push"], 60.0)
            return original_git(cwd, *args, **kwargs)

        server = _server(
            tmp_path, local,
            argv=["/bin/sh", "-c",
                  "echo change >> README && git add README && git commit -m 'work'"],
        )
        with mock.patch.object(agent_mod, "_git", side_effect=_git_push_timeout):
            a = server.assign(_spec(local, issue_number=8, issue_title="push timeout"))
            final = server.wait_for(a.id, timeout=10)

        assert final.status == DONE, (
            f"Assignment stuck in '{final.status}' after push timeout — "
            "reap thread did not complete status update"
        )
        server.shutdown()

    def test_retry_reuses_existing_remote_branch(
        self, tmp_path: Path, repo_with_remote: tuple[Path, Path]
    ) -> None:
        """If a branch already exists on remote, worktree checks it out instead of creating."""
        local, remote = repo_with_remote
        # First run: create and push a branch
        server1 = _server(
            tmp_path, local,
            argv=["/bin/sh", "-c",
                  "echo v1 >> README && git add README && git commit -m 'v1'"],
        )
        a1 = server1.assign(_spec(local, issue_number=7, issue_title="retry test"))
        final1 = server1.wait_for(a1.id, timeout=10)
        assert final1.status == DONE
        assert final1.branch == "issue-7-retry-test"
        server1.shutdown()

        # Second run: should pick up the existing branch
        server2 = AgentServer(
            machine_name="t",
            repos=["api"],
            state_dir=tmp_path / "state2",
            worker_command=lambda spec: ["/bin/sh", "-c",
                                          "echo v2 >> README && git add README && git commit -m 'v2'"],
            repo_paths={"api": str(local)},
        )
        a2 = server2.assign(_spec(local, issue_number=7, issue_title="retry test"))
        final2 = server2.wait_for(a2.id, timeout=10)
        assert final2.status == DONE
        assert final2.branch == "issue-7-retry-test"
        server2.shutdown()


class TestWorktreeSetupFailure:
    def test_non_git_directory_fails(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "not-git"
        not_a_repo.mkdir()
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/true"],
            repo_paths={"api": str(not_a_repo)},
        )
        a = server.assign(_spec(not_a_repo))
        assert a.status == FAILED
        assert "worktree setup failed" in a.error
        server.shutdown()

    def test_worker_not_spawned_on_setup_failure(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "not-git"
        not_a_repo.mkdir()
        canary = tmp_path / "canary.txt"
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", f"touch {canary}"],
            repo_paths={"api": str(not_a_repo)},
        )
        a = server.assign(_spec(not_a_repo))
        assert a.status == FAILED
        assert not canary.exists()
        server.shutdown()


class TestWorktreePersistence:
    def test_worktree_path_persisted_in_state(
        self, tmp_path: Path, repo: Path
    ) -> None:
        server = _server(tmp_path, repo)
        a = server.assign(_spec(repo))
        server.wait_for(a.id, timeout=10)

        state = json.loads((tmp_path / "state" / "agent_state.json").read_text())
        entry = next(e for e in state["assignments"] if e["id"] == a.id)
        assert entry["worktree_path"] is not None
        assert a.id in entry["worktree_path"]
        server.shutdown()

    def test_backward_compat_no_worktree_path(self, tmp_path: Path) -> None:
        """Old state files without worktree_path should load fine."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "agent_state.json").write_text(
            json.dumps({
                "machine": "t",
                "capabilities": [],
                "repos": ["api"],
                "assignments": [{
                    "id": "old123",
                    "status": "done",
                    "pid": None,
                    "started_at": 1.0,
                    "finished_at": 2.0,
                    "exit_code": 0,
                    "log_path": None,
                    "error": None,
                    "branch": "issue-1-old",
                    "worktree_path": None,
                    "spec": {
                        "repo_name": "api",
                        "repo_path": str(tmp_path),
                        "issue_number": 1,
                        "issue_title": "old",
                        "briefing": "b",
                        "files_allowed": [],
                        "files_forbidden": [],
                        "branch": "main",
                    },
                }],
            })
        )
        server = AgentServer(
            machine_name="t", repos=["api"], state_dir=state_dir
        )
        recovered = server.get("old123")
        assert recovered is not None
        assert recovered.status == DONE
        assert recovered.worktree_path is None
        server.shutdown()


class TestWorktreeStartupPrune:
    def test_prune_runs_on_init(self, tmp_path: Path, repo: Path) -> None:
        """AgentServer.__init__ should call git worktree prune without error."""
        # Just ensure no exception is raised when repo_paths has entries
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/true"],
            repo_paths={"api": str(repo)},
        )
        # If prune failed silently, we still succeed
        assert server is not None
        server.shutdown()

    def test_prune_tolerates_missing_repo_path(self, tmp_path: Path) -> None:
        """_prune_worktrees must not crash when a configured repo_path doesn't exist.

        subprocess.run raises FileNotFoundError (not _GitError) when its cwd
        argument points to a non-existent directory.  A stale editable install
        or a deleted worktree used as a source directory can trigger this on
        agent startup (e.g. after exec_restart following /update).  Regression
        test for issue #280.
        """
        nonexistent = str(tmp_path / "repo_that_was_deleted")
        # Path deliberately does NOT exist — agent init must survive.
        server = AgentServer(
            machine_name="t",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/true"],
            repo_paths={"api": nonexistent},
        )
        assert server is not None
        server.shutdown()

    def test_prune_continues_after_one_missing_path(self, tmp_path: Path, repo: Path) -> None:
        """When one repo_path is missing, _prune_worktrees should still prune the rest."""
        nonexistent = str(tmp_path / "gone")
        # Two repos: one valid, one missing.  Both should be attempted; neither
        # should abort the loop.
        server = AgentServer(
            machine_name="t",
            repos=["api", "sdk"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/true"],
            repo_paths={"api": str(repo), "sdk": nonexistent},
        )
        assert server is not None
        server.shutdown()


class TestParallelWorktrees:
    def test_two_assignments_same_repo_different_issues(
        self, tmp_path: Path, repo: Path
    ) -> None:
        """Two assignments on the same repo with different issues should work in parallel."""
        import time

        server = _server(tmp_path, repo, argv=["/bin/sh", "-c", "sleep 0.5"])
        a1 = server.assign(_spec(repo, issue_number=10, issue_title="first"))
        a2 = server.assign(_spec(repo, issue_number=11, issue_title="second"))

        # Both should get different worktree paths
        assert a1.worktree_path != a2.worktree_path

        # Both should eventually complete
        final1 = server.wait_for(a1.id, timeout=10)
        final2 = server.wait_for(a2.id, timeout=10)
        assert final1.status == DONE
        assert final2.status == DONE
        assert final1.branch == "issue-10-first"
        assert final2.branch == "issue-11-second"
        server.shutdown()
