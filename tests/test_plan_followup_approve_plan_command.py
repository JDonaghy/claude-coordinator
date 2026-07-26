"""Tests for `coord approve-plan`'s #1430 model-routing: the plan's ESTIMATE
overrides the issue's label-derived model for the work assignment it
dispatches, falling back to the label and then to models.default.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Assignment, Board
from coord import state as state_mod

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
models:
  default: sonnet
  escalation: [haiku, sonnet, opus]
  labels:
    bug: sonnet
    tier:small: haiku
    tier:large: opus
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _make_plan_assignment(plan: dict | None) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Some issue",
        assignment_id="plan-001",
        type="plan",
        status="done",
        branch="plan-scratch",
        plan=plan,
    )


class TestApprovePlanModelRouting:
    def test_estimate_overrides_label(self, config_file: Path, coord_db) -> None:
        """A tier:small issue (haiku) whose plan estimates 'large' work
        dispatches on the top escalation rung (opus), not the label's haiku."""
        a = _make_plan_assignment({"estimate": "large"})
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with (
            patch(
                "coord.commands.plan_followup._dispatch_followup", return_value="work-001",
            ) as disp,
            patch(
                "coord.github_ops.get_issue",
                return_value={"labels": [{"name": "tier:small"}]},
            ),
        ):
            result = CliRunner().invoke(
                main, ["approve-plan", "plan-001", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert disp.call_args.kwargs["model"] == "opus"
        assert "opus" in result.output
        assert "overriding label-derived" in result.output

    def test_label_used_when_no_estimate(self, config_file: Path, coord_db) -> None:
        a = _make_plan_assignment({"estimate": ""})
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with (
            patch(
                "coord.commands.plan_followup._dispatch_followup", return_value="work-002",
            ) as disp,
            patch(
                "coord.github_ops.get_issue",
                return_value={"labels": [{"name": "tier:large"}]},
            ),
        ):
            result = CliRunner().invoke(
                main, ["approve-plan", "plan-001", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert disp.call_args.kwargs["model"] == "opus"

    def test_falls_back_to_default_when_neither_available(
        self, config_file: Path, coord_db
    ) -> None:
        a = _make_plan_assignment({"estimate": ""})
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with (
            patch(
                "coord.commands.plan_followup._dispatch_followup", return_value="work-003",
            ) as disp,
            patch("coord.github_ops.get_issue", return_value={"labels": []}),
        ):
            result = CliRunner().invoke(
                main, ["approve-plan", "plan-001", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert disp.call_args.kwargs["model"] is None
        assert "sonnet (default)" in result.output

    def test_trivial_estimate_maps_to_bottom_rung(
        self, config_file: Path, coord_db
    ) -> None:
        a = _make_plan_assignment({"estimate": "trivial"})
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with (
            patch(
                "coord.commands.plan_followup._dispatch_followup", return_value="work-004",
            ) as disp,
            patch("coord.github_ops.get_issue", return_value={"labels": []}),
        ):
            result = CliRunner().invoke(
                main, ["approve-plan", "plan-001", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert disp.call_args.kwargs["model"] == "haiku"
