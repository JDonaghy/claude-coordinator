"""Tests for the interactive `--smoke-of` live-checkout restore-on-exit
safety net (#1256).

`--smoke-of` runs the human-attended smoke agent directly in the LIVE
checkout (no worktree — #1010), because exercising an agent-side file
(e.g. `coord/agent.py`) requires that branch's version of the file to
actually sit in the editable-install path.  The only way to do that is a
path-scoped `git checkout <branch> -- <path>` in the live tree, which used
to be left dirty forever once the session ended (the concrete incident:
#1248 on elitebook).

Covers:
1. `snapshot_live_checkout_for_smoke` / `restore_live_checkout_from_smoke_snapshot`
   directly — the pair reverts a path-scoped mutation, leaves pre-existing
   dirt untouched, and restores HEAD if it drifted.
2. `finalize_interactive_exit` wired with `smoke_repo_path=...` runs the
   restore and surfaces it on `InteractiveFinalizeResult`.
3. Every OTHER interactive flavour (smoke_repo_path omitted, e.g. a
   read-only review) is completely unaffected — the restore step is a
   silent no-op without a matching snapshot marker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.interactive import (
    finalize_interactive_exit,
    restore_live_checkout_from_smoke_snapshot,
    snapshot_live_checkout_for_smoke,
)
from tests.test_issue_store_seam import _seed_running_assignment


# ── Helpers ──────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _porcelain(cwd: Path) -> str:
    return _git(cwd, "status", "--porcelain")


@pytest.fixture
def live_checkout(tmp_path: Path) -> Path:
    """A minimal local git repo standing in for the coordinator's own live
    checkout — a branch (`feature`) exists with a modified agent-side file,
    mirroring the #1248 incident shape."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    (r / "agent.py").write_text("original\n")
    (r / "README").write_text("init\n")
    _git(r, "add", "agent.py", "README")
    _git(r, "commit", "-m", "initial")

    _git(r, "checkout", "-b", "feature")
    (r / "agent.py").write_text("branch version\n")
    _git(r, "add", "agent.py")
    _git(r, "commit", "-m", "agent-side change")
    _git(r, "checkout", "main")
    return r


# ── snapshot + restore, called directly ─────────────────────────────────────


class TestSnapshotAndRestore:
    def test_reverts_path_scoped_checkout(self, live_checkout: Path) -> None:
        """The core #1256 scenario: a path-scoped `git checkout <branch> --
        <path>` used to exercise an agent-side file is fully reverted."""
        snapshot_live_checkout_for_smoke(str(live_checkout), "smoke-aid-1")

        # Simulate what the smoke agent did: pull the branch's version of
        # agent.py into the live tree WITHOUT moving HEAD (#1010 contract).
        _git(live_checkout, "checkout", "feature", "--", "agent.py")
        assert _porcelain(live_checkout) != "", "checkout should have dirtied the tree"
        assert (live_checkout / "agent.py").read_text() == "branch version\n"

        restored, error = restore_live_checkout_from_smoke_snapshot(
            str(live_checkout), "smoke-aid-1"
        )

        assert error is None
        assert restored, "restore should report what it reverted"
        assert _porcelain(live_checkout) == "", "live checkout must be clean after restore"
        assert (live_checkout / "agent.py").read_text() == "original\n"
        assert _git(live_checkout, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_reverts_new_file_added_by_checkout(self, live_checkout: Path) -> None:
        """A path that doesn't exist on HEAD (newly added on the branch) must
        be un-added, not just left staged."""
        _git(live_checkout, "checkout", "feature")
        (live_checkout / "new_agent_helper.py").write_text("new on branch\n")
        _git(live_checkout, "add", "new_agent_helper.py")
        _git(live_checkout, "commit", "-m", "add helper")
        _git(live_checkout, "checkout", "main")

        snapshot_live_checkout_for_smoke(str(live_checkout), "smoke-aid-new")
        _git(live_checkout, "checkout", "feature", "--", "new_agent_helper.py")
        assert (live_checkout / "new_agent_helper.py").exists()
        assert _porcelain(live_checkout) != ""

        restored, error = restore_live_checkout_from_smoke_snapshot(
            str(live_checkout), "smoke-aid-new"
        )

        assert error is None
        assert restored
        assert _porcelain(live_checkout) == ""
        assert not (live_checkout / "new_agent_helper.py").exists()

    def test_leaves_preexisting_dirt_untouched(self, live_checkout: Path) -> None:
        """Dirt that pre-dates the smoke session must survive the restore —
        only the delta introduced DURING the session is reverted."""
        (live_checkout / "README").write_text("pre-existing manual edit\n")
        assert _porcelain(live_checkout) != ""

        snapshot_live_checkout_for_smoke(str(live_checkout), "smoke-aid-2")

        # Smoke session dirties a SECOND, different file.
        _git(live_checkout, "checkout", "feature", "--", "agent.py")
        assert len(_porcelain(live_checkout).splitlines()) == 2

        restored, error = restore_live_checkout_from_smoke_snapshot(
            str(live_checkout), "smoke-aid-2"
        )

        assert error is None
        assert any("agent.py" in r for r in restored)
        status_lines = _porcelain(live_checkout).splitlines()
        assert len(status_lines) == 1
        assert "README" in status_lines[0]
        assert (live_checkout / "README").read_text() == "pre-existing manual edit\n"

    def test_restores_branch_when_head_drifted(self, live_checkout: Path) -> None:
        """Sibling symptom from the issue: HEAD itself moved during the
        session (a full branch switch instead of a path-scoped checkout)."""
        snapshot_live_checkout_for_smoke(str(live_checkout), "smoke-aid-3")

        _git(live_checkout, "checkout", "feature")
        assert _git(live_checkout, "rev-parse", "--abbrev-ref", "HEAD") == "feature"

        restored, error = restore_live_checkout_from_smoke_snapshot(
            str(live_checkout), "smoke-aid-3"
        )

        assert error is None
        assert any("branch" in r for r in restored)
        assert _git(live_checkout, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_noop_without_a_snapshot(self, live_checkout: Path) -> None:
        """No prior `snapshot_live_checkout_for_smoke` call ⇒ silent no-op,
        even if the tree happens to be dirty — nothing to compare against."""
        (live_checkout / "README").write_text("dirty, no snapshot taken\n")

        restored, error = restore_live_checkout_from_smoke_snapshot(
            str(live_checkout), "never-snapshotted"
        )

        assert restored == []
        assert error is None
        assert _porcelain(live_checkout) != "", "must not touch anything without a baseline"

    def test_restore_is_idempotent(self, live_checkout: Path) -> None:
        """Calling restore twice (e.g. dispatch-exit finalize THEN a later
        `coord reattach` on the same session) must not error the second time."""
        snapshot_live_checkout_for_smoke(str(live_checkout), "smoke-aid-4")
        _git(live_checkout, "checkout", "feature", "--", "agent.py")

        first = restore_live_checkout_from_smoke_snapshot(str(live_checkout), "smoke-aid-4")
        second = restore_live_checkout_from_smoke_snapshot(str(live_checkout), "smoke-aid-4")

        assert first[1] is None
        assert second == ([], None)


# ── finalize_interactive_exit integration ───────────────────────────────────


class TestFinalizeRunsRestore:
    def test_smoke_repo_path_triggers_restore(self, live_checkout: Path) -> None:
        _seed_running_assignment("fin-smoke-1256", assignment_type="smoke", issue_number=1256)
        snapshot_live_checkout_for_smoke(str(live_checkout), "fin-smoke-1256")
        _git(live_checkout, "checkout", "feature", "--", "agent.py")
        assert _porcelain(live_checkout) != ""

        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="fin-smoke-1256",
                repo_name="api",
                repo_github="acme/api",
                issue_number=1256,
                machine_name="laptop",
                worktree_path=None,  # smoke runs in the live checkout, no worktree
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=None,
                smoke_repo_path=str(live_checkout),
            )

        assert result.smoke_restore_error is None
        assert result.smoke_restored_paths
        assert _porcelain(live_checkout) == "", (
            "git status must be clean after finalize_interactive_exit for a "
            "completed smoke session (#1256 acceptance bar)"
        )

    def test_runs_even_when_already_recorded(self, live_checkout: Path) -> None:
        """A smoke session that self-reports via `coord report-result` before
        exiting must still get the live checkout restored — the
        already-recorded early-return must not skip it."""
        import coord.issue_store as issue_store

        _seed_running_assignment("fin-smoke-early", assignment_type="smoke", issue_number=1256)
        snapshot_live_checkout_for_smoke(str(live_checkout), "fin-smoke-early")
        _git(live_checkout, "checkout", "feature", "--", "agent.py")

        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="fin-smoke-early",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=1256,
                    status="done",
                    verdict=None,
                    summary="reported early",
                )
            )

        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="fin-smoke-early",
                repo_name="api",
                repo_github="acme/api",
                issue_number=1256,
                machine_name="laptop",
                worktree_path=None,
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=None,
                smoke_repo_path=str(live_checkout),
            )

        assert result.already_recorded is True
        assert result.smoke_restored_paths
        assert _porcelain(live_checkout) == ""

    def test_other_interactive_flavours_unaffected(self) -> None:
        """A read-only REVIEW (worktree_path=None, no smoke_repo_path — the
        existing shape) must behave exactly as before: no restore fields
        populated, no git commands run against anything."""
        _seed_running_assignment("rev-no-smoke-1256", assignment_type="review", issue_number=1256)

        with patch("coord.github_ops.post_issue_comment"), patch(
            "coord.interactive.restore_live_checkout_from_smoke_snapshot"
        ) as mock_restore:
            result = finalize_interactive_exit(
                assignment_id="rev-no-smoke-1256",
                repo_name="api",
                repo_github="acme/api",
                issue_number=1256,
                machine_name="laptop",
                worktree_path=None,
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=None,
                # smoke_repo_path NOT passed — every non-smoke caller today.
            )

        mock_restore.assert_not_called()
        assert result.smoke_restored_paths == []
        assert result.smoke_restore_error is None
