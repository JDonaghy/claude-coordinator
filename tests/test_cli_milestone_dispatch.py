"""Black-box tests for `coord milestone dispatch` (#769 Phase 1).

Mocks `coord.github_ops` (no live `gh` calls) and `coord.dispatch.dispatch`
(no live HTTP POST to an agent) so the test drives the real Click command
end to end: fetch tracking issue -> parse work order -> resolve membership/
terminal state -> compute ready frontier -> pick machines -> (maybe)
dispatch. Mirrors tests/test_cli_milestone_order.py's fixture/mock style.

Includes the #769 acceptance-criteria scenario verbatim: a `group: A`
cohort + an `after`-gated node -> dry-run shows the cohort dispatching now
and the gated node waiting; simulating the cohort merging -> the gated node
enters the frontier.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import state as state_mod
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
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


CONFIG_YAML_WITH_ACCEPTANCE_DRIVER = CONFIG_YAML + """\
acceptance:
  drivers:
    api:
      kind: tui-tuidriver
      run: "cargo test"
      mock: "*.screen"
"""


@pytest.fixture
def config_file_with_gate_a(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_WITH_ACCEPTANCE_DRIVER)
    return p


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


def _get_issue(repo, number, *, milestone_number=9, closed=frozenset(), bodies=None):
    bodies = bodies or {}
    if number == 100:
        return {
            "number": 100, "title": "tracking", "body": bodies.get(100, TRACKING_BODY),
            "state": "OPEN", "milestone": {"number": milestone_number, "title": "M"},
        }
    return {
        "number": number, "title": f"issue {number}", "body": bodies.get(number, ""),
        "state": "CLOSED" if number in closed else "OPEN",
        "milestone": {"number": milestone_number, "title": "M"},
        "labels": [],
    }


class TestMilestoneDispatchValidation:
    def test_unknown_repo_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main, ["milestone", "dispatch", "nope", "100", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "unknown repo" in result.output


class TestMilestoneDispatchDryRun:
    def test_cohort_dispatches_now_and_gated_node_waits(self, config_file: Path) -> None:
        """#769 acceptance criteria (first half)."""
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()

        will_dispatch = result.output.split("Will dispatch now:")[1].split("Waiting:")[0]
        assert "#762" in will_dispatch
        assert "#763" in will_dispatch
        waiting_section = result.output.split("Waiting:")[1]
        assert "#765" in waiting_section
        assert "waiting on #762, #763" in waiting_section
        assert "dry run" in result.output

    def test_cohort_merging_unblocks_gated_node(self, config_file: Path) -> None:
        """#769 acceptance criteria (second half): simulate #762/#763 merged ->
        #765 enters the ready frontier."""
        open_issues = [{"number": 765, "milestone": {"number": 9}}]

        def get_issue(repo, number):
            return _get_issue(repo, number, closed=frozenset({762, 763}))

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()

        will_dispatch = result.output.split("Will dispatch now:")[1]
        assert "#765" in will_dispatch
        assert "Waiting:" not in result.output

    def test_no_work_order_block_reports_and_exits_zero(self, config_file: Path) -> None:
        def get_issue_no_block(repo, number):
            return {
                "number": number, "title": "t", "body": "just prose",
                "state": "OPEN", "milestone": {"number": 9, "title": "M"},
            }

        with patch("coord.github_ops.get_issue", side_effect=get_issue_no_block):
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "no `## Work order` block found" in result.output


class TestMilestoneDispatchBulk:
    def test_dispatches_ready_frontier_and_registers_drain(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch", side_effect=[{"id": "a1"}, {"id": "a2"}]) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        assert disp.call_count == 2
        assert "a1" in result.output
        assert "a2" in result.output

        # Both cohort issues fanned out to distinct machines.
        machine_names = {c.args[0].machine_name for c in disp.call_args_list}
        assert machine_names == {"laptop", "server"}

        records = state_mod.load_dispatched()
        assert len(records) == 2

        # #765 still open (not terminal) -> the milestone registers for
        # daemon auto-drain.
        drains = state_mod.list_milestone_drains()
        assert drains == [{"repo_name": "api", "tracking_issue": 100}]
        assert "registered for daemon auto-drain" in result.output

    def test_already_complete_milestone_dispatches_nothing_and_does_not_register(
        self, config_file: Path
    ) -> None:
        """Every node already terminal at fetch time -> empty ready frontier,
        nothing dispatched, no daemon auto-drain registration needed."""
        body = "## Work order\n- [ ] #762\n"
        open_issues: list[dict] = []

        def get_issue(repo, number):
            return _get_issue(repo, number, bodies={100: body}, closed=frozenset({762}))

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()
        assert "Nothing to dispatch right now." in result.output
        assert state_mod.list_milestone_drains() == []


class TestMilestoneDispatchNext:
    def test_next_lists_choices_and_dispatches_the_pick(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch", return_value={"id": "picked-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file), "--next"],
                input="2\n",
            )
        assert result.exit_code == 0, result.output
        assert "pick one" in result.output.lower()
        disp.assert_called_once()
        assert "picked-1" in result.output

        # --next never registers for daemon auto-drain — the lighter,
        # fully-manual single-pick path.
        assert state_mod.list_milestone_drains() == []

    def test_next_dry_run_lists_without_dispatching(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file),
                 "--next", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()
        assert "#762" in result.output
        assert "#763" in result.output

    def test_pick_dispatches_without_prompt(self, config_file: Path) -> None:
        """#1003: --pick is the non-interactive companion to --next — the
        coord-tui "Dispatch next…" action's backend, which has no TTY to
        answer `click.prompt`."""
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch", return_value={"id": "picked-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "dispatch", "api", "100", "--config", str(config_file),
                    "--next", "--pick", "763",
                ],
                # No stdin — a prompt here would hang/abort the test.
                input="",
            )
        assert result.exit_code == 0, result.output
        disp.assert_called_once()
        assert "picked-1" in result.output
        assert state_mod.list_milestone_drains() == []

    def test_pick_dry_run_previews_without_dispatching(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "dispatch", "api", "100", "--config", str(config_file),
                    "--next", "--pick", "762", "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()
        assert "#762" in result.output
        assert "dry run" in result.output.lower()

    def test_pick_issue_not_in_ready_frontier_errors(self, config_file: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "dispatch", "api", "100", "--config", str(config_file),
                    # #765 is gated on #762/#763 — not ready yet.
                    "--next", "--pick", "765",
                ],
            )
        assert result.exit_code == 1
        assert "not in the ready-to-dispatch frontier" in result.output
        disp.assert_not_called()

    def test_pick_without_next_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "milestone", "dispatch", "api", "100", "--config", str(config_file),
                "--pick", "762",
            ],
        )
        assert result.exit_code == 2
        assert "--pick requires --next" in result.output


class TestMilestoneDispatchGateA:
    """#930 (docs/ORACLE_LOOP.md, Gate A) — the issue's specified black-box
    scenario: a milestone with no contract refuses issue dispatch; with a
    contract, allows it. Only applies to repos with an acceptance driver
    configured; ``config_file`` (no driver) is exercised everywhere above
    and is unaffected by this gate."""

    def test_no_contract_refuses_dispatch(self, config_file_with_gate_a: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.get_repo_file", side_effect=RuntimeError("404")), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config",
                 str(config_file_with_gate_a)],
            )
        assert result.exit_code == 1, result.output
        assert "Gate A not satisfied" in result.output
        assert "tests/acceptance/ms-9/contract.md" in result.output
        assert "coord acceptance mock api" in result.output
        disp.assert_not_called()

    def test_no_contract_refuses_even_under_dry_run_and_next(
        self, config_file_with_gate_a: Path,
    ) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.get_repo_file", side_effect=RuntimeError("404")), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config",
                 str(config_file_with_gate_a), "--dry-run", "--next"],
            )
        assert result.exit_code == 1, result.output
        assert "Gate A not satisfied" in result.output
        disp.assert_not_called()

    def test_contract_present_allows_dispatch(self, config_file_with_gate_a: Path) -> None:
        open_issues = [
            {"number": 762, "milestone": {"number": 9}},
            {"number": 763, "milestone": {"number": 9}},
            {"number": 765, "milestone": {"number": 9}},
        ]
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=open_issues), \
             patch("coord.github_ops.get_repo_file", return_value="# Contract\n"), \
             patch("coord.board_service.read_board", return_value=Board()), \
             patch("coord.dispatch.dispatch") as disp:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config",
                 str(config_file_with_gate_a), "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "Gate A" not in result.output
        will_dispatch = result.output.split("Will dispatch now:")[1].split("Waiting:")[0]
        assert "#762" in will_dispatch
        assert "#763" in will_dispatch

    def test_repo_without_acceptance_driver_is_unaffected(self, config_file: Path) -> None:
        """No `acceptance.drivers` entry for this repo -> Gate A is a no-op,
        exactly as before #930."""
        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.github_ops.get_repo_file") as get_file:
            result = CliRunner().invoke(
                main,
                ["milestone", "dispatch", "api", "100", "--config", str(config_file),
                 "--dry-run"],
            )
        get_file.assert_not_called()
        assert "Gate A" not in result.output
