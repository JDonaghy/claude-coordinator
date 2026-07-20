"""CLI tests for `coord milestone gate-b` (#933, docs/PIPELINE_V2.md).

Gate B dispatches an independent architecture review of the *assembled*
milestone against its Gate-A contract, once every issue in the milestone has
landed. No `feature/ms-NN -> develop` git model exists yet (#934 deferred),
so — like `coord milestone gate-c` — this is a manual, non-automated command:
it dispatches the review and reports where it went; the request-changes vs.
approve verdict itself is posted later, to the tracking issue, when the
reviewer's session ends (via `coord.notify`'s existing, unmodified
REVIEW_VERDICT parsing).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from coord.cli import main


CONFIG_YAML = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
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
      run: echo '{{}}'
"""

TRACKING_BODY = """\
Milestone plan.

## Work order
- [ ] #930
- [ ] #931
"""


def _write_config(tmp_path: Path, *, repo_path: str) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML.format(repo_path=repo_path))
    return p


def _make_get_issue(*, milestone_number: int = 17, closed_numbers=frozenset()):
    """Build a `github_ops.get_issue` fake. #100 is always the tracking
    issue; any other number is a milestone member, CLOSED iff it's in
    *closed_numbers* — this is what `is_milestone_complete` reads for any
    node NOT already present in the bulk `get_open_issues` fake below."""

    def _get_issue(repo: str, number: int) -> dict:
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

    return _get_issue


def _open_issues(milestone_number: int = 17, numbers=(930, 931)) -> list[dict]:
    """Bulk `get_open_issues` fake: the milestone's still-open issues."""
    return [
        {"number": n, "title": f"issue {n}", "milestone": {"number": milestone_number}}
        for n in numbers
    ]


class TestMilestoneGateB:
    def test_unknown_repo_errors(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        result = CliRunner().invoke(
            main, ["milestone", "gate-b", "nope", "100", "--config", str(config_path)]
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_gate_a_not_satisfied_errors(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        with patch("coord.github_ops.get_issue", side_effect=_make_get_issue()), \
             patch("coord.github_ops.get_open_issues", return_value=_open_issues()), \
             patch("coord.github_ops.get_repo_file", side_effect=RuntimeError("404")):
            result = CliRunner().invoke(main, [
                "milestone", "gate-b", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "Gate A not satisfied" in result.output

    def test_milestone_incomplete_errors(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        # #930 is closed (terminal, absent from the bulk open-issues fake so
        # fetch_milestone_context falls back to the individual-fetch path
        # below) but #931 is still open — Gate B must refuse until every
        # issue in the milestone has landed.
        with patch("coord.github_ops.get_issue", side_effect=_make_get_issue(closed_numbers={930})), \
             patch("coord.github_ops.get_open_issues", return_value=_open_issues(numbers=(931,))), \
             patch("coord.github_ops.get_repo_file", return_value="# contract"):
            result = CliRunner().invoke(main, [
                "milestone", "gate-b", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "still open" in result.output
        assert "#931" in result.output

    def test_no_idle_machine_errors(self, tmp_path: Path, coord_db) -> None:
        from coord.models import Assignment, Board

        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        busy_board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="coord-tui", issue_number=1,
                issue_title="busy", status="running",
            )
        ])
        with patch("coord.github_ops.get_issue", side_effect=_make_get_issue(closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=_open_issues(numbers=())), \
             patch("coord.github_ops.get_repo_file", return_value="# contract"), \
             patch("coord.board_service.read_board", return_value=busy_board):
            result = CliRunner().invoke(main, [
                "milestone", "gate-b", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1
        assert "no idle" in result.output

    def test_dry_run_does_not_dispatch(self, tmp_path: Path, coord_db) -> None:
        from coord.models import Board

        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        with patch("coord.github_ops.get_issue", side_effect=_make_get_issue(closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=_open_issues(numbers=())), \
             patch("coord.github_ops.get_repo_file", return_value="# contract"), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.gate_b.dispatch_gate_b_review") as mock_dispatch:
            result = CliRunner().invoke(main, [
                "milestone", "gate-b", "coord-tui", "100", "--dry-run",
                "--config", str(config_path),
            ])
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output
        assert "laptop" in result.output
        mock_dispatch.assert_not_called()

    def test_happy_path_dispatches_review(self, tmp_path: Path, coord_db) -> None:
        from coord.models import Assignment, Board

        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        fake_assignment = Assignment(
            machine_name="laptop", repo_name="coord-tui", issue_number=100,
            issue_title="[gate-b] Epic: milestone", status="running",
            assignment_id="gb-1", type="review", review_target="gate-b-ms-17",
        )
        with patch("coord.github_ops.get_issue", side_effect=_make_get_issue(closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=_open_issues(numbers=())), \
             patch("coord.github_ops.get_repo_file", return_value="# contract"), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.gate_b.dispatch_gate_b_review", return_value=fake_assignment) as mock_dispatch:
            result = CliRunner().invoke(main, [
                "milestone", "gate-b", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 0, result.output
        assert "gb-1" in result.output
        assert mock_dispatch.call_count == 1
        _, kwargs = mock_dispatch.call_args
        assert kwargs["tracking_issue"] == 100
        assert kwargs["milestone_number"] == 17

    def test_explicit_machine_override(self, tmp_path: Path, coord_db) -> None:
        from coord.models import Assignment, Board

        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        fake_assignment = Assignment(
            machine_name="laptop", repo_name="coord-tui", issue_number=100,
            issue_title="[gate-b] Epic: milestone", status="running",
            assignment_id="gb-2", type="review", review_target="gate-b-ms-17",
        )
        with patch("coord.github_ops.get_issue", side_effect=_make_get_issue(closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=_open_issues(numbers=())), \
             patch("coord.github_ops.get_repo_file", return_value="# contract"), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.gate_b.dispatch_gate_b_review", return_value=fake_assignment) as mock_dispatch:
            result = CliRunner().invoke(main, [
                "milestone", "gate-b", "coord-tui", "100", "--machine", "laptop",
                "--config", str(config_path),
            ])
        assert result.exit_code == 0, result.output
        _, kwargs = mock_dispatch.call_args
        assert kwargs["machine"].name == "laptop"

    def test_unknown_explicit_machine_errors(self, tmp_path: Path, coord_db) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path))
        with patch("coord.github_ops.get_issue", side_effect=_make_get_issue(closed_numbers={930, 931})), \
             patch("coord.github_ops.get_open_issues", return_value=_open_issues(numbers=())), \
             patch("coord.github_ops.get_repo_file", return_value="# contract"):
            result = CliRunner().invoke(main, [
                "milestone", "gate-b", "coord-tui", "100", "--machine", "nope",
                "--config", str(config_path),
            ])
        assert result.exit_code == 2
        assert "unknown machine" in result.output
