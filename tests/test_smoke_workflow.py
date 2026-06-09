"""Tests for the smoke test + PR workflow: coord test extensions, coord pr, coord fix."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord import merge_queue as mq
from coord.cli import main
from coord.models import Assignment, Board


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""

CONFIG_YAML_REVIEWS_DISABLED = CONFIG_YAML + "reviews:\n  enabled: false\n"


def _make_board(assignment: Assignment) -> Board:
    """Build a board with a single completed assignment."""
    return Board(completed=[assignment])


def _done_assignment(**overrides) -> Assignment:
    """Create a done assignment with sensible defaults."""
    defaults = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Add feature X",
        assignment_id="abc-123",
        status="done",
        branch="issue-42-feature-x",
    )
    defaults.update(overrides)
    return Assignment(**defaults)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
    """Provide an isolated in-memory DB for state and return a temp dir.

    Also redirects state.COORD_DIR to the temp dir so that CLI commands that
    use COORD_DIR for file I/O (e.g. test-output storage) don't touch the
    real ~/.coord directory.
    """
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_mod, "COORD_DIR", d)
    return d


# ── coord test --fail --output ──────────────────────────────────────────


class TestTestOutputCapture:
    def test_fail_with_output_stores_file(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--fail --output <file> stores the test output in ~/.coord/test_output/."""
        assignment = _done_assignment()
        board = _make_board(assignment)

        state_mod.save_board(board)

        # Create a fake test output file
        output_file = coord_dir / "my_test_output.log"
        output_file.write_text("FAIL: test_auth.py::test_login - AssertionError\n")

        result = CliRunner().invoke(
            main,
            [
                "test", "abc-123",
                "--fail",
                "--reason", "auth tests broken",
                "--output", str(output_file),
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "FAILED" in result.output
        assert "test output stored" in result.output

        # Verify stored file
        stored = coord_dir / "test_output" / "abc-123.txt"
        assert stored.exists()
        assert "test_auth.py" in stored.read_text()

    def test_fail_with_missing_output_warns(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--fail --output <nonexistent> warns but doesn't crash."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            [
                "test", "abc-123",
                "--fail",
                "--output", "/tmp/nonexistent_test_output_xyz.log",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "warning: output file not found" in result.output

    def test_fail_with_output_includes_path_in_reason(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """The stored path is encoded in smoke_test_reason for coord fix."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        output_file = coord_dir / "fail.log"
        output_file.write_text("error details here")

        CliRunner().invoke(
            main,
            [
                "test", "abc-123",
                "--fail",
                "--reason", "tests broke",
                "--output", str(output_file),
                "--config", str(config_file),
            ],
        )

        # Reload board and check
        reloaded = state_mod.load_board()
        a = reloaded.find_by_id("abc-123")
        assert "[output:" in a.smoke_test_reason
        assert "tests broke" in a.smoke_test_reason


class TestTestPassedHint:
    def test_passed_prints_merge_hint(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--passed prints the coord merge hint (review already ran; next step is merge)."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            [
                "test", "abc-123",
                "--passed",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "PASSED" in result.output
        assert "coord merge" in result.output


# ── coord pr ─────────────────────────────────────────────────────────────


class TestPr:
    def test_dispatches_with_correct_briefing(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr dispatches a worker with the right briefing content."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured_proposal = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured_proposal["proposal"] = proposal
            return {"id": "pr-001"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review", return_value=None):
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "pr-001" in result.output
        assert "dispatched" in result.output.lower()

        # Verify briefing contains required elements
        briefing = captured_proposal["proposal"].briefing
        assert "issue-42-feature-x" in briefing  # branch name
        assert "#42" in briefing  # issue number
        assert "main" in briefing  # default branch
        assert "gh pr create" in briefing
        assert "Closes #42" in briefing
        assert "Do NOT modify any code" in briefing

    def test_pr_on_assignment_without_branch_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr fails when assignment has no branch."""
        assignment = _done_assignment(branch=None)
        board = _make_board(assignment)
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["pr", "abc-123", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "no branch" in result.output.lower()

    def test_pr_on_non_done_assignment_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr fails when assignment is not done."""
        assignment = _done_assignment(status="running")
        board = Board(active=[assignment])
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["pr", "abc-123", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "running" in result.output.lower()

    def test_pr_dispatches_to_same_machine(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """PR worker is dispatched to the same machine as the original."""
        assignment = _done_assignment(machine_name="laptop")
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["machine"] = proposal.machine_name
            return {"id": "pr-002"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review", return_value=None):
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert captured["machine"] == "laptop"

    def test_pr_not_found(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr fails when assignment ID doesn't exist."""
        board = Board()
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["pr", "nonexistent", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_pr_records_dispatched(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr records the dispatch in the dispatched ledger."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "pr-003"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review", return_value=None):
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output

        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["assignment_id"] == "pr-003"
        assert records[0]["machine_name"] == "laptop"
        assert records[0]["repo_name"] == "api"

    def test_pr_dispatches_review_when_reviews_enabled(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr auto-dispatches a review when reviews are enabled."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        fake_review = _done_assignment(
            assignment_id="rev-001",
            machine_name="server",
            issue_title="[review] Add feature X",
            status="running",
        )

        with patch("coord.dispatch.dispatch", return_value={"id": "pr-004"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review", return_value=fake_review) as mock_review:
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "Review dispatched" in result.output
        assert "rev-001" in result.output
        assert "server" in result.output

        mock_review.assert_called_once()
        # Verify the original completed assignment was passed, not the PR worker
        passed_assignment = mock_review.call_args[0][0]
        assert passed_assignment.assignment_id == "abc-123"

    def test_pr_no_review_flag_skips_review(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--no-review skips the review dispatch even when reviews are enabled."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "pr-005"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review") as mock_review:
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--no-review", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        mock_review.assert_not_called()
        assert "Review dispatched" not in result.output

    def test_pr_reviews_disabled_skips_review(
        self, coord_dir: Path, tmp_path: Path
    ) -> None:
        """reviews disabled in config skips the review dispatch."""
        config_no_reviews = tmp_path / "coordinator_no_reviews.yml"
        config_no_reviews.write_text(CONFIG_YAML_REVIEWS_DISABLED)

        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "pr-006"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review") as mock_review:
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_no_reviews)],
            )

        assert result.exit_code == 0, result.output
        mock_review.assert_not_called()
        assert "Review dispatched" not in result.output

    def test_pr_review_returns_none_prints_note(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When dispatch_review returns None, coord pr prints a note and exits cleanly."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "pr-007"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review", return_value=None):
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "review not dispatched" in result.output


# ── coord fix ────────────────────────────────────────────────────────────


class TestFix:
    def test_dispatches_with_test_output(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord fix includes stored test output in the briefing."""
        assignment = _done_assignment(smoke_test="fail", smoke_test_reason="tests broke")
        board = _make_board(assignment)
        state_mod.save_board(board)

        # Store test output
        test_output_dir = coord_dir / "test_output"
        test_output_dir.mkdir(parents=True, exist_ok=True)
        (test_output_dir / "abc-123.txt").write_text(
            "FAIL: test_login.py - expected 200, got 401\n"
        )

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            return {"id": "fix-001"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                ["fix", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "fix-001" in result.output
        assert "dispatched" in result.output.lower()

        # Verify test output is in briefing
        assert "expected 200, got 401" in captured["briefing"]
        assert "issue-42-feature-x" in captured["briefing"]  # branch
        assert "#42" in captured["briefing"]  # issue number

    def test_fix_with_guidance(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--guidance text appears in the fix-up briefing."""
        assignment = _done_assignment(smoke_test="fail", smoke_test_reason="flaky")
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            return {"id": "fix-002"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "fix", "abc-123",
                    "--guidance", "The auth token is expired, mock it",
                    "--config", str(config_file),
                ],
            )

        assert result.exit_code == 0, result.output
        assert "The auth token is expired, mock it" in captured["briefing"]

    def test_fix_on_non_failed_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord fix fails when smoke_test is not 'fail'."""
        assignment = _done_assignment(smoke_test="pass")
        board = _make_board(assignment)
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["fix", "abc-123", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "expected 'fail'" in result.output.lower() or "pass" in result.output.lower()

    def test_fix_on_no_smoke_test_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord fix fails when no smoke test has been recorded."""
        assignment = _done_assignment(smoke_test=None)
        board = _make_board(assignment)
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["fix", "abc-123", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "expected 'fail'" in result.output.lower() or "none" in result.output.lower()

    def test_fix_not_found(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord fix fails when assignment ID doesn't exist."""
        board = Board()
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["fix", "nonexistent", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_fix_dispatches_to_same_machine(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Fix-up worker goes to the same machine."""
        assignment = _done_assignment(machine_name="laptop", smoke_test="fail")
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["machine"] = proposal.machine_name
            return {"id": "fix-003"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                ["fix", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert captured["machine"] == "laptop"

    def test_fix_briefing_has_continuation_structure(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Fix briefing has the expected structure: what was done, test failure, rules."""
        assignment = _done_assignment(
            smoke_test="fail", smoke_test_reason="segfault in parser"
        )
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            return {"id": "fix-004"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            CliRunner().invoke(
                main,
                ["fix", "abc-123", "--config", str(config_file)],
            )

        briefing = captured["briefing"]
        assert "## What was done" in briefing
        assert "## Test failure" in briefing
        assert "## Rules" in briefing
        assert "Do NOT start over" in briefing
        assert "git push origin HEAD" in briefing
        assert "segfault in parser" in briefing
