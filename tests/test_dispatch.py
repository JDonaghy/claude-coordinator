"""Tests for coord.dispatch — assignment dispatch and briefing."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch, MagicMock

import pytest

from coord.config import (
    AcceptanceConfig,
    AcceptanceDriverConfig,
    Config,
    ProviderDef,
    ProvidersConfig,
)
from coord.dispatch import dispatch, enforce_oracle_readiness, post_briefing
from coord.models import Machine, Proposal, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[
            Repo(name="api", github="acme/api"),
        ],
        machines=[
            Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            ),
        ],
    )


@pytest.fixture
def proposal() -> Proposal:
    return Proposal(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=10,
        issue_title="Fix auth",
        rationale="best fit",
        files_likely=["auth.py"],
        briefing="Fix the auth module",
    )


class TestDispatch:
    @patch("coord.dispatch.httpx.post")
    def test_posts_to_agent_server(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        result = dispatch(proposal, config)
        # #324: dispatch() injects _provider_name metadata into the result dict
        # so callers can record it without re-resolving the config chain.
        assert result["ok"] is True
        assert "_provider_name" in result  # injected by dispatch()
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "laptop.tailnet" in call_args.args[0]
        payload = call_args.kwargs["json"]
        assert payload["issue_number"] == 10
        assert payload["repo_path"] == "/home/user/src/api"
        assert payload["files_allowed"] == ["auth.py"]
        assert "files_likely" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_payload_prepends_issue_context(
        self, mock_post: MagicMock, config: Config, proposal: Proposal, coord_db,
    ) -> None:
        # #603: a -p WORK briefing carries the per-issue context digest at the top.
        from coord import state

        state._add_issue_context_entry_local(
            "api", 10, "depends on lib #99 (commit abc); do X first", pinned=True
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert briefing.startswith("## ⚠️ Issue context")  # block at the top
        assert "depends on lib #99" in briefing
        assert "Fix the auth module" in briefing  # original briefing preserved below

    @patch("coord.dispatch.httpx.post")
    def test_payload_no_context_when_none(
        self, mock_post: MagicMock, config: Config, proposal: Proposal, coord_db,
    ) -> None:
        # No context for the issue → briefing unchanged (no empty block noise).
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        assert mock_post.call_args.kwargs["json"]["briefing"] == "Fix the auth module"

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_default_branch(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#255: the dispatch payload must include the repo's configured
        default_branch so the agent doesn't fall back to a hardcoded "main"
        and silently route around `default_branch: develop` repos."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="develop")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["branch"] == "develop", (
            f"#255: expected branch=develop in payload, got {payload.get('branch')!r}"
        )

    @patch("coord.dispatch.httpx.post")
    def test_payload_branch_falls_back_to_main_when_unset(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """When a repo doesn't specify default_branch, the payload still
        carries an explicit "main" so the agent never sees branch=None."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["branch"] == "main"

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_target_branch_when_set(
        self, mock_post: MagicMock, config: Config,
    ) -> None:
        """When proposal.target_branch is set, dispatch payload includes it
        so the agent checks out the explicit branch instead of slugifying the
        (possibly `[fix-N] …` / `[conflict-fix] …`-prefixed) issue title."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=206, issue_title="[fix-1] tui machines panel restart update",
            rationale="follow-up",
            target_branch="issue-206-tui-machines-panel-restart-update",
        )
        dispatch(p, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["target_branch"] == "issue-206-tui-machines-panel-restart-update"

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_target_branch_when_unset(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """Older agents (pre-#target_branch) reject unknown kwargs in
        AssignmentSpec(**body), so the field must be omitted when not set."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert "target_branch" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_coordinator_only_files(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api", github="acme/api",
                coordinator_only_files=["README.md", "CHANGELOG.md"],
            )],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["files_forbidden"] == ["README.md", "CHANGELOG.md"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_auto_seals_acceptance_dir_when_driver_configured(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#944 sealing v1: a repo with an oracle-loop acceptance driver gets
        tests/acceptance/ auto-forbidden even without listing it under
        coordinator_only_files — sealing shouldn't depend on remembering to
        configure both."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "tests/acceptance/" in payload["files_forbidden"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_auto_seals_acceptance_dir_when_driver_is_routed(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#1125 review finding 1: a REPO's acceptance driver may be routed
        (acceptance.drivers.<repo>.routes) rather than flat — sealing must
        still trigger, since `driver_for(repo_name)` (no path) can't select
        a route and would otherwise return None here, silently un-sealing
        tests/acceptance/ the instant a repo adopts routes."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(
                        match="coord/**", kind="cli-pytest", run="pytest",
                    ),
                ]),
            }),
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "tests/acceptance/" in payload["files_forbidden"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_mock_author_exempt_from_acceptance_seal(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#930: `type="mock-author"` is the one type whose entire job is
        writing under tests/acceptance/ms-NN/ (Gate A) — it must NOT get the
        #944 auto-forbid, even though the repo has a driver configured."""
        from dataclasses import replace

        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        mock_author_proposal = replace(proposal, type="mock-author")
        dispatch(mock_author_proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "tests/acceptance/" not in payload["files_forbidden"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_no_acceptance_seal_without_driver(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["files_forbidden"] == []

    @patch("coord.dispatch.httpx.post")
    def test_payload_acceptance_seal_dedupes_with_coordinator_only_files(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api", github="acme/api",
                coordinator_only_files=["tests/acceptance/"],
            )],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["files_forbidden"].count("tests/acceptance/") == 1

    @patch("coord.dispatch.httpx.post")
    def test_payload_prepends_oracle_loop_contract_when_slice_authored(
        self, mock_post: MagicMock, proposal: Proposal, tmp_path, coord_db,
    ) -> None:
        """#945: a repo with an acceptance driver configured AND an authored
        manifest slice for this issue gets the oracle-loop contract block
        prepended (after the #603 digest, before the original briefing)."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        acceptance_dir = tmp_path / "tests" / "acceptance" / "ms01"
        acceptance_dir.mkdir(parents=True)
        (acceptance_dir / "manifest.yml").write_text("tests:\n  ms01::a: 10\n")

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": str(tmp_path)},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "## 🔒 Oracle-loop acceptance contract" in briefing
        assert "tests/acceptance/ms01/contract.md" in briefing
        assert "coord acceptance run --repo api --issue 10" in briefing
        assert briefing.rstrip().endswith("Fix the auth module")  # original briefing last

    @patch("coord.dispatch.httpx.post")
    def test_payload_oracle_loop_contract_with_tilde_repo_path(
        self, mock_post: MagicMock, proposal: Proposal, tmp_path, coord_db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#945 review follow-up: repo_paths entries configured with a
        literal ``~`` (the README's canonical ``repo_paths: { my-project:
        ~/src/my-project }`` example) must resolve the same way the sibling
        ``.expanduser()`` call three lines above does. Before the fix,
        ``Path(repo_path) / ACCEPTANCE_DIRNAME`` left the ``~`` unexpanded,
        ``.exists()`` was always False, and the contract block silently
        never appeared for any repo configured the documented way."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        repo_dir = fake_home / "src" / "api"
        acceptance_dir = repo_dir / "tests" / "acceptance" / "ms01"
        acceptance_dir.mkdir(parents=True)
        (acceptance_dir / "manifest.yml").write_text("tests:\n  ms01::a: 10\n")

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "~/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "## 🔒 Oracle-loop acceptance contract" in briefing
        assert "tests/acceptance/ms01/contract.md" in briefing

    @patch("coord.dispatch.httpx.post")
    def test_payload_prepends_oracle_loop_contract_when_driver_is_routed(
        self, mock_post: MagicMock, proposal: Proposal, tmp_path, coord_db,
    ) -> None:
        """#1125 review finding 1: the same as
        test_payload_prepends_oracle_loop_contract_when_slice_authored, but
        the repo's driver is routed rather than flat — the injection guard
        (`config.acceptance.has_driver(...)`) must still fire, since a bare
        `driver_for(repo_name)` (no path) can't resolve a route and would
        otherwise return None here, silently dropping the contract block."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        acceptance_dir = tmp_path / "tests" / "acceptance" / "ms01"
        acceptance_dir.mkdir(parents=True)
        (acceptance_dir / "manifest.yml").write_text("tests:\n  ms01::a: 10\n")

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": str(tmp_path)},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(
                        match="**", kind="cli-pytest", run="pytest",
                    ),
                ]),
            }),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "## 🔒 Oracle-loop acceptance contract" in briefing
        assert "tests/acceptance/ms01/contract.md" in briefing

    @patch("coord.dispatch.httpx.post")
    def test_payload_no_oracle_loop_contract_without_driver(
        self, mock_post: MagicMock, config: Config, proposal: Proposal, coord_db,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "Oracle-loop acceptance contract" not in briefing

    @patch("coord.dispatch.httpx.post")
    def test_payload_no_oracle_loop_contract_when_issue_not_authored(
        self, mock_post: MagicMock, proposal: Proposal, tmp_path, coord_db,
    ) -> None:
        """Driver configured, but no manifest covers this issue yet (Gate
        A/#931 hasn't authored its slice) — no block, no crash."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": str(tmp_path)},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "Oracle-loop acceptance contract" not in briefing
        assert briefing == "Fix the auth module"

    def test_unknown_machine_raises(self, config: Config) -> None:
        bad = Proposal(
            id=1, machine_name="ghost", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
        )
        with pytest.raises(ValueError, match="Unknown machine"):
            dispatch(bad, config)

    def test_missing_repo_path_raises(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="h", repos=["api"])],
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
        )
        with pytest.raises(ValueError, match="repo_path"):
            dispatch(p, cfg)


class TestOracleReadinessGate:
    """#1138: `dispatch()` hard-gates a `type="work"` dispatch on the
    issue-level oracle gate (`coord.milestone_dispatch.issue_oracle_ready`)
    for issues in an oracle-opted-in milestone (Gate A satisfied) with no
    authored acceptance slice yet — the exact gap that let #1118 slip
    through the ordinary pipeline despite ms-37's Gate A being satisfied."""

    def _cfg(self, *, kind: str = "cli-pytest") -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(
                drivers={"api": AcceptanceDriverConfig(kind=kind, run="pytest")}
            ),
        )

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_repo_file")
    @patch("coord.github_ops.get_issue")
    def test_refuses_work_dispatch_with_no_slice(
        self, mock_get_issue, mock_get_repo_file, mock_post,
    ) -> None:
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1118, issue_title="Usage Core",
            rationale="", type="work",
        )
        mock_get_issue.return_value = {"milestone": {"number": 37}, "labels": []}
        # contract.md exists (Gate A satisfied); no manifest -> no slice.
        mock_get_repo_file.side_effect = lambda repo, path, branch=None: (
            "contract body" if path.endswith("contract.md") else (_ for _ in ()).throw(RuntimeError("404"))
        )

        with pytest.raises(ValueError, match="no acceptance slice yet"):
            dispatch(p, cfg)
        mock_post.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_repo_file")
    @patch("coord.github_ops.get_issue")
    def test_dispatches_when_slice_authored(
        self, mock_get_issue, mock_get_repo_file, mock_post,
    ) -> None:
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1118, issue_title="Usage Core",
            rationale="", type="work",
        )
        mock_get_issue.return_value = {"milestone": {"number": 37}, "labels": []}

        def _repo_file(repo, path, branch=None):
            if path.endswith("contract.md"):
                return "contract body"
            if path.endswith("manifest.yml"):
                return "tests:\n  ms37::a: 1118\n"
            raise RuntimeError("404")

        mock_get_repo_file.side_effect = _repo_file
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        result = dispatch(p, cfg)
        assert result["ok"] is True
        mock_post.assert_called_once()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_repo_file")
    @patch("coord.github_ops.get_issue")
    def test_no_gate_when_repo_has_no_acceptance_driver(
        self, mock_get_issue, mock_get_repo_file, mock_post, config, proposal,
    ) -> None:
        """Scenario (b)/(c): repos with no acceptance.drivers entry dispatch
        exactly as before #1138 — no extra GitHub calls, no refusal."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        mock_post.assert_called_once()
        mock_get_issue.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_repo_file")
    @patch("coord.github_ops.get_issue")
    def test_no_gate_for_plan_type(
        self, mock_get_issue, mock_get_repo_file, mock_post,
    ) -> None:
        """Read-only plan-only dispatches aren't gated — only `type="work"`
        creates code-writing sessions the gate exists to guard."""
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1118, issue_title="Usage Core",
            rationale="", type="plan",
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        mock_post.assert_called_once()
        mock_get_issue.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_repo_file")
    @patch("coord.github_ops.get_issue")
    def test_exempt_label_allows_dispatch_with_no_slice(
        self, mock_get_issue, mock_get_repo_file, mock_post,
    ) -> None:
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1125, issue_title="test-author driver",
            rationale="", type="work",
        )
        mock_get_issue.return_value = {
            "milestone": {"number": 37}, "labels": [{"name": "oracle:exempt"}],
        }
        mock_get_repo_file.side_effect = lambda repo, path, branch=None: (
            "contract body" if path.endswith("contract.md") else (_ for _ in ()).throw(RuntimeError("404"))
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        mock_post.assert_called_once()

    def test_enforce_oracle_readiness_direct_no_op_for_review_type(self) -> None:
        cfg = self._cfg()
        repo = cfg.repo("api")
        # No mocking needed — "review" isn't "work", so this must short
        # circuit before any GitHub call.
        enforce_oracle_readiness(
            proposal_type="review", repo=repo, config=cfg, issue_number=1,
        )


class TestPostBriefing:
    @patch("coord.dispatch.github_ops.post_issue_comment")
    def test_posts_comment(
        self, mock_comment: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        post_briefing(proposal, config)
        mock_comment.assert_called_once()
        args = mock_comment.call_args.args
        assert args[0] == "acme/api"
        assert args[1] == 10
        assert "laptop" in args[2]
        assert "auth.py" in args[2]

    def test_unknown_repo_raises(self, config: Config) -> None:
        bad = Proposal(
            id=1, machine_name="laptop", repo_name="ghost",
            issue_number=1, issue_title="x", rationale="",
        )
        with pytest.raises(ValueError, match="Unknown repo"):
            post_briefing(bad, config)

    @patch("coord.dispatch.github_ops.add_issue_labels")
    @patch("coord.dispatch.github_ops.post_issue_comment")
    def test_auto_labels_issue_with_tracked_labels(
        self,
        mock_comment: MagicMock,
        mock_add_labels: MagicMock,
        config: Config,
        proposal: Proposal,
    ) -> None:
        """post_briefing must tag the issue with cfg.pipeline.tracked_labels()
        so the TUI Pipeline panel picks it up.  Without this, manually
        filed issues stay invisible until the user remembers to label them
        (we hit this on quadraui#263)."""
        post_briefing(proposal, config)
        mock_add_labels.assert_called_once_with("acme/api", 10, ["coord"])

    @patch("coord.dispatch.github_ops.add_issue_labels")
    @patch("coord.dispatch.github_ops.post_issue_comment")
    def test_labeling_failure_does_not_break_briefing(
        self,
        mock_comment: MagicMock,
        mock_add_labels: MagicMock,
        config: Config,
        proposal: Proposal,
    ) -> None:
        """Labeling is best-effort — a `gh` failure must not propagate
        and break the briefing flow."""
        mock_add_labels.side_effect = RuntimeError("gh not installed")
        post_briefing(proposal, config)  # must not raise
        mock_comment.assert_called_once()
        mock_add_labels.assert_called_once()


class TestResumeSessionId:
    """#315: resume_session_id flows from Proposal through dispatch payload."""

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_resume_session_id_when_set(
        self, mock_post: MagicMock, config: Config,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Chat",
            rationale="continuation",
            type="refinement",
            resume_session_id="ses-abc-123",
        )
        dispatch(p, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["resume_session_id"] == "ses-abc-123"

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_resume_session_id_when_unset(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """Older agents reject unknown keys — the field must be absent when None."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert "resume_session_id" not in payload


class TestArtifactPaths:
    """#305: artifact_paths flows from repo config through dispatch payload."""

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_artifact_paths_for_work_assignment(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload for a work proposal should include the repo's
        artifact_paths so remote agents can stash artifacts without coordinator.yml."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api",
                github="acme/api",
                artifact_paths=["target/debug/mybinary*", "dist/*.tar.gz"],
            )],
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Build release",
            rationale="build",
            type="work",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["artifact_paths"] == ["target/debug/mybinary*", "dist/*.tar.gz"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_artifact_paths_for_work_when_not_configured(
        self, mock_post: MagicMock,
    ) -> None:
        """Older agents reject unknown keys — artifact_paths must be absent
        when the repo has no artifact_paths configured (empty list)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],  # no artifact_paths
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix bug",
            rationale="fix",
            type="work",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "artifact_paths" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_payload_excludes_artifact_paths_for_review_assignment(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload for a review proposal should not include
        artifact_paths at all — reviews don't build artifacts, and older
        agents reject unknown payload keys with a 400."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api",
                github="acme/api",
                artifact_paths=["target/debug/mybinary*"],
            )],
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Review PR",
            rationale="review",
            type="review",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "artifact_paths" not in payload


class TestNewIssueGuidance:
    """#352: new_issue_guidance flows from repo config through dispatch payload."""

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_new_issue_guidance_for_new_issue_chat(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload for a new-issue-chat proposal should include
        the repo's resolved new_issue_guidance so the agent can include it
        in the system prompt."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        guidance = "Required sections: Title, Description, Acceptance Criteria"
        cfg = Config(
            repos=[Repo(
                name="api",
                github="acme/api",
                new_issue_guidance=guidance,
            )],
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=0,
            issue_title="(new issue draft)",
            rationale="new-issue-chat",
            type="new-issue-chat",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["new_issue_guidance"] == guidance

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_new_issue_guidance_when_not_configured(
        self, mock_post: MagicMock,
    ) -> None:
        """When the repo has no custom new_issue_guidance, the payload must
        OMIT the field entirely so agents that predate #352 can accept the
        dispatch.  The agent's built-in NEW_ISSUE_CHAT_SYSTEM_PROMPT is fine
        without the guidance augmentation."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],  # no new_issue_guidance
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=0,
            issue_title="(new issue draft)",
            rationale="new-issue-chat",
            type="new-issue-chat",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "new_issue_guidance" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_payload_excludes_new_issue_guidance_for_work_assignment(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload for a work proposal should not include
        new_issue_guidance — it's only for new-issue-chat type."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        guidance = "Required sections: Title, Description, Acceptance Criteria"
        cfg = Config(
            repos=[Repo(
                name="api",
                github="acme/api",
                new_issue_guidance=guidance,
            )],
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix bug",
            rationale="fix",
            type="work",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "new_issue_guidance" not in payload


class TestProviderDispatch:
    """#324: dispatch() resolves provider name and threads it through the payload and DB."""

    @patch("coord.dispatch.httpx.post")
    def test_default_provider_omitted_from_payload(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """When the effective provider is 'claude' (the default), the wire
        payload must NOT include 'provider' — older agents reject unknown keys
        and the no-config parity requirement demands byte-identical payloads."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert "provider" not in payload, (
            "default provider 'claude' must not appear in wire payload "
            "(no-config parity, #324)"
        )

    @patch("coord.dispatch.httpx.post")
    def test_non_default_provider_in_payload(self, mock_post: MagicMock) -> None:
        """When the repo configures a non-default provider, its name is sent
        in the payload so the agent routes the assignment correctly."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="fast-claude")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "fast-claude": ProviderDef(type="claude"),
                    "claude": ProviderDef(type="claude"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "fast-claude", (
            f"expected provider='fast-claude' in payload, got {payload.get('provider')!r}"
        )

    @patch("coord.dispatch.httpx.post")
    def test_result_contains_provider_name_metadata(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """dispatch() returns _provider_name in the result dict so callers
        can persist the resolved name without re-resolving the config chain."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "def"}
        mock_post.return_value = mock_resp

        result = dispatch(proposal, config)
        assert "_provider_name" in result, (
            "dispatch() must inject _provider_name into the return dict (#324)"
        )
        assert result["_provider_name"] == "claude"  # default config

    @patch("coord.dispatch.httpx.post")
    def test_result_provider_name_reflects_repo_override(
        self, mock_post: MagicMock,
    ) -> None:
        """When the repo configures a non-default provider, _provider_name in
        the result reflects the resolved (not just the spec-level) name."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "ghi"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="fast-claude")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "fast-claude": ProviderDef(type="claude"),
                    "claude": ProviderDef(type="claude"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        result = dispatch(p, cfg)
        assert result["_provider_name"] == "fast-claude"

    @patch("coord.dispatch.httpx.post")
    def test_spec_provider_override_beats_repo(self, mock_post: MagicMock) -> None:
        """proposal.provider (spec-level) beats repo.provider in the resolution
        chain and is reflected in both the payload and _provider_name."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "xyz"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="repo-provider")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "repo-provider": ProviderDef(type="claude"),
                    "spec-provider": ProviderDef(type="claude"),
                    "claude": ProviderDef(type="claude"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            provider="spec-provider",  # explicit spec override
        )
        result = dispatch(p, cfg)
        assert result["_provider_name"] == "spec-provider"
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "spec-provider"


class TestProviderNamePersistence:
    """#324: record_dispatched() persists provider_name on the assignment row."""

    def test_record_dispatched_stores_provider_name(self) -> None:
        """provider_name kwarg is persisted in the assignments table."""
        import sqlite3
        from coord.db import override_connection, close
        from coord.state import record_dispatched, load_dispatched

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from coord.db import _ensure_schema
        _ensure_schema(conn)
        override_connection(conn)
        try:
            p = Proposal(
                id=1, machine_name="laptop", repo_name="api",
                issue_number=10, issue_title="Fix auth", rationale="ok",
                briefing="do the thing", type="work",
            )
            record_dispatched(
                assignment_id="asgn-001",
                proposal=p,
                repo_github="acme/api",
                provider_name="fast-claude",
            )
            rows = load_dispatched()
            assert rows, "expected at least one dispatched row"
            # The raw row should carry provider_name
            row = conn.execute(
                "SELECT provider_name FROM assignments WHERE assignment_id=?",
                ("asgn-001",),
            ).fetchone()
            assert row is not None
            assert row["provider_name"] == "fast-claude"
        finally:
            close()

    def test_record_dispatched_provider_name_defaults_to_null(self) -> None:
        """When provider_name is not passed, the column stays NULL (backward
        compat — existing callers in cli.py don't pass the arg)."""
        import sqlite3
        from coord.db import override_connection, close
        from coord.state import record_dispatched

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from coord.db import _ensure_schema
        _ensure_schema(conn)
        override_connection(conn)
        try:
            p = Proposal(
                id=1, machine_name="laptop", repo_name="api",
                issue_number=10, issue_title="Fix auth", rationale="ok",
                briefing="do the thing", type="work",
            )
            record_dispatched(
                assignment_id="asgn-002",
                proposal=p,
                repo_github="acme/api",
                # no provider_name → default None
            )
            row = conn.execute(
                "SELECT provider_name FROM assignments WHERE assignment_id=?",
                ("asgn-002",),
            ).fetchone()
            assert row is not None
            assert row["provider_name"] is None
        finally:
            close()
