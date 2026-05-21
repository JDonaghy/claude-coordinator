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

# Config with pipeline.labels defined to test label→gate resolution.
CONFIG_YAML_WITH_PIPELINE = """\
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
pipeline:
  default_gates: [review, merge]
  labels:
    documentation: [merge]
    hotfix: [merge]
    needs-smoke: [review, smoke, merge]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    """Provide an isolated in-memory DB for state and return a temp dir for logs."""
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
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

    def test_force_deletes_stale_remote_branches(self, config_file: Path, coord_dir: Path) -> None:
        """--force should delete existing remote branches before dispatching."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-1"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim._default_branch_lookup", return_value=["issue-7-old-attempt"]) as lookup, \
             patch("coord.github_ops.delete_remote_branch", return_value=True) as delete_br:
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file), "--force"],
            )
        assert result.exit_code == 0
        assert "deleted stale remote branch: issue-7-old-attempt" in result.output
        delete_br.assert_called_once_with("acme/api", "issue-7-old-attempt")
        assert "dispatched" in result.output

    def test_force_no_stale_branches(self, config_file: Path, coord_dir: Path) -> None:
        """--force with no stale branches should dispatch normally."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-2"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim._default_branch_lookup", return_value=[]):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file), "--force"],
            )
        assert result.exit_code == 0
        assert "deleted stale" not in result.output
        assert "dispatched" in result.output

    def test_force_delete_failure_warns_but_continues(self, config_file: Path, coord_dir: Path) -> None:
        """If branch deletion fails, warn but still dispatch."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-3"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim._default_branch_lookup", return_value=["issue-7-stuck"]), \
             patch("coord.github_ops.delete_remote_branch", return_value=False):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file), "--force"],
            )
        assert result.exit_code == 0
        assert "warning: failed to delete remote branch: issue-7-stuck" in result.output
        assert "dispatched" in result.output

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


class TestAssignLabelGateResolution:
    """Tests for label→required_gates resolution in coord assign (cli.py:1200-1207)."""

    @pytest.fixture
    def pipeline_config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(CONFIG_YAML_WITH_PIPELINE)
        return p

    def test_documentation_label_resolves_to_merge_only(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        """Issue with 'documentation' label → required_gates=["merge"] (skip review)."""
        issue_payload = {
            "title": "Update docs",
            "body": "Documentation update",
            "labels": [{"name": "documentation"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "doc-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "20", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["merge"]

    def test_hotfix_label_resolves_to_merge_only(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        issue_payload = {
            "title": "Hotfix auth",
            "body": "",
            "labels": [{"name": "hotfix"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "hf-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "21", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["merge"]

    def test_needs_smoke_label_resolves_to_full_pipeline(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        issue_payload = {
            "title": "Big feature",
            "body": "",
            "labels": [{"name": "needs-smoke"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "ns-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "22", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["review", "smoke", "merge"]

    def test_unrecognized_label_falls_back_to_default_gates(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        """Labels not in pipeline.labels fall back to pipeline.default_gates."""
        issue_payload = {
            "title": "Fix bug",
            "body": "",
            "labels": [{"name": "bug"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "bug-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "23", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        # No matching label → default_gates from config
        assert proposal.required_gates == ["review", "merge"]

    def test_no_labels_falls_back_to_default_gates(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        """Issue with no labels falls back to pipeline.default_gates."""
        issue_payload = {"title": "Unlabeled issue", "body": "", "labels": []}
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "nolabel-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "24", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["review", "merge"]
