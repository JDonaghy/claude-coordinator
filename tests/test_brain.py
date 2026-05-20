"""Tests for coord.brain — prompt assembly and proposal parsing."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from coord.brain import build_prompt, parse_proposals, gather_context, propose, resolve_required_gates
from coord.config import Config, PipelineConfig
from coord.models import Machine, Proposal, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[
            Repo(name="api", github="acme/api", depends_on=["shared"]),
            Repo(name="shared", github="acme/shared"),
        ],
        machines=[
            Machine(name="laptop", host="laptop.tailnet", capabilities=["python"], repos=["api", "shared"]),
            Machine(name="server", host="server.tailnet", capabilities=["python", "docker"], repos=["api"]),
        ],
    )


@pytest.fixture
def sample_context() -> dict:
    return {
        "issues_by_repo": {
            "api": [
                {"number": 10, "title": "Fix auth", "labels": [{"name": "bug"}], "body": "Auth is broken"},
                {"number": 11, "title": "Add caching", "labels": [], "body": ""},
            ],
            "shared": [],
        },
        "machine_status": {
            "laptop": {"status": "idle"},
            "server": {"status": "offline"},
        },
    }


class TestBuildPrompt:
    def test_contains_repos(self, config: Config, sample_context: dict) -> None:
        prompt = build_prompt(config, sample_context)
        assert "api (acme/api)" in prompt
        assert "shared (acme/shared)" in prompt
        assert "depends on: shared" in prompt

    def test_contains_machines_with_status(self, config: Config, sample_context: dict) -> None:
        prompt = build_prompt(config, sample_context)
        assert "laptop" in prompt
        assert "idle" in prompt
        assert "server" in prompt
        assert "offline" in prompt

    def test_contains_issues(self, config: Config, sample_context: dict) -> None:
        prompt = build_prompt(config, sample_context)
        assert "#10: Fix auth" in prompt
        assert "[bug]" in prompt
        assert "#11: Add caching" in prompt
        assert "Auth is broken" in prompt

    def test_no_issues_shows_placeholder(self, config: Config, sample_context: dict) -> None:
        prompt = build_prompt(config, sample_context)
        assert "shared: (no open issues)" in prompt

    def test_busy_machine_shows_assignment(self, config: Config) -> None:
        context = {
            "issues_by_repo": {"api": [], "shared": []},
            "machine_status": {
                "laptop": {
                    "status": "busy",
                    "assignment": {"issue_number": 5, "issue_title": "Something"},
                },
                "server": {"status": "idle"},
            },
        }
        prompt = build_prompt(config, context)
        assert "busy" in prompt
        assert "Something" in prompt

    def test_long_body_truncated(self, config: Config) -> None:
        context = {
            "issues_by_repo": {
                "api": [{"number": 1, "title": "X", "labels": [], "body": "A" * 500}],
                "shared": [],
            },
            "machine_status": {"laptop": {"status": "idle"}, "server": {"status": "idle"}},
        }
        prompt = build_prompt(config, context)
        assert "..." in prompt
        assert "A" * 150 in prompt
        assert "A" * 151 not in prompt


class TestParseProposals:
    def test_parse_valid_json(self) -> None:
        text = json.dumps([
            {
                "machine_name": "laptop",
                "repo_name": "api",
                "issue_number": 10,
                "issue_title": "Fix auth",
                "rationale": "laptop has python",
                "files_likely": ["auth.py"],
                "briefing": "Fix the auth module",
            }
        ])
        proposals = parse_proposals(text)
        assert len(proposals) == 1
        assert proposals[0].id == 1
        assert proposals[0].machine_name == "laptop"
        assert proposals[0].issue_number == 10
        assert proposals[0].files_likely == ["auth.py"]

    def test_parse_multiple_proposals(self) -> None:
        text = json.dumps([
            {"machine_name": "a", "repo_name": "r", "issue_number": 1, "issue_title": "x"},
            {"machine_name": "b", "repo_name": "r", "issue_number": 2, "issue_title": "y"},
        ])
        proposals = parse_proposals(text)
        assert len(proposals) == 2
        assert proposals[0].id == 1
        assert proposals[1].id == 2

    def test_parse_empty_array(self) -> None:
        assert parse_proposals("[]") == []

    def test_parse_strips_markdown_fences(self) -> None:
        text = '```json\n[{"machine_name":"a","repo_name":"r","issue_number":1,"issue_title":"x"}]\n```'
        proposals = parse_proposals(text)
        assert len(proposals) == 1

    def test_parse_rejects_non_array(self) -> None:
        with pytest.raises(ValueError, match="Expected JSON array"):
            parse_proposals('{"not": "an array"}')

    def test_parse_rejects_invalid_json(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_proposals("not json at all")

    def test_optional_fields_default(self) -> None:
        text = json.dumps([{
            "machine_name": "a",
            "repo_name": "r",
            "issue_number": 1,
            "issue_title": "x",
        }])
        proposals = parse_proposals(text)
        assert proposals[0].rationale == ""
        assert proposals[0].files_likely == []
        assert proposals[0].briefing == ""


class TestGatherContext:
    @patch("coord.brain.httpx.get")
    @patch("coord.brain.github_ops.get_open_issues")
    def test_gathers_issues_and_status(
        self, mock_issues: MagicMock, mock_get: MagicMock, config: Config,
    ) -> None:
        mock_issues.return_value = [{"number": 1, "title": "x"}]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "idle"}
        mock_get.return_value = mock_resp

        ctx = gather_context(config)
        assert "api" in ctx["issues_by_repo"]
        assert "shared" in ctx["issues_by_repo"]
        assert ctx["machine_status"]["laptop"] == {"status": "idle"}

    @patch("coord.brain.httpx.get")
    @patch("coord.brain.github_ops.get_open_issues")
    def test_handles_github_error(
        self, mock_issues: MagicMock, mock_get: MagicMock, config: Config,
    ) -> None:
        mock_issues.side_effect = RuntimeError("gh failed")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "idle"}
        mock_get.return_value = mock_resp

        ctx = gather_context(config)
        assert ctx["issues_by_repo"]["api"] == []

    @patch("coord.brain.httpx.get")
    @patch("coord.brain.github_ops.get_open_issues")
    def test_handles_agent_offline(
        self, mock_issues: MagicMock, mock_get: MagicMock, config: Config,
    ) -> None:
        mock_issues.return_value = []
        import httpx
        mock_get.side_effect = httpx.ConnectError("refused")

        ctx = gather_context(config)
        assert ctx["machine_status"]["laptop"] == {"status": "offline"}


class TestPropose:
    @patch("coord.brain.call_claude")
    @patch("coord.brain.gather_context")
    def test_full_cycle(
        self, mock_gather: MagicMock, mock_claude: MagicMock, config: Config,
    ) -> None:
        mock_gather.return_value = {
            "issues_by_repo": {"api": [], "shared": []},
            "machine_status": {"laptop": {"status": "idle"}, "server": {"status": "idle"}},
        }
        mock_claude.return_value = json.dumps([{
            "machine_name": "laptop",
            "repo_name": "api",
            "issue_number": 10,
            "issue_title": "Fix auth",
            "rationale": "best fit",
            "files_likely": ["auth.py"],
            "briefing": "do the thing",
        }])

        proposals, splits = propose(config)
        assert len(proposals) == 1
        assert proposals[0].machine_name == "laptop"
        assert splits == []
        mock_claude.assert_called_once()


class TestResolveRequiredGates:
    def _config_with_labels(self) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
            pipeline=PipelineConfig(
                default_gates=["review", "merge"],
                labels={
                    "documentation": ["merge"],
                    "hotfix": ["merge"],
                    "needs-smoke": ["review", "smoke", "merge"],
                },
            ),
        )

    def _proposal(self, issue_number: int = 10, repo: str = "api") -> Proposal:
        return Proposal(
            id=1,
            machine_name="laptop",
            repo_name=repo,
            issue_number=issue_number,
            issue_title="Some issue",
            rationale="",
        )

    def test_no_labels_config_leaves_gates_unchanged(self) -> None:
        """When config.pipeline.labels is empty, proposals are untouched."""
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )
        p = self._proposal()
        issues_by_repo = {"api": [{"number": 10, "title": "x", "labels": [{"name": "documentation"}]}]}
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == []  # unchanged

    def test_matching_label_sets_gates(self) -> None:
        """A matching issue label overrides required_gates on the proposal."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "documentation"}]}]
        }
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == ["merge"]

    def test_hotfix_label_resolves_to_merge_only(self) -> None:
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "hotfix"}]}]
        }
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == ["merge"]

    def test_smoke_label_resolves_to_full_pipeline(self) -> None:
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "needs-smoke"}]}]
        }
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == ["review", "smoke", "merge"]

    def test_no_matching_label_leaves_gates_unchanged(self) -> None:
        """Labels on the issue that aren't in config.pipeline.labels are ignored."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "bug"}]}]
        }
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == []  # unchanged

    def test_first_matching_label_wins(self) -> None:
        """When multiple labels match, the first one in the issue labels list wins."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [
                {
                    "number": 10,
                    "title": "x",
                    "labels": [{"name": "needs-smoke"}, {"name": "documentation"}],
                }
            ]
        }
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == ["review", "smoke", "merge"]  # needs-smoke wins

    def test_missing_issue_leaves_gates_unchanged(self) -> None:
        """If the issue isn't in issues_by_repo, proposal is untouched."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=99)
        issues_by_repo = {"api": [{"number": 10, "title": "x", "labels": []}]}
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == []

    def test_multiple_proposals_resolved_independently(self) -> None:
        cfg = self._config_with_labels()
        p1 = self._proposal(issue_number=10)
        p2 = Proposal(
            id=2,
            machine_name="laptop",
            repo_name="api",
            issue_number=11,
            issue_title="Another issue",
            rationale="",
        )
        issues_by_repo = {
            "api": [
                {"number": 10, "title": "x", "labels": [{"name": "documentation"}]},
                {"number": 11, "title": "y", "labels": [{"name": "needs-smoke"}]},
            ]
        }
        resolve_required_gates([p1, p2], cfg, issues_by_repo)
        assert p1.required_gates == ["merge"]
        assert p2.required_gates == ["review", "smoke", "merge"]

    def test_propose_calls_resolve_required_gates(self, config: Config) -> None:
        """propose() should apply label-based gate resolution to proposals."""
        from unittest.mock import patch

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
            pipeline=PipelineConfig(
                default_gates=["review", "merge"],
                labels={"documentation": ["merge"]},
            ),
        )
        issues_ctx = {
            "issues_by_repo": {
                "api": [
                    {
                        "number": 10,
                        "title": "Fix auth",
                        "labels": [{"name": "documentation"}],
                        "body": "",
                    }
                ]
            },
            "machine_status": {"laptop": {"status": "idle"}},
        }
        response_json = json.dumps([{
            "machine_name": "laptop",
            "repo_name": "api",
            "issue_number": 10,
            "issue_title": "Fix auth",
        }])
        with patch("coord.brain.gather_context", return_value=issues_ctx), \
             patch("coord.brain.call_claude", return_value=response_json):
            proposals, _ = propose(cfg)

        assert len(proposals) == 1
        assert proposals[0].required_gates == ["merge"]
