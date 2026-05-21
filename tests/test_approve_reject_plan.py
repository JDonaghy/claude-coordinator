"""Tests for coord approve-plan, coord reject-plan commands and dispatch.require_plan config.

Covers:
- approve-plan: error when assignment not found
- approve-plan: error when assignment type is not 'plan'
- approve-plan: error when assignment is not 'done'
- approve-plan: error when no plan data is available
- approve-plan: successful dispatch of work assignment with enhanced briefing
- approve-plan: files_modify from plan used as files_likely
- reject-plan: error when assignment not found
- reject-plan: error when assignment type is not 'plan'
- reject-plan: error when assignment is not 'done'
- reject-plan: error when no plan data is available
- reject-plan: successful dispatch of plan assignment with guidance
- config: dispatch.require_plan parsed correctly
- assign: require_plan=True defaults to plan-only
- assign: --no-plan overrides require_plan=True
- assign: --plan-only always wins regardless of require_plan
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Assignment, Board
from coord import state as state_mod


# ── Fixtures ─────────────────────────────────────────────────────────────────

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

CONFIG_YAML_REQUIRE_PLAN = """\
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
dispatch:
  require_plan: true
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def require_plan_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_REQUIRE_PLAN)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    """Isolated in-memory DB + temp dir."""
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_plan_assignment(
    *,
    assignment_id: str = "plan-001",
    status: str = "done",
    type: str = "plan",
    briefing: str = "Read the codebase",
    files_allowed: list[str] | None = None,
    plan: dict | None = None,
) -> Assignment:
    """Return a minimal plan Assignment for tests."""
    return Assignment(
        assignment_id=assignment_id,
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Plan feature X",
        status=status,
        type=type,
        briefing=briefing,
        files_allowed=files_allowed or [],
        plan=plan,
    )


_SAMPLE_PLAN_DICT = {
    "plan": "Add caching layer",
    "files_read": ["coord/cache.py"],
    "files_modify": ["coord/cache.py", "coord/api.py"],
    "approach": "Use an LRU cache.",
    "risks": "Thread safety.",
    "estimate": "small",
    "raw_text": "PLAN: Add caching layer\nFILES_READ: coord/cache.py\nFILES_MODIFY: coord/cache.py, coord/api.py\nAPPROACH: Use an LRU cache.\nRISKS: Thread safety.\nESTIMATE: small",
}


# ── approve-plan ──────────────────────────────────────────────────────────────


class TestApprovePlan:
    def test_assignment_not_found(self, config_file: Path, coord_dir: Path) -> None:
        result = CliRunner().invoke(
            main, ["approve-plan", "nonexistent-id", "--config", str(config_file)]
        )
        assert result.exit_code == 1
        assert "not found in board" in result.output

    def test_wrong_type(self, config_file: Path, coord_dir: Path) -> None:
        a = _make_plan_assignment(assignment_id="work-001", type="work", status="done")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main, ["approve-plan", "work-001", "--config", str(config_file)]
        )
        assert result.exit_code == 1
        assert "not 'plan'" in result.output

    def test_not_done(self, config_file: Path, coord_dir: Path) -> None:
        a = _make_plan_assignment(assignment_id="plan-002", status="running")
        board = Board(active=[a], completed=[])
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main, ["approve-plan", "plan-002", "--config", str(config_file)]
        )
        assert result.exit_code == 1
        assert "not 'done'" in result.output

    def test_no_plan_data(self, config_file: Path, coord_dir: Path) -> None:
        """When no plan data is cached, approve-plan should report an error."""
        a = _make_plan_assignment(assignment_id="plan-003", plan=None)
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch("coord.cli._load_plan_for_assignment", return_value=None):
            result = CliRunner().invoke(
                main, ["approve-plan", "plan-003", "--config", str(config_file)]
            )
        assert result.exit_code == 1
        assert "no plan data found" in result.output

    def test_dispatches_work_assignment(self, config_file: Path, coord_dir: Path) -> None:
        """Successful approve-plan dispatches a work assignment."""
        a = _make_plan_assignment(
            assignment_id="plan-010", plan=_SAMPLE_PLAN_DICT
        )
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "work-010"}) as disp, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["approve-plan", "plan-010", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert "work-010" in result.output
        assert "Work assignment dispatched" in result.output

        # Verify proposal type is 'work'
        proposal = disp.call_args[0][0]
        assert proposal.type == "work"

    def test_enhanced_briefing_contains_plan_text(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """The work assignment briefing must include plan approval text."""
        a = _make_plan_assignment(
            assignment_id="plan-011",
            briefing="Original briefing",
            plan=_SAMPLE_PLAN_DICT,
        )
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "work-011"}) as disp, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["approve-plan", "plan-011", "--config", str(config_file)]
            )

        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert "Your plan was reviewed and approved" in proposal.briefing
        assert "Original briefing" in proposal.briefing
        assert "LRU cache" in proposal.briefing  # from approach

    def test_files_modify_used_as_files_likely(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """files_modify from the plan should be passed as files_likely to the work proposal."""
        a = _make_plan_assignment(
            assignment_id="plan-012", plan=_SAMPLE_PLAN_DICT
        )
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "work-012"}) as disp, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["approve-plan", "plan-012", "--config", str(config_file)]
            )

        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        # files_modify from the plan dict: ["coord/cache.py", "coord/api.py"]
        assert "coord/cache.py" in proposal.files_likely
        assert "coord/api.py" in proposal.files_likely

    def test_same_machine_used(self, config_file: Path, coord_dir: Path) -> None:
        """The work assignment must target the same machine as the plan."""
        a = _make_plan_assignment(
            assignment_id="plan-013", plan=_SAMPLE_PLAN_DICT
        )
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "work-013"}) as disp, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["approve-plan", "plan-013", "--config", str(config_file)]
            )

        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.machine_name == "laptop"
        assert proposal.repo_name == "api"
        assert proposal.issue_number == 42

    def test_dispatch_failure_exits_nonzero(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        import httpx

        a = _make_plan_assignment(
            assignment_id="plan-014", plan=_SAMPLE_PLAN_DICT
        )
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch(
            "coord.dispatch.dispatch",
            side_effect=httpx.ConnectError("refused"),
        ):
            result = CliRunner().invoke(
                main, ["approve-plan", "plan-014", "--config", str(config_file)]
            )

        assert result.exit_code == 1
        assert "dispatch failed" in result.output


# ── reject-plan ───────────────────────────────────────────────────────────────


class TestRejectPlan:
    def test_assignment_not_found(self, config_file: Path, coord_dir: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["reject-plan", "nonexistent-id", "--guidance", "Try again", "--config", str(config_file)],
        )
        assert result.exit_code == 1
        assert "not found in board" in result.output

    def test_wrong_type(self, config_file: Path, coord_dir: Path) -> None:
        a = _make_plan_assignment(assignment_id="work-020", type="work", status="done")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["reject-plan", "work-020", "--guidance", "Redo", "--config", str(config_file)],
        )
        assert result.exit_code == 1
        assert "not 'plan'" in result.output

    def test_not_done(self, config_file: Path, coord_dir: Path) -> None:
        a = _make_plan_assignment(assignment_id="plan-021", status="running")
        board = Board(active=[a], completed=[])
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["reject-plan", "plan-021", "--guidance", "Redo", "--config", str(config_file)],
        )
        assert result.exit_code == 1
        assert "not 'done'" in result.output

    def test_no_plan_data(self, config_file: Path, coord_dir: Path) -> None:
        a = _make_plan_assignment(assignment_id="plan-022", plan=None)
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch("coord.cli._load_plan_for_assignment", return_value=None):
            result = CliRunner().invoke(
                main,
                ["reject-plan", "plan-022", "--guidance", "Fix it", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "no plan data found" in result.output

    def test_guidance_required(self, config_file: Path, coord_dir: Path) -> None:
        """reject-plan requires --guidance."""
        result = CliRunner().invoke(
            main, ["reject-plan", "plan-023", "--config", str(config_file)]
        )
        assert result.exit_code != 0

    def test_dispatches_plan_assignment(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Successful reject-plan dispatches a new plan assignment."""
        a = _make_plan_assignment(
            assignment_id="plan-030", plan=_SAMPLE_PLAN_DICT
        )
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "plan-031"}) as disp, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "reject-plan", "plan-030",
                    "--guidance", "Focus on error handling",
                    "--config", str(config_file),
                ],
            )

        assert result.exit_code == 0, result.output
        assert "plan-031" in result.output
        assert "Revised plan assignment dispatched" in result.output

        # Verify new proposal type is 'plan'
        proposal = disp.call_args[0][0]
        assert proposal.type == "plan"

    def test_enhanced_briefing_contains_guidance_and_old_plan(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """The revised plan briefing must include both the rejected plan and guidance."""
        a = _make_plan_assignment(
            assignment_id="plan-031",
            briefing="Original briefing",
            plan=_SAMPLE_PLAN_DICT,
        )
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "plan-032"}) as disp, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "reject-plan", "plan-031",
                    "--guidance", "Focus on error handling",
                    "--config", str(config_file),
                ],
            )

        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert "Previous plan (rejected)" in proposal.briefing
        assert "Guidance" in proposal.briefing
        assert "Focus on error handling" in proposal.briefing
        assert "Original briefing" in proposal.briefing
        # Plan text should appear
        assert "LRU cache" in proposal.briefing

    def test_same_machine_used(self, config_file: Path, coord_dir: Path) -> None:
        a = _make_plan_assignment(
            assignment_id="plan-033", plan=_SAMPLE_PLAN_DICT
        )
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "plan-034"}) as disp, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "reject-plan", "plan-033",
                    "--guidance", "Redo",
                    "--config", str(config_file),
                ],
            )

        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.machine_name == "laptop"
        assert proposal.issue_number == 42


# ── dispatch.require_plan config parsing ─────────────────────────────────────


class TestRequirePlanConfig:
    def test_default_is_false(self) -> None:
        from coord.config import load

        # Use a minimal config with no dispatch section
        import tempfile, textwrap
        yaml = textwrap.dedent("""\
            repos:
              - name: api
                github: acme/api
            machines:
              - name: laptop
                host: laptop.tailnet
                repos: [api]
        """)
        with tempfile.NamedTemporaryFile(suffix=".yml", mode="w", delete=False) as f:
            f.write(yaml)
            fname = f.name
        cfg = load(fname)
        assert cfg.dispatch.require_plan is False

    def test_require_plan_true_parsed(self, require_plan_config_file: Path) -> None:
        from coord.config import load

        cfg = load(require_plan_config_file)
        assert cfg.dispatch.require_plan is True

    def test_require_plan_non_bool_raises(self, tmp_path: Path) -> None:
        from coord.config import load, ConfigError

        yaml = """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
dispatch:
  require_plan: yes-please
"""
        p = tmp_path / "bad.yml"
        p.write_text(yaml)
        with pytest.raises(ConfigError, match="require_plan"):
            load(p)


# ── coord assign respects require_plan ───────────────────────────────────────


class TestAssignRequirePlan:
    def test_require_plan_defaults_to_plan_only(
        self, require_plan_config_file: Path, coord_dir: Path
    ) -> None:
        """With require_plan=true, assign without --no-plan should dispatch type='plan'."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Feature X"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "plan-rp-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(require_plan_config_file)],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.type == "plan"
        assert "plan" in result.output.lower()

    def test_no_plan_overrides_require_plan(
        self, require_plan_config_file: Path, coord_dir: Path
    ) -> None:
        """--no-plan with require_plan=true should dispatch type='work'."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Feature X"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "work-rp-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--no-plan",
                    "--config", str(require_plan_config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.type == "work"

    def test_plan_only_wins_over_require_plan(
        self, require_plan_config_file: Path, coord_dir: Path
    ) -> None:
        """--plan-only always dispatches a plan, even when require_plan is already true."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Feature X"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "plan-rp-2"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--plan-only",
                    "--config", str(require_plan_config_file),
                ],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.type == "plan"

    def test_require_plan_false_dispatches_work(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """With require_plan=false (default), assign dispatches type='work'."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Feature X"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "work-rp-3"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.type == "work"

    def test_no_plan_flag_accepted_when_require_plan_false(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--no-plan is a valid flag even when require_plan=false (it's a no-op)."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Feature X"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "work-rp-4"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--no-plan",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.type == "work"
