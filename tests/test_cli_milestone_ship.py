"""CLI tests for `coord milestone ship` (#934, docs/PIPELINE_V2.md, Gate D).

Ship is the last step of the develop + feature-branch-per-milestone git
model: merge `feature/ms-NN` into `develop_branch`, but only when both Gate B
(an `approve` verdict) and Gate C (the full acceptance suite, re-run live
here) are green.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from coord.cli import main
from coord.gate_b import review_target_for
from coord.models import Assignment, Board


CONFIG_YAML = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
    default_branch: main
    {develop_line}
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
    repo_paths:
      coord-tui: {repo_path}
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: {run_cmd}
"""

TRACKING_BODY = """\
Milestone plan.

## Work order
- [ ] #930
- [ ] #931
"""


def _write_config(
    tmp_path: Path, *, repo_path: str, run_cmd: str = "echo '{}'", opted_in: bool = True,
) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML.format(
        repo_path=repo_path,
        run_cmd=json.dumps(run_cmd),
        develop_line="develop_branch: develop" if opted_in else "",
    ))
    return p


def _get_issue(repo, number, *, milestone_number=17, closed_numbers=frozenset()):
    if number == 100:
        return {
            "number": 100, "title": "Epic: milestone", "body": TRACKING_BODY,
            "state": "OPEN", "milestone": {"number": milestone_number, "title": "M"},
        }
    return {
        "number": number, "title": f"issue {number}", "body": "",
        "state": "CLOSED" if number in closed_numbers else "OPEN",
        "milestone": {"number": milestone_number, "title": "M"},
        "labels": [],
    }


def _gate_b_review(*, verdict: str | None, milestone_number: int = 17) -> Assignment:
    return Assignment(
        machine_name="laptop", repo_name="coord-tui", issue_number=100,
        issue_title="[gate-b] tracking", assignment_id="gb-1", type="review",
        status="done", review_target=review_target_for(milestone_number),
        review_verdict=verdict,
    )


_GREEN_BLOB = json.dumps({"tests": [{"id": "ms17::a", "status": "pass"}]})
_RED_BLOB = json.dumps({"tests": [{"id": "ms17::a", "status": "fail", "message": "boom"}]})


class TestMilestoneShip:
    def test_unknown_repo_errors(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        result = CliRunner().invoke(
            main, ["milestone", "ship", "nope", "100", "--config", str(config_path)]
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_repo_not_opted_in_errors(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path), opted_in=False)
        result = CliRunner().invoke(
            main, ["milestone", "ship", "coord-tui", "100", "--config", str(config_path)]
        )
        assert result.exit_code == 1
        assert "has not opted into" in result.output

    def test_milestone_incomplete_errors(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        with patch("coord.github_ops.get_issue", side_effect=lambda r, n: _get_issue(r, n, closed_numbers={930})), \
             patch("coord.github_ops.get_open_issues", return_value=[
                 {"number": 931, "title": "issue 931", "milestone": {"number": 17}},
             ]):
            result = CliRunner().invoke(main, [
                "milestone", "ship", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "still open" in result.output
        assert "#931" in result.output

    def test_no_gate_b_review_errors(self, tmp_path: Path, coord_db) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        with patch("coord.github_ops.get_issue", side_effect=lambda r, n: _get_issue(r, n, closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=Board()):
            result = CliRunner().invoke(main, [
                "milestone", "ship", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "Gate B is not green" in result.output
        assert "no Gate B review found" in result.output

    def test_gate_b_request_changes_errors(self, tmp_path: Path, coord_db) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        board = Board(completed=[_gate_b_review(verdict="request-changes")])
        with patch("coord.github_ops.get_issue", side_effect=lambda r, n: _get_issue(r, n, closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=board):
            result = CliRunner().invoke(main, [
                "milestone", "ship", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "Gate B is not green (request-changes)" in result.output

    def test_gate_c_red_errors(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{_RED_BLOB}'")
        board = Board(completed=[_gate_b_review(verdict="approve")])
        with patch("coord.github_ops.get_issue", side_effect=lambda r, n: _get_issue(r, n, closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=board):
            result = CliRunner().invoke(main, [
                "milestone", "ship", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "Gate B: approve" in result.output
        assert "Gate C is RED" in result.output

    def test_dry_run_reports_plan_without_shipping(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{_GREEN_BLOB}'")
        board = Board(completed=[_gate_b_review(verdict="approve")])
        with patch("coord.github_ops.get_issue", side_effect=lambda r, n: _get_issue(r, n, closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=board), \
             patch("coord.github_ops.branch_exists_on_remote") as mock_exists, \
             patch("coord.github_ops.create_pr") as mock_create_pr:
            result = CliRunner().invoke(main, [
                "milestone", "ship", "coord-tui", "100", "--dry-run",
                "--config", str(config_path),
            ])
        assert result.exit_code == 0, result.output
        assert "Gate B: approve" in result.output
        assert "Gate C: GREEN" in result.output
        assert "dry run" in result.output
        assert "feature/ms-17 -> develop" in result.output
        mock_exists.assert_not_called()
        mock_create_pr.assert_not_called()

    def test_feature_branch_missing_errors(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{_GREEN_BLOB}'")
        board = Board(completed=[_gate_b_review(verdict="approve")])
        with patch("coord.github_ops.get_issue", side_effect=lambda r, n: _get_issue(r, n, closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=board), \
             patch("coord.github_ops.branch_exists_on_remote", return_value=False):
            result = CliRunner().invoke(main, [
                "milestone", "ship", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "does not exist on" in result.output

    def test_happy_path_ships(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{_GREEN_BLOB}'")
        board = Board(completed=[_gate_b_review(verdict="approve")])
        with patch("coord.github_ops.get_issue", side_effect=lambda r, n: _get_issue(r, n, closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=board), \
             patch("coord.github_ops.branch_exists_on_remote", return_value=True), \
             patch("coord.github_ops.create_pr", return_value={
                 "number": 55, "url": "https://github.com/acme/coord-tui/pull/55", "existed": False,
             }) as mock_create_pr, \
             patch("coord.github_ops.merge_pr", return_value=(True, "merged")) as mock_merge_pr:
            result = CliRunner().invoke(main, [
                "milestone", "ship", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 0, result.output
        assert "shipped: feature/ms-17 -> develop" in result.output
        assert "#55" in result.output

        mock_create_pr.assert_called_once()
        _, kwargs = mock_create_pr.call_args
        assert kwargs["base"] == "develop"
        assert kwargs["head"] == "feature/ms-17"

        mock_merge_pr.assert_called_once_with("acme/coord-tui", 55, method="merge")

    def test_merge_method_option_is_threaded(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{_GREEN_BLOB}'")
        board = Board(completed=[_gate_b_review(verdict="approve")])
        with patch("coord.github_ops.get_issue", side_effect=lambda r, n: _get_issue(r, n, closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=board), \
             patch("coord.github_ops.branch_exists_on_remote", return_value=True), \
             patch("coord.github_ops.create_pr", return_value={
                 "number": 55, "url": "https://github.com/acme/coord-tui/pull/55", "existed": False,
             }), \
             patch("coord.github_ops.merge_pr", return_value=(True, "merged")) as mock_merge_pr:
            result = CliRunner().invoke(main, [
                "milestone", "ship", "coord-tui", "100", "--method", "squash",
                "--config", str(config_path),
            ])
        assert result.exit_code == 0, result.output
        mock_merge_pr.assert_called_once_with("acme/coord-tui", 55, method="squash")

    def test_merge_failure_reports_pr_and_errors(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{_GREEN_BLOB}'")
        board = Board(completed=[_gate_b_review(verdict="approve")])
        with patch("coord.github_ops.get_issue", side_effect=lambda r, n: _get_issue(r, n, closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=board), \
             patch("coord.github_ops.branch_exists_on_remote", return_value=True), \
             patch("coord.github_ops.create_pr", return_value={
                 "number": 55, "url": "https://github.com/acme/coord-tui/pull/55", "existed": False,
             }), \
             patch("coord.github_ops.merge_pr", return_value=(False, "conflict")):
            result = CliRunner().invoke(main, [
                "milestone", "ship", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "#55" in result.output
        assert "conflict" in result.output
