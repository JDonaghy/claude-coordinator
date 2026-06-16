"""Tests for the #604 merge-prep verification gate.

`coord assign --interactive --merge-of <work_aid>` is a human-attended agent
that rebases an approved branch onto the default branch and force-pushes it,
ready to merge.  It can get this wrong — rebase onto a stale base, or push a
polluted history dragging in unrelated already-merged commits — and still
self-report `done` (vimcode #494, 2026-06-15).

These tests pin the floor that catches that:

1. ``coord.agent.verify_merge_branch`` — the pure-git primitive (clean /
   behind-base / foreign-commit), exercised with local-only fixtures.
2. ``coord.interactive.finalize_interactive_exit(verify_merge=True)`` — the
   coordinator-side gate, which must record ``blocked`` (→ ``failed``) for a
   botched rebase, OVERRIDING any ``done`` the agent self-reported, and leave a
   clean rebase as ``done``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.agent import setup_interactive_worktree, verify_merge_branch


# ── helpers ──────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(cwd: Path, message: str, *, fname: str | None = None) -> str:
    """Make an empty-ish commit with *message*, return its sha."""
    f = fname or message.replace(" ", "_").replace("#", "n").replace("(", "").replace(
        ")", ""
    ).replace(":", "")
    (cwd / f).write_text(message + "\n")
    _git(cwd, "add", f)
    _git(cwd, "commit", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A local-only repo on ``main`` with one initial commit (no remote).

    `verify_merge_branch` falls back from ``origin/main`` to the local ``main``
    branch when no remote is configured, so this exercises the same code path a
    real merge worktree hits.
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    _commit(r, "initial")
    return r


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone whose ``origin`` is a local bare repo, on ``main``.

    Returns ``(clone, origin)``.  Mirrors the seam-test fixture so the
    commits-ahead / verify primitives have a real ``origin/main`` to count
    against.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "remote", "add", "origin", str(origin))
    _commit(clone, "initial")
    _git(clone, "push", "-u", "origin", "main")
    return clone, origin


# ── verify_merge_branch (pure-git primitive) ─────────────────────────────────


class TestVerifyMergeBranch:
    def test_clean_branch_is_ok(self, local_repo: Path) -> None:
        """Branch off current main + only the issue's commit → ok."""
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "feat(#604): real work")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)

        assert mv.default_ahead == 0
        assert len(mv.added) == 1
        assert mv.foreign == []
        assert mv.ok is True

    def test_branch_behind_base_is_blocked(self, local_repo: Path) -> None:
        """main advances after the branch forks (no rebase) → default_ahead>0."""
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "feat(#604): real work")
        # main moves on; the feature branch never rebased onto it.
        _git(local_repo, "checkout", "main")
        _commit(local_repo, "unrelated main progress")
        _git(local_repo, "checkout", "issue-604-fix")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)

        assert mv.default_ahead == 1, "branch is missing 1 commit from main"
        assert mv.ok is False

    def test_foreign_commit_is_blocked(self, local_repo: Path) -> None:
        """A dragged-in commit referencing a DIFFERENT issue → foreign → blocked."""
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "feat(#604): real work")
        _commit(local_repo, "fix(#514): unrelated already-merged work")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)

        assert mv.default_ahead == 0
        assert len(mv.foreign) == 1
        _, subj = mv.foreign[0]
        assert "#514" in subj
        assert mv.ok is False

    def test_commit_without_issue_ref_is_not_foreign(self, local_repo: Path) -> None:
        """A bare-message commit (no #NNN) is the branch's own work, not foreign."""
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "wip refactor")
        _commit(local_repo, "feat(#604): real work")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)

        assert mv.foreign == []
        assert mv.ok is True

    def test_commit_referencing_own_and_other_issue_not_foreign(
        self, local_repo: Path
    ) -> None:
        """Referencing the issue (even alongside another #NNN) is never foreign."""
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "feat(#604): also relates to #500")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)

        assert mv.foreign == []
        assert mv.ok is True

    def test_missing_base_ref_is_not_ok(self, local_repo: Path) -> None:
        """An unresolvable base ref → default_ahead None → NOT ok (conservative)."""
        mv = verify_merge_branch(
            local_repo, base="does-not-exist", issue_number=604
        )
        assert mv.default_ahead is None
        assert mv.ok is False

    def test_block_summary_mentions_missing_and_foreign(
        self, local_repo: Path
    ) -> None:
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "fix(#514): foreign")
        _git(local_repo, "checkout", "main")
        _commit(local_repo, "main moves")
        _git(local_repo, "checkout", "issue-604-fix")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)
        summary = mv.block_summary("main")
        assert "missing" in summary
        assert "foreign" in summary.lower()
        assert "#514" in summary


# ── finalize_interactive_exit(verify_merge=True) — the coordinator gate ───────


def _read_status(assignment_id: str) -> str | None:
    from coord import state as state_mod

    row = state_mod.get_connection().execute(
        "SELECT status FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    return None if row is None else row["status"]


class TestFinalizeMergeGate:
    def test_clean_rebase_records_done(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """A clean merge worktree (base contained, only the issue's commit) is
        recorded ``done`` by the gate."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        clone, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            clone,
            issue_number=604,
            issue_title="merge gate clean",
            assignment_id="mg-clean",
            default_branch="main",
            state_dir=state_dir,
        )
        # The worktree branched off current main (== origin/main); add the
        # issue's own commit so there is something to merge.
        _commit(wt_path, "feat(#604): the fix")

        _seed_running_assignment("mg-clean", issue_number=604)
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="mg-clean",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(clone),
                verify_merge=True,
            )

        assert result.merge_verify is not None
        assert result.merge_verify.ok is True
        assert result.terminal_status == "done"
        assert _read_status("mg-clean") == "done"
        assert not wt_path.exists()

    def test_polluted_rebase_blocks_and_overrides_self_reported_done(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """The incident: agent self-reports ``done`` but the branch is behind
        origin/main (botched rebase).  Git truth must override → ``failed``."""
        import coord.issue_store as issue_store
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        clone, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            clone,
            issue_number=604,
            issue_title="merge gate polluted",
            assignment_id="mg-bad",
            default_branch="main",
            state_dir=state_dir,
        )
        _commit(wt_path, "feat(#604): the fix")
        # Advance origin/main AFTER the branch forked, without rebasing the
        # worktree onto it → the branch is now missing a commit from main.
        _commit(clone, "main moved on")
        _git(clone, "push", "origin", "main")  # updates shared refs/remotes/origin/main

        _seed_running_assignment("mg-bad", issue_number=604)
        # Agent self-reported DONE before exiting (the false success).
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="mg-bad",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=604,
                    status="done",
                    verdict=None,
                    summary="rebased and pushed",
                )
            )
        assert _read_status("mg-bad") == "done"

        with patch("coord.github_ops.post_issue_comment") as post:
            result = finalize_interactive_exit(
                assignment_id="mg-bad",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(clone),
                verify_merge=True,
            )

        # Git truth overrode the agent's self-reported done.
        assert result.merge_verify is not None
        assert result.merge_verify.ok is False
        assert result.merge_verify.default_ahead == 1
        assert result.already_recorded is True, "the prior done must be visible"
        assert result.terminal_status == "failed"  # blocked → failed board state
        assert _read_status("mg-bad") == "failed"
        # A failure comment with the reason was posted.
        post.assert_called()
        body = post.call_args.args[2]
        assert "604" in body
        assert not wt_path.exists()

    def test_foreign_commit_blocks(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Base fully contained but a foreign #NNN commit was dragged in → blocked."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        clone, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            clone,
            issue_number=604,
            issue_title="merge gate foreign",
            assignment_id="mg-foreign",
            default_branch="main",
            state_dir=state_dir,
        )
        _commit(wt_path, "feat(#604): the fix")
        _commit(wt_path, "fix(#514): unrelated already-merged work")

        _seed_running_assignment("mg-foreign", issue_number=604)
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="mg-foreign",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(clone),
                verify_merge=True,
            )

        assert result.merge_verify is not None
        assert result.merge_verify.ok is False
        assert len(result.merge_verify.foreign) == 1
        assert result.terminal_status == "failed"
        assert _read_status("mg-foreign") == "failed"

    def test_verify_merge_off_leaves_review_path_untouched(self) -> None:
        """Without verify_merge, a 0-commit review session (already-recorded)
        still defers to the agent — the gate must not touch other flavours."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment
        import coord.issue_store as issue_store

        _seed_running_assignment("rev-x", issue_number=604)
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="rev-x",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=604,
                    status="done",
                    verdict="approve",
                    summary="LGTM",
                )
            )
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="rev-x",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="laptop",
                worktree_path=None,  # review: no worktree
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=None,
                # verify_merge defaults False
            )
        assert result.already_recorded is True
        assert result.merge_verify is None
        assert _read_status("rev-x") == "done"
