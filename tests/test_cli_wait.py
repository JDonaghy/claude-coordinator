"""CLI tests for `coord wait`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from coord import merge_queue as mq
from coord import state as state_mod
from coord.cli import main


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


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db) -> Path:
    d = tmp_path / "state"
    return d


def _seed_dispatched(assignment_id: str = "abc-1", machine_name: str = "laptop") -> None:
    """Insert a dispatched record into SQLite so load_dispatched() can find it."""
    from coord.models import Proposal
    from coord.state import record_dispatched
    proposal = Proposal(
        id=1,
        machine_name=machine_name,
        repo_name="api",
        issue_number=42,
        issue_title="Add feature X",
        rationale="",
    )
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github="acme/api",
    )


def _mock_response(data: dict, status_code: int = 200) -> httpx.Response:
    """Build a mock httpx.Response with JSON body."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    return resp


class TestWaitHappyPath:
    """Assignment completes with exit 0."""

    def test_completed_exit_zero(self, config_file: Path, coord_dir: Path) -> None:
        _seed_dispatched()
        agent_data = {
            "active": [],
            "completed": [
                {
                    "id": "abc-1",
                    "exit_code": 0,
                    "branch": "issue-42-feature-x",
                    "started_at": 1000,
                    "finished_at": 1120,
                }
            ],
        }
        with patch("coord.cli.httpx.get", return_value=_mock_response(agent_data)):
            result = CliRunner().invoke(
                main,
                ["wait", "abc-1", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        assert "completed (exit 0, 2m 0s)" in result.output
        assert "branch: issue-42-feature-x" in result.output


class TestWaitWorkerFailure:
    """Assignment completes with non-zero exit code."""

    def test_completed_exit_nonzero(self, config_file: Path, coord_dir: Path) -> None:
        _seed_dispatched()
        agent_data = {
            "active": [],
            "completed": [
                {
                    "id": "abc-1",
                    "exit_code": 1,
                    "branch": "issue-42-feature-x",
                    "error": "tests failed",
                    "started_at": 1000,
                    "finished_at": 1060,
                }
            ],
        }
        with patch("coord.cli.httpx.get", return_value=_mock_response(agent_data)):
            result = CliRunner().invoke(
                main,
                ["wait", "abc-1", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "failed (exit 1, 1m 0s)" in result.output
        assert "error: tests failed" in result.output
        assert "branch: issue-42-feature-x" in result.output


class TestWaitAlreadyDone:
    """Assignment is already completed on first poll — exits immediately."""

    def test_already_completed(self, config_file: Path, coord_dir: Path) -> None:
        _seed_dispatched()
        agent_data = {
            "active": [],
            "completed": [
                {
                    "id": "abc-1",
                    "exit_code": 0,
                    "branch": "issue-42-done",
                    "started_at": 500,
                    "finished_at": 800,
                }
            ],
        }
        with patch("coord.cli.httpx.get", return_value=_mock_response(agent_data)) as mock_get:
            result = CliRunner().invoke(
                main,
                ["wait", "abc-1", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        assert "completed" in result.output
        # Should only poll once (already done)
        mock_get.assert_called_once()


class TestWaitNotFoundInDispatched:
    """Unknown assignment ID not in dispatched records."""

    def test_unknown_assignment_id(self, config_file: Path, coord_dir: Path) -> None:
        # coord_dir exists but no dispatched records match
        result = CliRunner().invoke(
            main,
            ["wait", "ghost-99", "--config", str(config_file)],
        )
        assert result.exit_code == 2
        assert "not found in dispatched records" in result.output


class TestWaitTimeout:
    """Assignment stays active past the deadline — exits 3."""

    def test_timeout_while_active(self, config_file: Path, coord_dir: Path) -> None:
        _seed_dispatched()
        agent_data = {
            "active": [{"id": "abc-1"}],
            "completed": [],
        }

        # Mock time.monotonic to simulate time passing quickly:
        # First call returns 0 (start), second call returns 0.5 (< deadline=1),
        # third call returns 2 (past deadline=1).
        mono_values = iter([0, 0.5, 2])

        with patch("coord.cli.httpx.get", return_value=_mock_response(agent_data)), \
             patch("coord.cli.time.monotonic", side_effect=mono_values), \
             patch("coord.cli.time.sleep"):
            result = CliRunner().invoke(
                main,
                ["wait", "abc-1", "--config", str(config_file), "--timeout", "1", "--interval", "1"],
            )
        assert result.exit_code == 3
        assert "Timed out" in result.output


class TestWaitAgentUnreachable:
    """Connection error during poll — warns but keeps polling."""

    def test_agent_unreachable_then_completes(self, config_file: Path, coord_dir: Path) -> None:
        _seed_dispatched()
        completed_data = {
            "active": [],
            "completed": [
                {
                    "id": "abc-1",
                    "exit_code": 0,
                    "branch": "issue-42-ok",
                    "started_at": 100,
                    "finished_at": 200,
                }
            ],
        }

        # First call raises connection error, second succeeds
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("connection refused")
            return _mock_response(completed_data)

        with patch("coord.cli.httpx.get", side_effect=side_effect), \
             patch("coord.cli.time.sleep"):
            result = CliRunner().invoke(
                main,
                ["wait", "abc-1", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        assert "warning:" in result.output
        assert "completed (exit 0" in result.output


class TestWaitVanished:
    """Assignment not found in active or completed on the agent."""

    def test_assignment_vanished(self, config_file: Path, coord_dir: Path) -> None:
        _seed_dispatched()
        agent_data = {
            "active": [{"id": "other-1"}],
            "completed": [],
        }
        with patch("coord.cli.httpx.get", return_value=_mock_response(agent_data)):
            result = CliRunner().invoke(
                main,
                ["wait", "abc-1", "--config", str(config_file)],
            )
        assert result.exit_code == 2
        assert "not found on agent" in result.output
