"""Tests for the `coord approve` Max-plan usage-gate pre-check (#1466).

`coord approve` can dispatch several headless workers in one batch — exactly
the shape that runs a 5h/weekly window dry mid-batch. `approve()` probes
once via `coord.usage_limits.get_plan_limits()` and gates on
`cfg.usage_gate` before touching any proposal.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from coord.models import Proposal
from coord.usage_limits import PlanLimits


def _config_file(tmp_path: Path, *, usage_gate_yaml: str = "") -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        + usage_gate_yaml
    )
    return p


def _make_proposal() -> Proposal:
    return Proposal(
        id=1,
        machine_name="m",
        repo_name="api",
        issue_number=42,
        issue_title="Some task",
        rationale="work",
        files_likely=["api/a.py"],
    )


def _invoke_approve(config_file: Path):
    from coord.cli import main

    runner = CliRunner()
    with patch("coord.claim.find_work_claim", return_value=None), patch(
        "coord.github_ops.get_issue", return_value={"labels": []}
    ):
        return runner.invoke(
            main, ["approve", "1", "--config", str(config_file), "--dry-run"]
        )


class TestApproveUsageGate:
    def test_below_threshold_proceeds_silently(self, tmp_path: Path, coord_db) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: warn\n  session_threshold_pct: 85\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="ok", session_pct=10.0),
        ):
            result = _invoke_approve(config_file)
        assert result.exit_code == 0, result.output
        assert "Max-plan usage near limit" not in result.output

    def test_warn_mode_above_threshold_warns_but_still_dispatches(
        self, tmp_path: Path, coord_db
    ) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: warn\n  session_threshold_pct: 85\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(
                status="ok", session_pct=92.0, session_resets_at="8pm (UTC)"
            ),
        ):
            result = _invoke_approve(config_file)
        assert result.exit_code == 0, result.output
        assert "Max-plan usage near limit" in result.output
        assert "8pm (UTC)" in result.output

    def test_block_mode_above_threshold_refuses(self, tmp_path: Path, coord_db) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: block\n  week_threshold_pct: 90\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="ok", week_pct=95.0, week_resets_at="Aug 1"),
        ):
            result = _invoke_approve(config_file)
        assert result.exit_code == 1
        assert "Max-plan usage near limit" in result.output
        assert "Aug 1" in result.output

    def test_probe_unavailable_never_blocks(self, tmp_path: Path, coord_db) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: block\n  session_threshold_pct: 1\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="unknown", error="probe timed out"),
        ):
            result = _invoke_approve(config_file)
        assert result.exit_code == 0, result.output
        assert "Max-plan usage near limit" not in result.output

    def test_disabled_mode_never_probes(self, tmp_path: Path, coord_db) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file(
            tmp_path, usage_gate_yaml="usage_gate:\n  mode: disabled\n"
        )
        with patch("coord.usage_limits.get_plan_limits") as mock_probe:
            result = _invoke_approve(config_file)
        assert result.exit_code == 0, result.output
        mock_probe.assert_not_called()
