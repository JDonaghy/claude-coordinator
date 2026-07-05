"""CLI tests for `coord milestone gate-c` (#932, docs/ORACLE_LOOP.md).

Gate C is the milestone-end check: the FULL accumulated acceptance suite
must be green before a milestone ships. No `feature/ms-NN -> develop` git
model exists yet (#933/#934 deferred), so this is a standalone, manual
check command — it runs the repo's acceptance driver once and reports
pass/fail, plus a per-issue rollup of the milestone's own Acceptance box
state, without mutating any git state.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from coord import state
from coord.cli import main
from coord.models import Board, Proposal


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
      run: {run_cmd}
"""

TRACKING_BODY = """\
Milestone plan.

## Work order
- [ ] #762
- [ ] #763
"""


def _write_config(tmp_path: Path, *, repo_path: str, run_cmd: str) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML.format(repo_path=repo_path, run_cmd=json.dumps(run_cmd)))
    return p


def _get_issue(repo, number, *, milestone_number=9):
    if number == 100:
        return {
            "number": 100, "title": "tracking", "body": TRACKING_BODY,
            "state": "OPEN", "milestone": {"number": milestone_number, "title": "M"},
        }
    return {
        "number": number, "title": f"issue {number}", "body": "",
        "state": "OPEN", "milestone": {"number": milestone_number, "title": "M"},
        "labels": [],
    }


def _seed_work_assignment(assignment_id: str, issue_number: int, acceptance_state: str | None) -> None:
    state.record_dispatched(
        assignment_id=assignment_id,
        proposal=Proposal(
            id=1, machine_name="laptop", repo_name="coord-tui",
            issue_number=issue_number, issue_title="t", rationale="",
        ),
        repo_github="acme/coord-tui",
    )
    if acceptance_state is not None:
        state.record_acceptance_verdict(
            assignment_id=assignment_id,
            acceptance_state=acceptance_state,
            acceptance_total=5,
            acceptance_passed=5 if acceptance_state == "passed" else 3,
        )


class TestMilestoneGateC:
    def test_unknown_repo_errors(self, tmp_path: Path) -> None:
        config_path = _write_config(tmp_path, repo_path=str(tmp_path), run_cmd="echo '{}'")
        result = CliRunner().invoke(
            main, ["milestone", "gate-c", "nope", "100", "--config", str(config_path)]
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_no_driver_configured_errors(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: coord-tui\n    github: acme/coord-tui\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n"
            "    repos: [coord-tui]\n    repo_paths:\n      coord-tui: /tmp/x\n"
        )
        with patch("coord.github_ops.get_issue", side_effect=_get_issue):
            result = CliRunner().invoke(
                main, ["milestone", "gate-c", "coord-tui", "100", "--config", str(p)]
            )
        assert result.exit_code == 1
        assert "no acceptance driver configured" in result.output

    def test_full_suite_green_reports_gate_c_green_and_rollup(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::b", "status": "pass"},
        ]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        _seed_work_assignment("w762", 762, "passed")
        _seed_work_assignment("w763", 763, "failed")

        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=[]):
            result = CliRunner().invoke(main, [
                "milestone", "gate-c", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 0, result.output
        assert "Gate C GREEN" in result.output
        assert "Milestone Acceptance boxes: 1/2 passed" in result.output

    def test_full_suite_red_reports_gate_c_red_and_exits_nonzero(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::b", "status": "fail", "message": "boom"},
        ]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.board_service.read_board", return_value=Board()):
            result = CliRunner().invoke(main, [
                "milestone", "gate-c", "coord-tui", "100", "--config", str(config_path),
            ])
        assert result.exit_code == 1, result.output
        assert "Gate C RED" in result.output
