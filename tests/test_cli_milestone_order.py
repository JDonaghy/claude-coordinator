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


# ── `coord milestone write-order` (#770 Phase 2 write path) ────────────────


class TestMilestoneWriteOrderCmd:
    def test_writes_valid_block_via_stdin(self, config_file: Path) -> None:
        tracking_body = "Milestone plan.\n\n## Refs\nsome refs\n"

        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": tracking_body,
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
        ]
        new_block = "- [ ] #762  {group: A}\n- [ ] #763  {after: #762}\n"
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input=new_block,
            )
        assert result.exit_code == 0, result.output
        assert "wrote `## Work order` block (2 node(s))" in result.output
        mock_update.assert_called_once()
        call_repo, call_issue, call_body = mock_update.call_args[0]
        assert call_repo == "acme/api"
        assert call_issue == 100
        assert "Milestone plan." in call_body
        assert "## Refs\nsome refs" in call_body
        assert "#762" in call_body and "#763" in call_body

    def test_writes_from_file_option(self, config_file: Path, tmp_path: Path) -> None:
        block_file = tmp_path / "order.txt"
        block_file.write_text("- [ ] #762\n")

        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": "",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        open_issues = [{"number": 762, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "write-order", "api", "100",
                    "--file", str(block_file), "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_update.assert_called_once()

    def test_idempotent_rewrite_is_a_noop(self, config_file: Path) -> None:
        tracking_body = "## Work order\n- [ ] #762  {group: A}\n"

        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": tracking_body,
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        open_issues = [{"number": 762, "milestone": {"number": 9}}]
        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #762  {group: A}\n",
            )
        assert result.exit_code == 0, result.output
        assert "unchanged (idempotent no-op)" in result.output
        mock_update.assert_not_called()

    def test_refuses_to_write_a_cycle(self, config_file: Path) -> None:
        def get_issue(repo, number):
            return {
                "number": 100, "title": "tracking", "body": "",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #1 {after: #2}\n- [ ] #2 {after: #1}\n",
            )
        assert result.exit_code == 1
        assert "cycle" in result.output
        mock_update.assert_not_called()

    def test_refuses_foreign_issue(self, config_file: Path) -> None:
        def get_issue(repo, number):
            if number == 100:
                return {
                    "number": 100, "title": "tracking", "body": "",
                    "state": "OPEN", "milestone": {"number": 9, "title": "M"},
                }
            return {
                "number": number, "title": "x", "body": "",
                "state": "OPEN", "milestone": {"number": 42, "title": "Other"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #999\n",
            )
        assert result.exit_code == 1
        assert "not an issue under this milestone" in result.output
        mock_update.assert_not_called()

    def test_refuses_empty_block(self, config_file: Path) -> None:
        with patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="",
            )
        assert result.exit_code == 2
        assert "no work-order content" in result.output
        mock_update.assert_not_called()

    def test_tracking_issue_without_milestone_errors(self, config_file: Path) -> None:
        def get_issue_no_milestone(repo, number):
            return {"number": number, "title": "t", "body": "", "state": "OPEN", "milestone": None}

        with patch("coord.github_ops.get_issue", side_effect=get_issue_no_milestone), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "write-order", "api", "100", "--config", str(config_file)],
                input="- [ ] #1\n",
            )
        assert result.exit_code == 1
        assert "no milestone" in result.output
        mock_update.assert_not_called()

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "write-order", "nope", "100", "--config", str(config_file)],
            input="- [ ] #1\n",
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output


# ── `coord milestone chat` (#770 Phase 2 dispatch) ──────────────────────────


class TestMilestoneChatCmd:
    def test_dispatches_and_prints_assignment_id(self, config_file: Path) -> None:
        with patch(
            "coord.milestone_chat.dispatch_milestone_chat",
            return_value=("asg123", "laptop"),
        ) as mock_dispatch:
            result = CliRunner().invoke(
                main,
                ["milestone", "chat", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert result.output.strip().splitlines()[-1] == "asg123"
        mock_dispatch.assert_called_once_with(
            "api", 100, mock_dispatch.call_args[0][2],
            machine_override=None, add_child_issue=None,
        )

    def test_dispatch_failure_reports_error(self, config_file: Path) -> None:
        with patch(
            "coord.milestone_chat.dispatch_milestone_chat",
            side_effect=RuntimeError("no machine claims repo 'api'"),
        ):
            result = CliRunner().invoke(
                main,
                ["milestone", "chat", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "no machine claims repo" in result.output

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "chat", "nope", "100", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output

    def test_missing_tracking_issue_without_new_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "chat", "api", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "TRACKING_ISSUE is required" in result.output

    def test_new_and_tracking_issue_together_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "chat", "api", "100", "--new", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "not both" in result.output

    def test_seed_options_without_new_error(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "chat", "api", "100", "--title", "Foo", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "only apply with --new" in result.output


# ── `coord milestone add-child` (#1008: epic-child splice helper) ──────────


class TestMilestoneAddChildCmd:
    def test_adds_first_child_creating_section(self, config_file: Path) -> None:
        def get_issue(repo, number):
            if number == 100:
                return {
                    "number": 100, "title": "epic",
                    "body": "Epic intro.\n", "state": "OPEN",
                }
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "1050", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "#1050 added to #100's" in result.output
        mock_update.assert_called_once()
        call_repo, call_issue, call_body = mock_update.call_args[0]
        assert call_repo == "acme/api"
        assert call_issue == 100
        assert "Epic intro." in call_body
        assert "## Sub-issues" in call_body
        assert "#1050" in call_body

    def test_adds_with_group_and_after_annotations(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [ ] #1050\n"

        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "add-child", "api", "100", "1051",
                    "--group", "B", "--after", "1050",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_update.assert_called_once()
        _repo, _issue, call_body = mock_update.call_args[0]
        assert "#1050" in call_body and "#1051" in call_body
        from coord.milestone_order import parse_sub_issues
        wo = parse_sub_issues(call_body)
        assert wo.node(1051).group == "B"
        assert wo.node(1051).after == (1050,)

    def test_idempotent_readd_is_a_noop(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [ ] #1050  {group: A}\n"

        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "add-child", "api", "100", "1050",
                    "--group", "A", "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "unchanged (idempotent no-op)" in result.output or "already in" in result.output
        mock_update.assert_not_called()

    def test_readd_with_different_annotations_updates_in_place(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [x] #1050  {group: A}\n"

        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "add-child", "api", "100", "1050",
                    "--group", "B", "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_update.assert_called_once()
        _repo, _issue, call_body = mock_update.call_args[0]
        from coord.milestone_order import parse_sub_issues
        wo = parse_sub_issues(call_body)
        # Annotation updated, but the existing checked state is preserved.
        assert wo.node(1050).group == "B"
        assert wo.node(1050).checked is True

    def test_remove_drops_existing_child(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [ ] #1050\n- [ ] #1051\n"

        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "1050", "--remove", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "removed from" in result.output
        mock_update.assert_called_once()
        _repo, _issue, call_body = mock_update.call_args[0]
        from coord.milestone_order import parse_sub_issues
        assert parse_sub_issues(call_body).issue_numbers == (1051,)

    def test_remove_absent_child_is_a_noop(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [ ] #1050\n"

        def get_issue(repo, number):
            return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "9999", "--remove", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert "no-op" in result.output
        mock_update.assert_not_called()

    def test_remove_rejects_group_and_after(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "milestone", "add-child", "api", "100", "1050",
                "--remove", "--group", "A", "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2
        assert "cannot be combined" in result.output

    def test_leaves_existing_work_order_section_untouched(self, config_file: Path) -> None:
        tracking_body = (
            "## Work order\n- [ ] #762  {group: A}\n\n"
            "## Sub-issues\n- [ ] #1050\n"
        )

        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "1051", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        _repo, _issue, call_body = mock_update.call_args[0]
        from coord.milestone_order import parse_sub_issues, parse_work_order
        assert parse_work_order(call_body).issue_numbers == (762,)
        assert parse_sub_issues(call_body).issue_numbers == (1050, 1051)

    def test_invalid_after_value_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "milestone", "add-child", "api", "100", "1050",
                "--after", "not-a-number", "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2
        assert "--after must be a comma-separated list" in result.output

    def test_refuses_to_write_a_cycle(self, config_file: Path) -> None:
        tracking_body = "## Sub-issues\n- [ ] #1050  {after: #1051}\n- [ ] #1051\n"

        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": tracking_body, "state": "OPEN"}
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "add-child", "api", "100", "1051",
                    "--after", "1050", "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "cycle" in result.output
        mock_update.assert_not_called()

    def test_unknown_child_issue_errors(self, config_file: Path) -> None:
        def get_issue(repo, number):
            if number == 100:
                return {"number": 100, "title": "epic", "body": "", "state": "OPEN"}
            raise RuntimeError("gh issue view 9999 failed: no such issue")

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "9999", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "could not fetch issue #9999" in result.output
        mock_update.assert_not_called()

    def test_unknown_epic_errors(self, config_file: Path) -> None:
        with patch(
            "coord.github_ops.get_issue",
            side_effect=RuntimeError("gh issue view 100 failed: no such issue"),
        ), patch("coord.github_ops.update_issue_body") as mock_update:
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "100", "1050", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "could not fetch epic #100" in result.output
        mock_update.assert_not_called()

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "add-child", "nope", "100", "1050", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output


# ── `coord milestone chat --new` (#1009 brand-new-milestone dispatch) ───────


class TestMilestoneChatNewCmd:
    def test_dispatches_and_prints_assignment_id(self, config_file: Path) -> None:
        with patch(
            "coord.milestone_chat.dispatch_new_milestone_chat",
            return_value=("asg456", "laptop"),
        ) as mock_dispatch:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "chat", "api", "--new",
                    "--title", "Q4 push", "--seed", "ship the widget",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert result.output.strip().splitlines()[-1] == "asg456"
        mock_dispatch.assert_called_once_with(
            "api",
            mock_dispatch.call_args[0][1],
            seed_title="Q4 push",
            seed_prompt="ship the widget",
            machine_override=None,
        )

    def test_dispatch_failure_reports_error(self, config_file: Path) -> None:
        with patch(
            "coord.milestone_chat.dispatch_new_milestone_chat",
            side_effect=RuntimeError("no machine claims repo 'api'"),
        ):
            result = CliRunner().invoke(
                main,
                ["milestone", "chat", "api", "--new", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "no machine claims repo" in result.output

    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "chat", "nope", "--new", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output
