"""Black-box tests for `coord milestone chat --add-child` (#1017).

Covers the CLI-level plumbing added on top of the already-covered
`dispatch_milestone_chat(add_child_issue=...)` seam (test_milestone_chat.py):
- `--add-child` requires TRACKING_ISSUE (rejected with `--new`).
- `--add-child` is threaded through to `dispatch_milestone_chat` as
  `add_child_issue`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

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
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


class TestMilestoneChatAddChildCli:
    def test_add_child_with_new_is_rejected(self, config_file: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "milestone", "chat", "api", "--new", "--add-child", "1050",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2
        assert "--add-child requires TRACKING_ISSUE" in result.output

    def test_add_child_threaded_through_to_dispatch(self, config_file: Path) -> None:
        runner = CliRunner()
        with patch(
            "coord.milestone_chat.dispatch_milestone_chat",
            return_value=("asg-child", "laptop"),
        ) as mock_dispatch:
            result = runner.invoke(
                main,
                [
                    "milestone", "chat", "api", "100", "--add-child", "1050",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "asg-child" in result.output
        mock_dispatch.assert_called_once_with(
            "api", 100, mock_dispatch.call_args.args[2],
            machine_override=None,
            add_child_issue=1050,
        )
