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


# ── Branch checkout ─────────────────────────────────────────────────────────


class TestBranchCheckout:
    @patch("subprocess.run")
    def test_fetches_and_checks_out_branch(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])

        assert result.exit_code == 0
        assert "checked out" in result.output

        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        assert len(git_calls) == 2
        assert git_calls[0].args[0] == ["git", "fetch", "origin"]
        assert git_calls[1].args[0] == ["git", "checkout", "issue-42-fix-auth"]

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
    def test_git_failure_reported(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "git fetch", stderr="fatal: not a git repository"
        )

        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "git command failed" in result.output


# ── Help text ───────────────────────────────────────────────────────────────


class TestHelpText:
    def test_coord_test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["test", "--help"])
        assert result.exit_code == 0
        assert "--passed" in result.output
        assert "--fail" in result.output
        assert "--reason" in result.output
