"""Tests for model tiering: auto-select worker model by complexity,
escalate on failure."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.agent import AssignmentSpec, default_worker_command
from coord.cli import main
from coord.config import Config, ConfigError, ModelsConfig, load
from coord.dispatch import dispatch
from coord.models import Assignment, Machine, Proposal, Repo


# ── ModelsConfig defaults and parsing ──────────────────────────────────────


class TestModelsConfigDefaults:
    def test_defaults(self) -> None:
        mc = ModelsConfig()
        assert mc.default == "sonnet"
        assert mc.escalation == ["haiku", "sonnet", "opus"]
        assert mc.labels == {}

    def test_parsed_from_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
            "models:\n"
            "  default: sonnet\n"
            "  escalation: [haiku, sonnet, opus]\n"
            "  labels:\n"
            "    documentation: haiku\n"
            "    bug: sonnet\n"
            "    enhancement: sonnet\n"
            "    infrastructure: opus\n"
        )
        cfg = load(p)
        assert cfg.models.default == "sonnet"
        assert cfg.models.escalation == ["haiku", "sonnet", "opus"]
        assert cfg.models.labels == {
            "documentation": "haiku",
            "bug": "sonnet",
            "enhancement": "sonnet",
            "infrastructure": "opus",
        }

    def test_missing_section_uses_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.models.default == "sonnet"
        assert cfg.models.escalation == ["haiku", "sonnet", "opus"]
        assert cfg.models.labels == {}

    def test_custom_default_only(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
            "models:\n"
            "  default: opus\n"
        )
        cfg = load(p)
        assert cfg.models.default == "opus"
        # Other fields fall back to dataclass defaults.
        assert cfg.models.escalation == ["haiku", "sonnet", "opus"]

    def test_invalid_models_type(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
            "models: true\n"
        )
        with pytest.raises(ConfigError, match="must be a mapping"):
            load(p)

    def test_invalid_default_type(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
            "models:\n"
            "  default: 42\n"
        )
        with pytest.raises(ConfigError, match="default"):
            load(p)

    def test_invalid_escalation_type(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
            "models:\n"
            "  escalation: not-a-list\n"
        )
        with pytest.raises(ConfigError, match="escalation"):
            load(p)


# ── next_model() escalation helper ─────────────────────────────────────────


class TestNextModel:
    def test_haiku_to_sonnet(self) -> None:
        mc = ModelsConfig()
        assert mc.next_model("haiku") == "sonnet"

    def test_sonnet_to_opus(self) -> None:
        mc = ModelsConfig()
        assert mc.next_model("sonnet") == "opus"

    def test_opus_stays_at_opus(self) -> None:
        """Top of the ladder: no further escalation."""
        mc = ModelsConfig()
        assert mc.next_model("opus") == "opus"

    def test_unknown_stays_same(self) -> None:
        """Models not on the ladder return unchanged."""
        mc = ModelsConfig()
        assert mc.next_model("gpt-4") == "gpt-4"

    def test_custom_escalation(self) -> None:
        mc = ModelsConfig(escalation=["tiny", "small", "big"])
        assert mc.next_model("tiny") == "small"
        assert mc.next_model("small") == "big"
        assert mc.next_model("big") == "big"


# ── Worker command --model flag ────────────────────────────────────────────


def _spec(**overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="do the thing",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


class TestWorkerCommandModel:
    def test_model_flag_appears_when_set(self) -> None:
        spec = _spec(model="opus")
        argv = default_worker_command(spec)
        assert "--model" in argv
        idx = argv.index("--model")
        assert argv[idx + 1] == "opus"

    def test_no_model_flag_when_none(self) -> None:
        spec = _spec(model=None)
        argv = default_worker_command(spec)
        assert "--model" not in argv

    def test_no_model_flag_when_empty_string(self) -> None:
        """Empty string is falsy — treat like None."""
        spec = _spec(model="")
        argv = default_worker_command(spec)
        assert "--model" not in argv

    def test_model_pair_present_when_set(self) -> None:
        """With stream-json input mode the briefing is sent on stdin, not
        as a positional argv tail — but --model still needs to appear as
        a flag/value pair."""
        spec = _spec(model="haiku", briefing="my-briefing")
        argv = default_worker_command(spec)
        idx = argv.index("--model")
        assert argv[idx + 1] == "haiku"
        # Briefing no longer appears in argv — it's stdin-delivered now.
        assert "my-briefing" not in argv

    def test_model_haiku(self) -> None:
        spec = _spec(model="haiku")
        argv = default_worker_command(spec)
        idx = argv.index("--model")
        assert argv[idx + 1] == "haiku"


# ── Dispatch payload includes model ────────────────────────────────────────


def _make_cfg(
    *,
    default_model: str = "sonnet",
    escalation: list[str] | None = None,
) -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            ),
        ],
        models=ModelsConfig(
            default=default_model,
            escalation=escalation or ["haiku", "sonnet", "opus"],
        ),
    )


def _make_proposal(**overrides) -> Proposal:
    base = dict(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=10,
        issue_title="t",
        rationale="r",
    )
    base.update(overrides)
    return Proposal(**base)


class TestDispatchModel:
    @patch("coord.dispatch.httpx.post")
    def test_proposal_model_takes_precedence(self, mock_post: MagicMock) -> None:
        cfg = _make_cfg(default_model="sonnet")
        proposal = _make_proposal(model="opus")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "opus"

    @patch("coord.dispatch.httpx.post")
    def test_default_model_when_proposal_has_none(
        self, mock_post: MagicMock
    ) -> None:
        cfg = _make_cfg(default_model="sonnet")
        proposal = _make_proposal(model=None)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "sonnet"

    @patch("coord.dispatch.httpx.post")
    def test_payload_includes_model_field(self, mock_post: MagicMock) -> None:
        """Even when no model is set anywhere, the payload key exists."""
        cfg = _make_cfg(default_model="haiku")
        proposal = _make_proposal()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "model" in payload
        assert payload["model"] == "haiku"


# ── coord assign --model passes through ────────────────────────────────────


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
"""


@pytest.fixture
def cli_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def cli_coord_dir(tmp_path: Path, coord_db) -> Path:
    """Provide an isolated in-memory DB for state and return a temp dir."""
    d = tmp_path / "state"
    return d


class TestCliAssignModel:
    def test_assign_model_flag_passes_through(
        self, cli_config_file: Path, cli_coord_dir: Path,
    ) -> None:
        with patch(
            "coord.github_ops.get_issue", return_value={"title": "Issue title"}
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign",
                    "laptop", "api", "42",
                    "--config", str(cli_config_file),
                    "--model", "opus",
                ],
            )
        assert result.exit_code == 0, result.output
        disp.assert_called_once()
        proposal = disp.call_args[0][0]
        assert proposal.model == "opus"

    def test_assign_no_model_uses_config_default(
        self, cli_config_file: Path, cli_coord_dir: Path,
    ) -> None:
        with patch(
            "coord.github_ops.get_issue", return_value={"title": "Issue title"}
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign",
                    "laptop", "api", "42",
                    "--config", str(cli_config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        # Config default is sonnet.
        assert proposal.model == "sonnet"

    def test_assign_dispatched_record_includes_model(
        self, cli_config_file: Path, cli_coord_dir: Path,
    ) -> None:
        from coord import state as state_mod

        with patch(
            "coord.github_ops.get_issue", return_value={"title": "t"}
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "rec-1"}
        ), patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign",
                    "laptop", "api", "7",
                    "--config", str(cli_config_file),
                    "--model", "haiku",
                ],
            )
        assert result.exit_code == 0, result.output
        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["model"] == "haiku"


# ── Escalation on follow-up commands ───────────────────────────────────────


class TestFollowupEscalation:
    def test_dispatch_followup_uses_provided_model(self) -> None:
        """_dispatch_followup builds a Proposal carrying the model override."""
        from coord.cli import _dispatch_followup

        cfg = _make_cfg(default_model="sonnet")
        original = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="t",
            assignment_id="abc12345",
            status="failed",
            briefing="b",
            model="sonnet",
        )

        captured: dict = {}

        def fake_dispatch(proposal, _cfg, **_kwargs):
            captured["model"] = proposal.model
            return {"id": "newid12345"}

        def fake_post_briefing(*_a, **_kw):
            return None

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), patch(
            "coord.dispatch.post_briefing", side_effect=fake_post_briefing
        ), patch("coord.state.record_dispatched"), patch(
            "coord.state.save_board"
        ), patch("coord.state.build_board"), patch(
            "coord.state.load_dispatched", return_value=[]
        ):
            new_id = _dispatch_followup(cfg, original, "follow-up briefing", model="opus")

        assert new_id == "newid12345"
        assert captured["model"] == "opus"

    def test_dispatch_followup_falls_back_to_config_default(self) -> None:
        """When no model override is passed, the proposal uses config.models.default."""
        from coord.cli import _dispatch_followup

        cfg = _make_cfg(default_model="sonnet")
        original = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="t",
            assignment_id="abc12345",
            status="failed",
            briefing="b",
        )

        captured: dict = {}

        def fake_dispatch(proposal, _cfg, **_kwargs):
            captured["model"] = proposal.model
            return {"id": "newid12345"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), patch(
            "coord.dispatch.post_briefing"
        ), patch("coord.state.record_dispatched"), patch(
            "coord.state.save_board"
        ), patch("coord.state.build_board"), patch(
            "coord.state.load_dispatched", return_value=[]
        ):
            _dispatch_followup(cfg, original, "follow-up briefing")

        assert captured["model"] == "sonnet"

    def test_followup_carries_parent_branch_as_target_branch(self) -> None:
        """Regression: _dispatch_followup must pin the new worker to the
        parent's existing branch so a `[fix-N] …` / `[conflict-fix] …`-prefixed
        issue title doesn't make the agent slugify it into an orphan branch.

        Reproduces the #206 incident where `coord pr` on a fix-up assignment
        derived branch `issue-206-fix-1-tui-machines-panel-restart-update`
        instead of pushing to the original `issue-206-…` branch.
        """
        from coord.cli import _dispatch_followup

        cfg = _make_cfg(default_model="sonnet")
        original = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=206,
            issue_title="[fix-1] tui machines panel restart update",
            assignment_id="parent12345",
            status="done",
            branch="issue-206-tui-machines-panel-restart-update",
            briefing="b",
        )

        captured: dict = {}

        def fake_dispatch(proposal, _cfg, **_kwargs):
            captured["target_branch"] = proposal.target_branch
            return {"id": "newid12345"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), patch(
            "coord.dispatch.post_briefing"
        ), patch("coord.state.record_dispatched"), patch(
            "coord.state.save_board"
        ), patch("coord.state.build_board"), patch(
            "coord.state.load_dispatched", return_value=[]
        ):
            _dispatch_followup(cfg, original, "create the PR")

        assert captured["target_branch"] == "issue-206-tui-machines-panel-restart-update"

    def test_followup_target_branch_none_when_parent_has_no_branch(self) -> None:
        """Plan-type parents have branch=None; followups must not invent one.
        The agent then derives the branch from the (unprefixed) issue title."""
        from coord.cli import _dispatch_followup

        cfg = _make_cfg(default_model="sonnet")
        original = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="Plan: refactor cache",
            assignment_id="planparent1",
            status="done",
            branch=None,
            briefing="b",
            type="plan",
        )

        captured: dict = {}

        def fake_dispatch(proposal, _cfg, **_kwargs):
            captured["target_branch"] = proposal.target_branch
            return {"id": "workchild12"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), patch(
            "coord.dispatch.post_briefing"
        ), patch("coord.state.record_dispatched"), patch(
            "coord.state.save_board"
        ), patch("coord.state.build_board"), patch(
            "coord.state.load_dispatched", return_value=[]
        ):
            _dispatch_followup(cfg, original, "do the work", type="work")

        assert captured["target_branch"] is None

    def test_escalation_in_fix_sonnet_to_opus(self) -> None:
        """coord fix on an assignment that ran sonnet escalates to opus."""
        cfg = _make_cfg(default_model="sonnet")
        original_model = "sonnet"
        escalated = cfg.models.next_model(original_model)
        assert escalated == "opus"

    def test_escalation_at_top_stays_opus(self) -> None:
        """Already at the top of the ladder — no further escalation."""
        cfg = _make_cfg(default_model="sonnet")
        original_model = "opus"
        escalated = cfg.models.next_model(original_model)
        assert escalated == original_model

    def test_escalation_uses_config_default_when_original_unset(self) -> None:
        """If the original assignment has no model, escalate from the config default."""
        cfg = _make_cfg(default_model="haiku")
        original_assignment_model = None
        original = original_assignment_model or cfg.models.default
        escalated = cfg.models.next_model(original)
        assert original == "haiku"
        assert escalated == "sonnet"


# ── Reassign carries model ─────────────────────────────────────────────────


class TestReassignModel:
    @patch("coord.reconcile.httpx.post")
    def test_reassign_uses_failed_model_when_no_override(
        self, mock_post: MagicMock
    ) -> None:
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tailnet",
                    repos=["api"],
                    repo_paths={"api": "/tmp/api"},
                ),
                Machine(
                    name="server",
                    host="server.tailnet",
                    repos=["api"],
                    repo_paths={"api": "/tmp/api"},
                ),
            ],
            models=ModelsConfig(default="sonnet"),
        )
        board = Board()
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            briefing="b",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
        )

        result = _reassign(failed, board, cfg)
        assert result is not None
        assert result.model == "sonnet"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "sonnet"

    @patch("coord.reconcile.httpx.post")
    def test_reassign_with_model_override(self, mock_post: MagicMock) -> None:
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tailnet",
                    repos=["api"],
                    repo_paths={"api": "/tmp/api"},
                ),
                Machine(
                    name="server",
                    host="server.tailnet",
                    repos=["api"],
                    repo_paths={"api": "/tmp/api"},
                ),
            ],
            models=ModelsConfig(default="sonnet"),
        )
        board = Board()
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            briefing="b",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
        )

        result = _reassign(failed, board, cfg, model="opus")
        assert result is not None
        assert result.model == "opus"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "opus"


# ── Assignment dataclass backward compatibility ────────────────────────────


class TestAssignmentModelField:
    def test_assignment_defaults_model_to_none(self) -> None:
        a = Assignment(
            machine_name="m",
            repo_name="r",
            issue_number=1,
            issue_title="t",
        )
        assert a.model is None

    def test_proposal_defaults_model_to_none(self) -> None:
        p = Proposal(
            id=1,
            machine_name="m",
            repo_name="r",
            issue_number=1,
            issue_title="t",
            rationale="x",
        )
        assert p.model is None
