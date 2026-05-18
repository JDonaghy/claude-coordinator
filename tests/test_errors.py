"""Error handling tests: agent unreachable, claude -p fails, GitHub is down."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
import pytest
from click.testing import CliRunner

from coord.brain import gather_context, call_claude, propose
from coord.config import Config
from coord.dispatch import dispatch
from coord.models import Machine, Proposal, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
        ],
    )


# ── Agent unreachable ──────────────────────────────────────────────────────


class TestAgentUnreachable:
    def test_gather_context_marks_offline(self, config: Config) -> None:
        with (
            patch("coord.brain.github_ops.get_open_issues", return_value=[]),
            patch("coord.brain.httpx.get", side_effect=httpx.ConnectError("refused")),
        ):
            ctx = gather_context(config)

        assert ctx["machine_status"]["laptop"] == {"status": "offline"}

    def test_dispatch_connection_error(self, config: Config) -> None:
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
        )
        with patch("coord.dispatch.httpx.post", side_effect=httpx.ConnectError("refused")):
            with pytest.raises(httpx.ConnectError):
                dispatch(proposal, config)

    def test_dispatch_timeout(self, config: Config) -> None:
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
        )
        with patch("coord.dispatch.httpx.post", side_effect=httpx.TimeoutException("timeout")):
            with pytest.raises(httpx.TimeoutException):
                dispatch(proposal, config)


# ── claude -p fails ────────────────────────────────────────────────────────


class TestClaudeFails:
    def test_nonzero_exit_raises_runtime_error(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "claude error: no subscription"

        with patch("coord.brain.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="claude -p failed"):
                call_claude("system", "user")

    def test_invalid_json_response_raises(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json at all"

        with patch("coord.brain.subprocess.run", return_value=mock_result):
            with pytest.raises(json.JSONDecodeError):
                call_claude("system", "user")

    def test_subprocess_timeout_raises(self) -> None:
        with patch(
            "coord.brain.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=120),
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                call_claude("system", "user")

    def test_propose_surfaces_claude_error(self, config: Config) -> None:
        with (
            patch("coord.brain.gather_context", return_value={
                "issues_by_repo": {"api": []},
                "machine_status": {"laptop": {"status": "idle"}},
            }),
            patch("coord.brain.call_claude", side_effect=RuntimeError("claude -p failed")),
        ):
            with pytest.raises(RuntimeError, match="claude -p failed"):
                propose(config)


# ── GitHub is down ─────────────────────────────────────────────────────────


class TestGitHubDown:
    def test_gather_context_returns_empty_issues(self, config: Config) -> None:
        with (
            patch(
                "coord.brain.github_ops.get_open_issues",
                side_effect=RuntimeError("gh: not authenticated"),
            ),
            patch("coord.brain.httpx.get") as mock_get,
        ):
            mock_get.return_value = MagicMock(json=lambda: {"active": [], "completed": []})
            ctx = gather_context(config)

        assert ctx["issues_by_repo"]["api"] == []

    def test_post_briefing_failure_is_catchable(self, config: Config) -> None:
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
            briefing="do stuff",
        )
        with patch(
            "coord.dispatch.github_ops.post_issue_comment",
            side_effect=RuntimeError("gh: API rate limit exceeded"),
        ):
            with pytest.raises(RuntimeError, match="rate limit"):
                from coord.dispatch import post_briefing
                post_briefing(proposal, config)


# ── CLI error messages ─────────────────────────────────────────────────────


class TestCLIErrors:
    def test_approve_no_pending_proposals(self, tmp_path: Path) -> None:
        from coord.cli import main

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )

        runner = CliRunner()
        result = runner.invoke(main, ["approve", "1", "--config", str(config_file)])
        assert result.exit_code != 0
        assert "No pending proposals" in result.output

    def test_approve_bad_ids(self, tmp_path: Path) -> None:
        from coord.cli import main
        from coord.models import Proposal

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )

        proposals_file = tmp_path / "pending_proposals.json"
        with (
            patch("coord.state.COORD_DIR", tmp_path),
            patch("coord.state.PROPOSALS_FILE", proposals_file),
        ):
            from coord.state import save_proposals
            save_proposals([Proposal(
                id=1, machine_name="m", repo_name="api",
                issue_number=1, issue_title="x", rationale="",
            )])

            runner = CliRunner()
            result = runner.invoke(main, ["approve", "abc", "--config", str(config_file)])
        assert result.exit_code != 0
        assert "comma-separated integers" in result.output

    def test_log_missing_assignment(self) -> None:
        from coord.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["log", "nonexistent_id"])
        assert result.exit_code != 0
        assert "no log found" in result.output
