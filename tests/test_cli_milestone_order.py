"""Black-box tests for `coord milestone order` (#768 Phase 0 CLI glue).

Mocks `coord.github_ops` (no live `gh` calls) and `coord.board_service`
so the test drives the real Click command end to end: fetch tracking issue
-> parse work order -> resolve membership/terminal state -> print DAG +
ready frontier. Board reads go through `board_service.read_board()` (#615
thin-client seam), not `coord.state.load_board()` directly — see
tests/test_thin_client_board_audit.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Board


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
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


TRACKING_BODY = """\
Milestone plan.

## Work order
- [ ] #762  {group: A}
- [ ] #763  {group: A}
- [ ] #765  {after: #762,#763}
"""


def _get_issue(repo, number, *, milestone_number=9, states=None, bodies=None):
    states = states or {}
    bodies = bodies or {}
    if number == 100:
        return {
            "number": 100,
            "title": "tracking",
            "body": bodies.get(100, TRACKING_BODY),
            "state": "OPEN",
            "milestone": {"number": milestone_number, "title": "M"},
        }
    return {
        "number": number,
        "title": f"issue {number}",
        "body": bodies.get(number, ""),
        "state": states.get(number, "OPEN"),
        "milestone": {"number": milestone_number, "title": "M"},
    }


class TestMilestoneOrderCmd:
    def test_prints_dag_and_ready_frontier(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()):
            result = CliRunner().invoke(
                main,
                ["milestone", "order", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "#762" in result.output
        assert "#763" in result.output
        assert "#765" in result.output
        assert "Ready frontier:" in result.output
        # 765 depends on 762+763, neither terminal -> blocked, not ready.
        ready_section = result.output.split("Ready frontier:")[1]
        blocked_section = ready_section.split("Blocked:")[1] if "Blocked:" in ready_section else ""
        assert "#765" in blocked_section
        assert "waiting on #762, #763" in blocked_section

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "order", "nope", "100", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_tracking_issue_without_milestone_errors(self, config_file: Path) -> None:
        def get_issue_no_milestone(repo, number):
            return {"number": number, "title": "t", "body": TRACKING_BODY, "state": "OPEN", "milestone": None}

        with patch("coord.github_ops.get_issue", side_effect=get_issue_no_milestone):
            result = CliRunner().invoke(
                main,
                ["milestone", "order", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "no milestone" in result.output

    def test_no_work_order_block_reports_and_exits_zero(self, config_file: Path) -> None:
        def get_issue_no_block(repo, number):
            return {
                "number": number,
                "title": "t",
                "body": "just prose, no work order here",
                "state": "OPEN",
                "milestone": {"number": 9, "title": "M"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue_no_block):
            result = CliRunner().invoke(
                main,
                ["milestone", "order", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "no `## Work order` block found" in result.output

    def test_foreign_issue_in_work_order_errors(self, config_file: Path) -> None:
        body = "## Work order\n- [ ] #762\n- [ ] #999\n"

        def get_issue(repo, number):
            if number == 100:
                return {
                    "number": 100, "title": "tracking", "body": body,
                    "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                }
            if number == 999:
                # Belongs to a different milestone entirely.
                return {
                    "number": 999, "title": "foreign", "body": "",
                    "state": "OPEN", "milestone": {"number": 42, "title": "Other"},
                }
            return {
                "number": number, "title": "x", "body": "",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        open_issues = [{"number": 762, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues):
            result = CliRunner().invoke(
                main,
                ["milestone", "order", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "#999" in result.output
        assert "not an issue under this milestone" in result.output

    def test_cycle_in_work_order_errors(self, config_file: Path) -> None:
        body = "## Work order\n- [ ] #1 {after: #2}\n- [ ] #2 {after: #1}\n"

        def get_issue(repo, number):
            return {
                "number": number, "title": "tracking", "body": body,
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue):
            result = CliRunner().invoke(
                main,
                ["milestone", "order", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "cycle" in result.output
