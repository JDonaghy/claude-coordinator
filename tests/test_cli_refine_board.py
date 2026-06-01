"""Tests for the `coord refine-board` CLI subcommand (#316 Phase C)."""
from __future__ import annotations

from unittest.mock import patch

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


class TestRefineBoardCli:
    """Integration smoke tests for `coord refine-board`."""

    @patch("coord.refine_chat.dispatch_board_refinement")
    def test_dispatches_and_prints_assignment_id(
        self, mock_dispatch, coord_db, simple_config
    ) -> None:
        """refine-board calls dispatch helper and prints the assignment id."""
        mock_dispatch.return_value = ("aid-321", "laptop")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["refine-board", "--config", str(simple_config), "api"],
        )
        assert result.exit_code == 0, result.output
        assert "aid-321" in result.output
        mock_dispatch.assert_called_once()

    @patch("coord.refine_chat.dispatch_board_refinement")
    def test_passes_repo_via_kwargs(
        self, mock_dispatch, coord_db, simple_config
    ) -> None:
        """dispatch_board_refinement receives the repo via the `repo=` kwarg."""
        mock_dispatch.return_value = ("aid-654", "laptop")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["refine-board", "--config", str(simple_config), "api"],
        )
        assert result.exit_code == 0, result.output
        _args, kwargs = mock_dispatch.call_args
        assert kwargs.get("repo") == "api"

    def test_exits_2_for_unknown_repo(self, coord_db, simple_config) -> None:
        """Unknown repo exits with code 2 (matches new-issue-chat behaviour)."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["refine-board", "--config", str(simple_config), "nosuchrepo"],
        )
        assert result.exit_code == 2

    @patch("coord.refine_chat.dispatch_board_refinement")
    def test_runtime_error_exits_1(
        self, mock_dispatch, coord_db, simple_config
    ) -> None:
        """RuntimeError from the dispatch helper exits with code 1."""
        mock_dispatch.side_effect = RuntimeError("no machine available")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["refine-board", "--config", str(simple_config), "api"],
        )
        assert result.exit_code == 1
        assert (
            "no machine available" in result.output
            or "no machine available" in (result.stderr or "")
        )

    @patch("coord.refine_chat.dispatch_board_refinement")
    def test_machine_override_passed_through(
        self, mock_dispatch, coord_db, simple_config
    ) -> None:
        """--machine option is forwarded to dispatch_board_refinement."""
        mock_dispatch.return_value = ("aid-987", "laptop")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "refine-board",
                "--config", str(simple_config),
                "--machine", "laptop",
                "api",
            ],
        )
        assert result.exit_code == 0, result.output
        _args, kwargs = mock_dispatch.call_args
        assert kwargs.get("machine_override") == "laptop"
