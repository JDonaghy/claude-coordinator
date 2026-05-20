"""CLI tests for `coord assign`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord import merge_queue as mq
from coord.cli import main


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


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "state"
    monkeypatch.setattr(state_mod, "COORD_DIR", d)
    monkeypatch.setattr(state_mod, "PROPOSALS_FILE", d / "proposals.json")
    monkeypatch.setattr(state_mod, "DISPATCHED_FILE", d / "dispatched.json")
    monkeypatch.setattr(state_mod, "NOTIFIED_FILE", d / "notified.json")
    monkeypatch.setattr(state_mod, "BOARD_FILE", d / "board.json")
    monkeypatch.setattr(state_mod, "SESSION_FILE", d / "session.json")
    monkeypatch.setattr(mq, "QUEUE_FILE", d / "merge_queue.json")
    return d


class TestAssignValidation:
    """Test argument validation before any network calls."""

    def test_unknown_machine(self, config_file: Path, coord_dir: Path) -> None:
        result = CliRunner().invoke(
            main, ["assign", "ghost", "api", "42", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "ghost" in result.output

    def test_unknown_repo(self, config_file: Path, coord_dir: Path) -> None:
        result = CliRunner().invoke(
            main, ["assign", "laptop", "nope", "42", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "nope" in result.output

    def test_machine_cannot_work_on_repo(self, tmp_path: Path, coord_dir: Path) -> None:
        """Machine exists but doesn't list the requested repo."""
        cfg = """\
repos:
  - name: api
    github: acme/api
  - name: web
    github: acme/web
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(cfg)
        result = CliRunner().invoke(
            main, ["assign", "laptop", "web", "1", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "does not list repo" in result.output


class TestAssignDryRun:
    def test_dry_run_does_not_dispatch(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Add feature X"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0
        assert "dry run" in result.output
        assert "laptop" in result.output
        assert "#42" in result.output
        assert "Add feature X" in result.output

    def test_dry_run_no_network_dispatch(self, config_file: Path, coord_dir: Path) -> None:
        """Dry run should not call dispatch or post_briefing."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}) as gi, \
             patch("coord.dispatch.dispatch") as disp, \
             patch("coord.dispatch.post_briefing") as brief:
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0
        gi.assert_called_once()
        disp.assert_not_called()
        brief.assert_not_called()


class TestAssignDispatch:
    def test_successful_dispatch(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "abc-123"}) as disp, \
             patch("coord.github_ops.post_issue_comment") as post_comment, \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        assert "dispatched" in result.output
        assert "abc-123" in result.output

        # Verify dispatch was called with a Proposal
        disp.assert_called_once()
        proposal = disp.call_args[0][0]
        assert proposal.machine_name == "laptop"
        assert proposal.repo_name == "api"
        assert proposal.issue_number == 7
        assert proposal.issue_title == "Fix bug"

    def test_dispatched_is_recorded(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "rec-1"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 0

        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["assignment_id"] == "rec-1"
        assert records[0]["machine_name"] == "laptop"
        assert records[0]["repo_name"] == "api"
        assert records[0]["issue_number"] == 7

    def test_briefing_text_passed_through(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "b-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "5",
                    "--config", str(config_file),
                    "--briefing", "Focus on the auth module only",
                ],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.briefing == "Focus on the auth module only"

    def test_claim_check_blocks_duplicate(self, config_file: Path, coord_dir: Path) -> None:
        """If issue is already claimed, assign should refuse."""
        from coord.claim import Claim

        fake_claim = Claim(
            issue_number=7, repo_name="api", source="board",
            machine_name="server", assignment_id="old-1",
        )
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=fake_claim), \
             patch("coord.claim.claim_message", return_value="already assigned to server"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "skipping" in result.output

    def test_dispatch_http_error(self, config_file: Path, coord_dir: Path) -> None:
        import httpx

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch(
                 "coord.dispatch.dispatch",
                 side_effect=httpx.ConnectError("connection refused"),
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "dispatch failed" in result.output

    def test_issue_fetch_failure(self, config_file: Path, coord_dir: Path) -> None:
        with patch(
            "coord.github_ops.get_issue",
            side_effect=RuntimeError("gh issue view failed: not found"),
        ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "999", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "could not fetch issue" in result.output

    def test_briefing_post_failure_is_nonfatal(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """If briefing post fails, the assignment should still succeed."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "ok-1"}), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch(
                 "coord.github_ops.post_issue_comment",
                 side_effect=RuntimeError("rate limited"),
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "3", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        assert "dispatched" in result.output
        assert "briefing post failed" in result.output
