"""Tests for the `coord new-issue-chat` CLI subcommand (#316)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main


@pytest.fixture
def simple_config(tmp_path):
    """Write a minimal coordinator.yml and return its path."""
    cfg_path = tmp_path / "coordinator.yml"
    cfg_path.write_text(
        """
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /home/user/src/api
"""
    )
    return cfg_path


class TestNewIssueChatCli:
    """Integration smoke tests for `coord new-issue-chat`."""

    @patch("coord.new_issue_chat.dispatch_new_issue_chat")
    def test_dispatches_and_prints_assignment_id(
        self, mock_dispatch, coord_db, simple_config
    ) -> None:
        """new-issue-chat calls dispatch helper and prints the assignment id."""
        mock_dispatch.return_value = ("aid-123", "laptop")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["new-issue-chat", "--config", str(simple_config), "api"],
        )
        assert result.exit_code == 0, result.output
        assert "aid-123" in result.output
        mock_dispatch.assert_called_once()

    @patch("coord.new_issue_chat.dispatch_new_issue_chat")
    def test_passes_repo_and_config(
        self, mock_dispatch, coord_db, simple_config
    ) -> None:
        """dispatch_new_issue_chat is called with the right repo and config."""
        mock_dispatch.return_value = ("aid-456", "laptop")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["new-issue-chat", "--config", str(simple_config), "api"],
        )
        assert result.exit_code == 0, result.output
        args, kwargs = mock_dispatch.call_args
        assert args[0] == "api"  # repo_name positional

    def test_exits_2_for_unknown_repo(self, coord_db, simple_config) -> None:
        """Unknown repo exits with code 2."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["new-issue-chat", "--config", str(simple_config), "nosuchrepo"],
        )
        assert result.exit_code == 2

    @patch("coord.new_issue_chat.dispatch_new_issue_chat")
    def test_runtime_error_exits_1(
        self, mock_dispatch, coord_db, simple_config
    ) -> None:
        """RuntimeError from the dispatch helper exits with code 1."""
        mock_dispatch.side_effect = RuntimeError("no machine available")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["new-issue-chat", "--config", str(simple_config), "api"],
        )
        assert result.exit_code == 1
        assert "no machine available" in result.output or "no machine available" in (result.stderr or "")

    @patch("coord.new_issue_chat.dispatch_new_issue_chat")
    def test_machine_override_passed_through(
        self, mock_dispatch, coord_db, simple_config
    ) -> None:
        """--machine option is forwarded to dispatch_new_issue_chat."""
        mock_dispatch.return_value = ("aid-789", "laptop")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "new-issue-chat",
                "--config", str(simple_config),
                "--machine", "laptop",
                "api",
            ],
        )
        assert result.exit_code == 0, result.output
        _args, kwargs = mock_dispatch.call_args
        assert kwargs.get("machine_override") == "laptop"
