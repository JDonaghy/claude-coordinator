"""Tests for the interactive worktree + branch creation path (#480).

Covers:
1. Branch name construction — ``issue-{N}-{slug}`` from issue_number + title,
   using the same ``_slugify`` primitive as the agent-dispatched path.
2. Worktree path location — ``<state_dir>/worktrees/<assignment_id>/``.
3. Worktree + branch creation using a real git repo (with and without a remote).
4. Worktree removal by :func:`coord.interactive.finalize_interactive_exit`
   when *repo_path* is supplied.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.agent import _GitError, _slugify, setup_interactive_worktree


# ── Helpers ──────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A minimal local-only git repo (no remote) on ``main``."""
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
    """A git repo with a bare remote. Returns ``(local_clone, bare_remote)``."""
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


# ── Branch name construction ────────────────────────────────────────────────


class TestBranchNameConstruction:
    """The feature branch name must follow ``issue-{N}-{slug}``."""

    def test_simple_title(self) -> None:
        slug = _slugify("Add widget")
        assert slug == "add-widget"
        assert f"issue-42-{slug}" == "issue-42-add-widget"

    def test_special_characters_stripped(self) -> None:
        slug = _slugify("Fix bug: foo & bar!")
        assert slug == "fix-bug-foo-bar"

    def test_slug_max_length_40(self) -> None:
        long_title = "a" * 60
        slug = _slugify(long_title)
        assert len(slug) <= 40

    def test_setup_returns_expected_branch_name(self, bare_repo: Path, tmp_path: Path) -> None:
        """``setup_interactive_worktree`` returns the ``issue-{N}-{slug}`` branch name."""
        state_dir = tmp_path / "state"
        _, branch = setup_interactive_worktree(
            bare_repo,
            issue_number=7,
            issue_title="Fix the thing",
            assignment_id="abc123",
            default_branch="main",
            state_dir=state_dir,
        )
        assert branch == "issue-7-fix-the-thing"

    def test_setup_returns_expected_branch_name_with_remote(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        local, _ = repo_with_remote
        state_dir = tmp_path / "state"
        _, branch = setup_interactive_worktree(
            local,
            issue_number=42,
            issue_title="Add widget",
            assignment_id="def456",
            default_branch="main",
            state_dir=state_dir,
        )
        assert branch == "issue-42-add-widget"


# ── Worktree path location ───────────────────────────────────────────────────


class TestWorktreePath:
    """The worktree must be created under ``<state_dir>/worktrees/<assignment_id>``."""

    def test_path_is_under_state_dir_worktrees(
        self, bare_repo: Path, tmp_path: Path
    ) -> None:
        state_dir = tmp_path / "mystate"
        wt_path, _ = setup_interactive_worktree(
            bare_repo,
            issue_number=1,
            issue_title="test path",
            assignment_id="xid001",
            default_branch="main",
            state_dir=state_dir,
        )
        assert wt_path == state_dir / "worktrees" / "xid001"

    def test_worktree_directory_exists_after_setup(
        self, bare_repo: Path, tmp_path: Path
    ) -> None:
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            bare_repo,
            issue_number=3,
            issue_title="check existence",
            assignment_id="yid002",
            default_branch="main",
            state_dir=state_dir,
        )
        assert wt_path.exists()
        assert wt_path.is_dir()

    def test_worktree_path_distinct_per_assignment_id(
        self, bare_repo: Path, tmp_path: Path
    ) -> None:
        state_dir = tmp_path / "state"
        wt1, _ = setup_interactive_worktree(
            bare_repo,
            issue_number=10,
            issue_title="first",
            assignment_id="aid001",
            default_branch="main",
            state_dir=state_dir,
        )
        # Clean up the first worktree before creating a second one on the
        # SAME issue (same branch_name) to avoid a "already used" collision.
        _git(bare_repo, "worktree", "remove", str(wt1), "--force")
        wt2, _ = setup_interactive_worktree(
            bare_repo,
            issue_number=10,
            issue_title="first",
            assignment_id="aid002",
            default_branch="main",
            state_dir=state_dir,
        )
        assert wt1 != wt2
        assert wt1.name == "aid001"
        assert wt2.name == "aid002"


# ── Worktree creation (local-only repo) ─────────────────────────────────────


class TestWorktreeCreationLocalOnly:
    """No remote: worktree should still be created on a fresh local branch."""

    def test_branch_checked_out_in_worktree(
        self, bare_repo: Path, tmp_path: Path
    ) -> None:
        state_dir = tmp_path / "state"
        wt_path, branch = setup_interactive_worktree(
            bare_repo,
            issue_number=5,
            issue_title="local branch",
            assignment_id="local01",
            default_branch="main",
            state_dir=state_dir,
        )
        actual_branch = _git(wt_path, "rev-parse", "--abbrev-ref", "HEAD")
        assert actual_branch == branch

    def test_main_repo_stays_on_main(
        self, bare_repo: Path, tmp_path: Path
    ) -> None:
        state_dir = tmp_path / "state"
        setup_interactive_worktree(
            bare_repo,
            issue_number=6,
            issue_title="main stays",
            assignment_id="local02",
            default_branch="main",
            state_dir=state_dir,
        )
        main_branch = _git(bare_repo, "rev-parse", "--abbrev-ref", "HEAD")
        assert main_branch == "main"

    def test_worktree_shares_history_with_main(
        self, bare_repo: Path, tmp_path: Path
    ) -> None:
        """The worktree should have the same initial commit as the main repo."""
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            bare_repo,
            issue_number=7,
            issue_title="history check",
            assignment_id="local03",
            default_branch="main",
            state_dir=state_dir,
        )
        main_sha = _git(bare_repo, "rev-parse", "main")
        wt_sha = _git(wt_path, "rev-parse", "HEAD")
        assert main_sha == wt_sha


# ── Worktree creation (repo with remote) ────────────────────────────────────


class TestWorktreeCreationWithRemote:
    """With a remote: worktree should branch from ``origin/<default_branch>``."""

    def test_branch_checked_out_in_worktree(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        local, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, branch = setup_interactive_worktree(
            local,
            issue_number=11,
            issue_title="remote branch",
            assignment_id="rem01",
            default_branch="main",
            state_dir=state_dir,
        )
        actual_branch = _git(wt_path, "rev-parse", "--abbrev-ref", "HEAD")
        assert actual_branch == branch

    def test_main_checkout_unchanged(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        local, _ = repo_with_remote
        state_dir = tmp_path / "state"
        setup_interactive_worktree(
            local,
            issue_number=12,
            issue_title="main unchanged",
            assignment_id="rem02",
            default_branch="main",
            state_dir=state_dir,
        )
        checkout_branch = _git(local, "rev-parse", "--abbrev-ref", "HEAD")
        assert checkout_branch == "main"

    def test_existing_remote_branch_is_reused(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """If the branch already exists on origin, it should be checked out."""
        local, remote = repo_with_remote
        state_dir1 = tmp_path / "state1"
        state_dir2 = tmp_path / "state2"

        # First call creates the branch.
        wt1, branch = setup_interactive_worktree(
            local,
            issue_number=13,
            issue_title="retry branch",
            assignment_id="rem03a",
            default_branch="main",
            state_dir=state_dir1,
        )
        # Push the branch so origin knows about it.
        _git(wt1, "push", "-u", "origin", "HEAD")
        # Remove the first worktree so we can create a new one on the same branch.
        _git(local, "worktree", "remove", str(wt1), "--force")

        # Second call — origin has the branch now.
        wt2, branch2 = setup_interactive_worktree(
            local,
            issue_number=13,
            issue_title="retry branch",
            assignment_id="rem03b",
            default_branch="main",
            state_dir=state_dir2,
        )
        assert branch == branch2
        actual_branch = _git(wt2, "rev-parse", "--abbrev-ref", "HEAD")
        assert actual_branch == branch


class TestExistingBranchOverride:
    """Leg 3 (#517): an explicit ``existing_branch`` (used by --fix-of) must
    override the derived ``issue-{N}-{slug}`` name so the fix continues the
    reviewed work's branch and updates the same PR."""

    def test_existing_branch_overrides_derived_name(
        self, bare_repo: Path, tmp_path: Path
    ) -> None:
        state_dir = tmp_path / "state"
        wt_path, branch = setup_interactive_worktree(
            bare_repo,
            issue_number=7,
            issue_title="A totally different title",
            assignment_id="fx001",
            default_branch="main",
            state_dir=state_dir,
            existing_branch="issue-7-original-work",
        )
        # The branch is the one we passed, NOT issue-7-a-totally-different-title.
        assert branch == "issue-7-original-work"
        actual = _git(wt_path, "rev-parse", "--abbrev-ref", "HEAD")
        assert actual == "issue-7-original-work"

    def test_existing_branch_continues_origin_branch(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """When the existing branch is on origin, the worktree checks it out at
        the remote tip — regardless of the (unrelated) issue title/number."""
        local, _ = repo_with_remote
        # Seed a branch on origin via a first interactive session.
        wt1, orig_branch = setup_interactive_worktree(
            local,
            issue_number=42,
            issue_title="Add widget",
            assignment_id="seed01",
            default_branch="main",
            state_dir=tmp_path / "s1",
        )
        _git(wt1, "push", "-u", "origin", "HEAD")
        _git(local, "worktree", "remove", str(wt1), "--force")

        # A fix with a DIFFERENT title/number but existing_branch=orig_branch
        # must continue orig_branch, not derive issue-99-fix-something.
        wt2, branch2 = setup_interactive_worktree(
            local,
            issue_number=99,
            issue_title="fix something else",
            assignment_id="fx002",
            default_branch="main",
            state_dir=tmp_path / "s2",
            existing_branch=orig_branch,
        )
        assert branch2 == orig_branch
        actual = _git(wt2, "rev-parse", "--abbrev-ref", "HEAD")
        assert actual == orig_branch


# ── Worktree removal via finalize_interactive_exit ──────────────────────────


class TestFinalizeRemovesWorktree:
    """With ``repo_path`` supplied, finalize should remove the worktree."""

    def test_worktree_removed_on_finalize(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        local, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            local,
            issue_number=20,
            issue_title="cleanup test",
            assignment_id="fin01",
            default_branch="main",
            state_dir=state_dir,
        )
        assert wt_path.exists()

        _seed_running_assignment("fin01")
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="fin01",
                repo_name="api",
                repo_github="acme/api",
                issue_number=20,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(local),
            )

        assert result.worktree_removed is True
        assert not wt_path.exists(), "worktree directory should have been removed"

    def test_review_finalize_with_no_worktree(self, tmp_path: Path) -> None:
        """A1: an interactive REVIEW finalizes with worktree_path=None — there
        is no session worktree (the review runs read-only in the live
        checkout).  The backstop must not crash, must not push or remove
        anything, and records a terminal state with commits_ahead=None."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        _seed_running_assignment("rev01")
        with patch("coord.github_ops.post_issue_comment"), \
             patch("coord.interactive._git_push") as mock_push:
            result = finalize_interactive_exit(
                assignment_id="rev01",
                repo_name="api",
                repo_github="acme/api",
                issue_number=30,
                machine_name="laptop",
                worktree_path=None,   # review: no session worktree
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=None,
            )

        assert result.worktree_removed is False
        assert result.commits_ahead is None
        assert result.push_ok is True, "push must be skipped (defaults to ok) when there is no worktree"
        assert result.already_recorded is False
        mock_push.assert_not_called()

    def test_worktree_not_removed_when_repo_path_omitted(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Without repo_path, the caller owns cleanup — worktree stays."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        local, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            local,
            issue_number=21,
            issue_title="no cleanup",
            assignment_id="fin02",
            default_branch="main",
            state_dir=state_dir,
        )
        assert wt_path.exists()

        _seed_running_assignment("fin02")
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="fin02",
                repo_name="api",
                repo_github="acme/api",
                issue_number=21,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                # repo_path NOT provided — caller owns cleanup
            )

        assert result.worktree_removed is False
        assert wt_path.exists(), "worktree should still exist when repo_path is omitted"
        # Manual cleanup.
        _git(local, "worktree", "remove", str(wt_path), "--force")

    def test_worktree_removed_even_when_already_recorded(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Worktree cleanup runs even when the backstop defers to a prior report."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment
        import coord.issue_store as issue_store

        local, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            local,
            issue_number=22,
            issue_title="already recorded",
            assignment_id="fin03",
            default_branch="main",
            state_dir=state_dir,
        )
        assert wt_path.exists()

        _seed_running_assignment("fin03", assignment_type="review")
        # Simulate coord report-result having already written DONE.
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="fin03",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=22,
                    status="done",
                    verdict="approve",
                    summary="done early",
                )
            )

        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="fin03",
                repo_name="api",
                repo_github="acme/api",
                issue_number=22,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(local),
            )

        assert result.already_recorded is True
        assert result.worktree_removed is True
        assert not wt_path.exists()


# ── Artifact stash on interactive finalize (#562) ─────────────────────────────


class TestFinalizeStashesArtifacts:
    """finalize_interactive_exit must stash build artifacts before worktree removal."""

    def test_stash_on_normal_finalize(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Artifacts matching artifact_paths are stashed before the worktree is removed."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        local, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            local,
            issue_number=562,
            issue_title="stash test",
            assignment_id="stash01",
            default_branch="main",
            state_dir=state_dir,
        )

        # Place a fake binary in the worktree that matches the artifact pattern.
        bin_dir = wt_path / "target" / "debug"
        bin_dir.mkdir(parents=True)
        (bin_dir / "myapp").write_bytes(b"\x7fELF" + b"\x00" * 200)

        _seed_running_assignment("stash01")
        coord_dir = tmp_path / "coord"
        coord_dir.mkdir()
        with patch("coord.github_ops.post_issue_comment"), \
             patch("coord.state.COORD_DIR", coord_dir):
            result = finalize_interactive_exit(
                assignment_id="stash01",
                repo_name="myrepo",
                repo_github="acme/myrepo",
                issue_number=562,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(local),
                artifact_paths=["target/debug/myapp"],
            )

        # Worktree should be gone.
        assert result.worktree_removed is True
        assert not wt_path.exists()

        # Artifact should be stashed under coord_dir.
        # Branch name is deterministic: issue-{N}-{slug} from setup_interactive_worktree.
        from coord.agent import _sanitize_branch, _slugify
        expected_branch = f"issue-562-{_slugify('stash test')}"
        sanitized = _sanitize_branch(expected_branch)
        stash = coord_dir / "artifacts" / "myrepo" / sanitized
        assert stash.exists(), f"stash dir not created at {stash}"
        assert (stash / "myapp").exists(), "artifact not stashed before worktree removal"
        assert (stash / ".assignment_id").read_text() == "stash01"

    def test_no_stash_when_artifact_paths_empty(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """When artifact_paths is empty, no stash directory is created."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        local, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            local,
            issue_number=563,
            issue_title="no stash",
            assignment_id="stash02",
            default_branch="main",
            state_dir=state_dir,
        )

        _seed_running_assignment("stash02")
        coord_dir = tmp_path / "coord"
        coord_dir.mkdir()
        with patch("coord.github_ops.post_issue_comment"), \
             patch("coord.state.COORD_DIR", coord_dir):
            finalize_interactive_exit(
                assignment_id="stash02",
                repo_name="myrepo",
                repo_github="acme/myrepo",
                issue_number=563,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(local),
                # No artifact_paths — default None
            )

        assert not (coord_dir / "artifacts").exists(), (
            "no artifact stash dir should be created when artifact_paths is empty"
        )

    def test_stash_on_already_recorded_finalize(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Artifacts are stashed even when coord report-result already ran."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment
        import coord.issue_store as issue_store

        local, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            local,
            issue_number=564,
            issue_title="early report",
            assignment_id="stash03",
            default_branch="main",
            state_dir=state_dir,
        )

        # Put a binary in the worktree.
        bin_dir = wt_path / "target" / "debug"
        bin_dir.mkdir(parents=True)
        (bin_dir / "myapp").write_bytes(b"\x7fELF" + b"\x00" * 200)

        _seed_running_assignment("stash03", assignment_type="review")
        # Simulate coord report-result having already written DONE.
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="stash03",
                    machine_name="laptop",
                    repo_name="myrepo",
                    repo_github="acme/myrepo",
                    issue_number=564,
                    status="done",
                    verdict="approve",
                    summary="done via report-result",
                )
            )

        coord_dir = tmp_path / "coord"
        coord_dir.mkdir()
        with patch("coord.github_ops.post_issue_comment"), \
             patch("coord.state.COORD_DIR", coord_dir):
            result = finalize_interactive_exit(
                assignment_id="stash03",
                repo_name="myrepo",
                repo_github="acme/myrepo",
                issue_number=564,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(local),
                artifact_paths=["target/debug/myapp"],
            )

        # Already recorded path: stash should still have run.
        assert result.already_recorded is True
        assert result.worktree_removed is True
        # Artifact stashed using the branch the worktree HEAD is on.
        stash_base = coord_dir / "artifacts" / "myrepo"
        stash_files = list(stash_base.rglob("myapp")) if stash_base.exists() else []
        assert stash_files, "artifact should be stashed even when already_recorded=True"


# ── #611 branch-fallback: finalize records dispatch-time branch when worktree gone ──


class TestFinalizeBranchFallback:
    """#611: finalize_interactive_exit must never record branch=None for a done
    work row when a dispatch-time branch is known, even if the worktree has
    already been removed when finalize runs."""

    def test_dispatch_branch_recorded_when_worktree_missing(
        self, tmp_path: Path
    ) -> None:
        """When the worktree path doesn't exist at finalize time (already cleaned
        up), the dispatch-time branch passed as `branch=` is used instead of
        falling through to None."""
        from coord.interactive import finalize_interactive_exit
        from coord.state import get_connection
        from tests.test_issue_store_seam import _seed_running_assignment

        _seed_running_assignment("wk-branch-611", issue_number=611)

        # Point finalize at a worktree path that doesn't exist — this is the
        # scenario where the worktree was already removed before finalize ran.
        missing_wt = tmp_path / "worktrees" / "wk-branch-611"
        assert not missing_wt.exists()

        dispatch_branch = "issue-611-branch-fallback-test"

        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="wk-branch-611",
                repo_name="api",
                repo_github="acme/api",
                issue_number=611,
                machine_name="laptop",
                worktree_path=str(missing_wt),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=None,
                branch=dispatch_branch,
            )

        # Finalize should have used the dispatch-time branch, not None.
        row = get_connection().execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            ("wk-branch-611",),
        ).fetchone()
        assert row is not None
        assert row["branch"] == dispatch_branch, (
            f"expected dispatch-time branch {dispatch_branch!r}, got {row['branch']!r}; "
            "branch=None on a done work row greys the TUI Test/Review/Merge chain"
        )
        # The worktree wasn't there to push or read a current branch from.
        assert result.commits_ahead is None

    def test_review_finalize_no_worktree_records_branch_none(
        self, tmp_path: Path
    ) -> None:
        """A human-attended REVIEW legitimately has no branch — worktree_path is
        None and no `branch` is passed.  The fallback must not invent one."""
        from coord.interactive import finalize_interactive_exit
        from coord.state import get_connection
        from tests.test_issue_store_seam import _seed_running_assignment

        _seed_running_assignment("rv-no-branch-611", assignment_type="review", issue_number=611)

        with patch("coord.github_ops.post_issue_comment"), \
             patch("coord.interactive._git_push") as mock_push:
            result = finalize_interactive_exit(
                assignment_id="rv-no-branch-611",
                repo_name="api",
                repo_github="acme/api",
                issue_number=611,
                machine_name="laptop",
                worktree_path=None,   # review runs read-only in the live checkout
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=None,
                # No `branch` kwarg — review callers must never pass one
            )

        mock_push.assert_not_called()
        # branch must remain None for a read-only review session.
        row = get_connection().execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            ("rv-no-branch-611",),
        ).fetchone()
        assert row is not None
        assert row["branch"] is None, (
            f"review rows must stay branch=None; got {row['branch']!r}"
        )
        assert result.worktree_removed is False
