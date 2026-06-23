"""Tests for coord test — smoke testing workflow."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import load
from coord.models import Assignment, Board, Repo
from coord.state import save_board


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    return d


@pytest.fixture
def config_file(tmp_path: Path, repo_dir: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        f"  - name: api\n    github: acme/api\n"
        f"    build_command: 'echo build-ok'\n"
        f"    test_command: 'echo test-ok'\n"
        "machines:\n"
        f"  - name: testbox\n    host: testbox.tailnet\n    repos: [api]\n"
        f"    repo_paths:\n      api: {repo_dir}\n"
    )
    return p


@pytest.fixture
def board_with_done(coord_db) -> Board:
    board = Board(completed=[
        Assignment(
            machine_name="testbox",
            repo_name="api",
            issue_number=42,
            issue_title="Fix auth",
            assignment_id="abc123",
            status="done",
            branch="issue-42-fix-auth",
            finished_at=1000.0,
        ),
    ])
    save_board(board)
    return board


# ── Config parsing ──────────────────────────────────────────────────────────


class TestRepoConfig:
    def test_build_and_test_commands_parsed(self, config_file: Path) -> None:
        cfg = load(config_file)
        repo = cfg.repo("api")
        assert repo.build_command == "echo build-ok"
        assert repo.test_command == "echo test-ok"

    def test_commands_optional(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.repo("api").build_command is None
        assert cfg.repo("api").test_command is None

    @pytest.mark.skipif(
        not (Path(__file__).resolve().parents[1] / "coordinator.yml").exists(),
        reason="coordinator.yml is gitignored",
    )
    def test_example_config_parses(self) -> None:
        cfg = load(Path(__file__).resolve().parents[1] / "coordinator.yml")
        assert any(r.build_command for r in cfg.repos)


# ── Verdict recording ──────────────────────────────────────────────────────


class TestSmokeVerdict:
    def test_pass_records_on_board(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--passed",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "PASSED" in result.output

        from coord.state import load_board
        board = load_board()
        assert board.completed[0].smoke_test == "pass"

    def test_fail_records_reason(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--fail", "--reason", "UI is broken",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "FAILED" in result.output
        assert "UI is broken" in result.output

        from coord.state import load_board
        board = load_board()
        assert board.completed[0].smoke_test == "fail"
        assert board.completed[0].smoke_test_reason == "UI is broken"

    def test_unknown_assignment_errors(self, config_file: Path, coord_db) -> None:
        save_board(Board())
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "nonexistent", "--passed",
            "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "not found" in result.output


# ── #271 part 1: default-branch restoration after pass/skip ────────────────


class TestRestoreDefaultBranch:
    """`coord test --passed` / `--skipped` should switch the local repo
    back to its `default_branch` after recording the verdict.  Local
    testing is done — restore the user's workflow."""

    @patch("subprocess.run")
    def test_passed_restores_default_branch(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        # First call: rev-parse for current branch (returns the worker
        # branch). Second call: git checkout main.
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="issue-42-fix-auth\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        result = CliRunner().invoke(main, [
            "test", "abc123", "--passed", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        assert "PASSED" in result.output

        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        # Expect rev-parse + checkout main (config_file's default_branch is "main").
        assert any(c.args[0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                   for c in git_calls)
        assert any(c.args[0] == ["git", "checkout", "main"] for c in git_calls)

    @patch("subprocess.run")
    def test_passed_removes_test_worktree(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        # #561: a pass verdict cleans up the throwaway test worktree.
        wt = tmp_path / "wt-abc123"
        wt.mkdir()
        monkeypatch.setattr("coord.cli._test_worktree_path", lambda aid, repo: wt)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = CliRunner().invoke(main, [
            "test", "abc123", "--passed", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        git_calls = [c.args[0] for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        assert any(
            call[:4] == ["git", "worktree", "remove", "--force"] and str(wt) in call
            for call in git_calls
        ), git_calls

    @patch("subprocess.run")
    def test_skipped_restores_default_branch(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="issue-42-fix-auth\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        result = CliRunner().invoke(main, [
            "test", "abc123", "--skipped", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        assert any(c.args[0] == ["git", "checkout", "main"] for c in git_calls)

    @patch("subprocess.run")
    def test_failed_leaves_branch_checked_out(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        """--fail must NOT restore — user may want to dig further on the failure."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = CliRunner().invoke(main, [
            "test", "abc123", "--fail", "--reason", "broken",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        # No git checkout main — only the verdict was recorded.
        assert not any(c.args[0] == ["git", "checkout", "main"] for c in git_calls)

    @patch("subprocess.run")
    def test_already_on_default_branch_is_silent_no_op(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        """When `rev-parse HEAD` says we're already on `main`, the
        helper doesn't attempt a no-op `git checkout main` and doesn't
        echo a misleading 'restored' line."""
        mock_run.return_value = MagicMock(returncode=0, stdout="main\n", stderr="")
        result = CliRunner().invoke(main, [
            "test", "abc123", "--passed", "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "restored: main" not in result.output

        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        # rev-parse only — no checkout.
        assert any(c.args[0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                   for c in git_calls)
        assert not any(c.args[0] == ["git", "checkout", "main"] for c in git_calls)

    @patch("subprocess.run")
    def test_dirty_tree_failure_warns_but_passes_verdict(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        """If the user has manual edits on the worker branch (dirty
        tree), git checkout fails.  The verdict still records cleanly
        and the user gets a warning to stash + retry."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="issue-42-fix-auth\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="error: would overwrite local changes"),
        ]
        result = CliRunner().invoke(main, [
            "test", "abc123", "--passed", "--config", str(config_file),
        ])
        # Verdict still records (exit 0) even when restore fails.
        assert result.exit_code == 0
        assert "PASSED" in result.output
        assert "warning" in result.output.lower()


# ── Branch checkout ─────────────────────────────────────────────────────────


class TestBranchCheckout:
    @patch("subprocess.run")
    def test_builds_in_throwaway_worktree_never_checks_out_base(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        # #561: build in a throwaway worktree fetched fresh — NEVER `git
        # checkout` in the base checkout (it's the live coordinator source).
        monkeypatch.setattr(
            "coord.cli._test_worktree_path", lambda aid, repo: tmp_path / f"wt-{aid}"
        )
        mock_run.return_value = MagicMock(returncode=0)

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])

        assert result.exit_code == 0, result.output
        assert "worktree" in result.output.lower()

        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        assert git_calls[0].args[0] == ["git", "fetch", "origin", "--prune"]
        add = git_calls[1].args[0]
        assert add[:5] == ["git", "worktree", "add", "--force", "--detach"]
        assert add[-1] == "origin/issue-42-fix-auth"
        # The base checkout's HEAD must never move — no `git checkout` at all.
        assert not any(c.args[0][:2] == ["git", "checkout"] for c in git_calls)

    def test_no_branch_errors(self, config_file: Path, coord_db) -> None:
        board = Board(completed=[
            Assignment(
                machine_name="testbox", repo_name="api",
                issue_number=1, issue_title="x",
                assignment_id="nobranch", status="done",
                branch=None,
            ),
        ])
        save_board(board)

        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "nobranch", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "no branch" in result.output

    @patch("subprocess.run")
    def test_git_fetch_failure_reported(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            "coord.cli._test_worktree_path", lambda aid, repo: tmp_path / f"wt-{aid}"
        )
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "git fetch", stderr="fatal: not a git repository"
        )

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "git fetch failed" in result.output


# ── Help text ───────────────────────────────────────────────────────────────


class TestHelpText:
    def test_coord_test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["test", "--help"])
        assert result.exit_code == 0
        assert "--passed" in result.output
        assert "--fail" in result.output
        assert "--reason" in result.output


# ── Branch reconciliation (#bounce-followup) ────────────────────────────────


class TestBranchReconciliation:
    """When `git checkout <db_branch>` fails with pathspec error AND
    the assignment has a PR in merge_queue, `coord test` falls back to
    `gh pr view --json headRefName` to learn the real branch, updates
    the DB, and retries.  Defends against:
      - auto-loop creating orphan branches (pre-#target_branch fix)
      - slugifier max_len changing across releases
      - manual `git branch -m` on origin
    """

    @patch("subprocess.run")
    def test_reconciles_when_pr_has_different_head_ref(
        self,
        mock_run: MagicMock,
        config_file: Path,
        board_with_done: Board,
        repo_dir: Path,
        coord_db,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        from coord.db import get_connection
        from coord.state import get_connection as _gc  # ensure import path
        _ = _gc

        monkeypatch.setattr(
            "coord.cli._test_worktree_path", lambda aid, repo: tmp_path / f"wt-{aid}"
        )

        # Seed a merge_queue row pointing the assignment at PR #999.
        conn = get_connection()
        conn.execute(
            "INSERT INTO merge_queue "
            "(assignment_id, repo_name, repo_github, branch, target_branch, "
            "issue_number, issue_title, state, pr_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "abc123", "api", "acme/api",
                "issue-42-fix-auth",  # stale name (DB)
                "main", 42, "Fix auth", "pending", 999,
            ),
        )
        conn.commit()

        # subprocess.run call sequence (#561 worktree path):
        # 1. git fetch origin --prune                        → ok
        # 2. git worktree add ... origin/issue-42-fix-auth   → FAIL (returncode 1)
        # 3. gh pr view 999 ... --json headRefName --jq ...  → returns the real name
        # 4. git rev-parse --verify origin/<real-name>       → ok (non-mutating)
        # 5. git worktree add ... origin/<real-name>         → ok (retry)
        # 6. echo build-ok                                   → ok (config_file's build_command)
        # 7. echo test-ok                                    → ok (config_file's test_command)
        real_branch = "issue-42-fix-auth-fix-1-additional-error-handling"
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git fetch --prune
            MagicMock(returncode=1, stdout="", stderr="fatal: invalid reference: origin/issue-42-fix-auth"),  # worktree add FAIL
            MagicMock(returncode=0, stdout=f"{real_branch}\n", stderr=""),  # gh pr view
            MagicMock(returncode=0, stdout="", stderr=""),  # git rev-parse --verify
            MagicMock(returncode=0, stdout="", stderr=""),  # git worktree add (retry)
            MagicMock(returncode=0, stdout="", stderr=""),  # echo build-ok
            MagicMock(returncode=0, stdout="", stderr=""),  # echo test-ok
        ]

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        assert "drift reconciled" in result.output
        # DB was updated.
        row = conn.execute(
            "SELECT branch FROM merge_queue WHERE assignment_id='abc123'",
        ).fetchone()
        assert row["branch"] == real_branch
        row2 = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id='abc123'",
        ).fetchone()
        assert row2["branch"] == real_branch

    @patch("subprocess.run")
    def test_falls_through_to_error_when_no_pr_in_merge_queue(
        self,
        mock_run: MagicMock,
        config_file: Path,
        board_with_done: Board,
        repo_dir: Path,
        coord_db,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """No PR registered → can't ask GitHub.  Reconciliation
        returns None and the worktree-add error is surfaced."""
        monkeypatch.setattr(
            "coord.cli._test_worktree_path", lambda aid, repo: tmp_path / f"wt-{aid}"
        )
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git fetch --prune
            MagicMock(returncode=1, stdout="", stderr="fatal: invalid reference: origin/issue-42-fix-auth"),  # worktree add FAIL
        ]

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "could not create test worktree" in result.output

    @patch("subprocess.run")
    def test_does_not_reconcile_when_head_ref_matches_db(
        self,
        mock_run: MagicMock,
        config_file: Path,
        board_with_done: Board,
        repo_dir: Path,
        coord_db,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """The PR's headRefName equals the DB-recorded branch — the
        worktree-add failure is unrelated (e.g. local clone missing the
        ref).  Don't pretend we fixed it; surface the original error."""
        monkeypatch.setattr(
            "coord.cli._test_worktree_path", lambda aid, repo: tmp_path / f"wt-{aid}"
        )
        from coord.db import get_connection
        conn = get_connection()
        conn.execute(
            "INSERT INTO merge_queue "
            "(assignment_id, repo_name, repo_github, branch, target_branch, "
            "issue_number, issue_title, state, pr_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "abc123", "api", "acme/api",
                "issue-42-fix-auth", "main", 42, "Fix auth",
                "pending", 999,
            ),
        )
        conn.commit()

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git fetch --prune
            MagicMock(returncode=1, stdout="", stderr="fatal: invalid reference: origin/issue-42-fix-auth"),  # worktree add FAIL
            # gh returns the SAME branch — no drift, nothing to fix.
            MagicMock(returncode=0, stdout="issue-42-fix-auth\n", stderr=""),
        ]

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "could not create test worktree" in result.output
        assert "drift reconciled" not in result.output


# ── #685: coord set-test-mode ────────────────────────────────────────────────


class TestSetTestModeCommand:
    """Tests for `coord set-test-mode REPO ISSUE MODE`."""

    def _fake_gh_view(self, labels: list[str]):
        """Return a mock subprocess.run result for `gh issue view --json labels`."""
        import json
        return MagicMock(returncode=0, stdout=json.dumps({"labels": [{"name": l} for l in labels]}), stderr="")

    @patch("subprocess.run")
    def test_set_test_mode_smoke(
        self,
        mock_run: MagicMock,
        config_file: Path,
        coord_db,
    ) -> None:
        """set-test-mode smoke adds the test-mode:smoke label."""
        import json
        # gh view returns no existing test-mode label; gh edit succeeds.
        mock_run.side_effect = [
            self._fake_gh_view([]),          # gh issue view
            MagicMock(returncode=0, stdout="", stderr=""),  # gh issue edit
        ]
        result = CliRunner().invoke(main, [
            "set-test-mode", "api", "42", "smoke", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        assert "smoke" in result.output

        # Should have called `gh issue edit --add-label test-mode:smoke`.
        edit_call = mock_run.call_args_list[1]
        edit_args = edit_call[0][0]
        assert "--add-label" in edit_args
        assert "test-mode:smoke" in edit_args

    @patch("subprocess.run")
    def test_set_test_mode_auto(
        self,
        mock_run: MagicMock,
        config_file: Path,
        coord_db,
    ) -> None:
        """set-test-mode auto adds the test-mode:auto label."""
        import json
        mock_run.side_effect = [
            self._fake_gh_view([]),          # gh issue view
            MagicMock(returncode=0, stdout="", stderr=""),  # gh issue edit
        ]
        result = CliRunner().invoke(main, [
            "set-test-mode", "api", "42", "auto", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        assert "auto" in result.output

        edit_call = mock_run.call_args_list[1]
        edit_args = edit_call[0][0]
        assert "--add-label" in edit_args
        assert "test-mode:auto" in edit_args

    @patch("subprocess.run")
    def test_set_test_mode_removes_old_label(
        self,
        mock_run: MagicMock,
        config_file: Path,
        coord_db,
    ) -> None:
        """set-test-mode smoke removes an existing test-mode:auto label."""
        mock_run.side_effect = [
            self._fake_gh_view(["coord", "test-mode:auto"]),  # gh issue view
            MagicMock(returncode=0, stdout="", stderr=""),     # gh issue edit
        ]
        result = CliRunner().invoke(main, [
            "set-test-mode", "api", "42", "smoke", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        edit_call = mock_run.call_args_list[1]
        edit_args = edit_call[0][0]
        assert "--remove-label" in edit_args
        assert "test-mode:auto" in edit_args

    def test_set_test_mode_rejects_invalid_mode(
        self,
        config_file: Path,
    ) -> None:
        """Passing an invalid mode should fail with a non-zero exit."""
        result = CliRunner().invoke(main, [
            "set-test-mode", "api", "42", "invalid", "--config", str(config_file),
        ])
        assert result.exit_code != 0
