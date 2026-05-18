"""Tests for coord.brain — prompt assembly and proposal parsing."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from coord.brain import build_prompt, parse_proposals, gather_context, propose
from coord.config import Config
from coord.models import Machine, Repo


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
        assert "A" * 300 in prompt
        assert "A" * 301 not in prompt


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

        proposals = propose(config)
        assert len(proposals) == 1
        assert proposals[0].machine_name == "laptop"
        mock_claude.assert_called_once()
