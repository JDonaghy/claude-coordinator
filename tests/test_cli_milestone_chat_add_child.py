"""Black-box tests for `coord milestone chat --add-child` (#1017).

Covers the CLI-level plumbing added on top of the already-covered
`dispatch_milestone_chat(add_child_issue=...)` seam (test_milestone_chat.py):
- `--add-child` requires TRACKING_ISSUE (rejected with `--new`).
- `--add-child` is threaded through to `dispatch_milestone_chat` as
  `add_child_issue`.
- `milestone chat` (both the `--new` and TRACKING_ISSUE branches) reports a
  clean one-line `error:` for a dispatch-layer `httpx.HTTPError`/`ValueError`
  instead of letting it escape as an unhandled traceback (#1017 fix-review:
  the smoke-test operator saw a "silent no-op" — dispatch failing with no
  visible reason — for the `--new` path; `dispatch_with_retry`/`dispatch()`
  raise these two exception types, not `RuntimeError`, so the pre-existing
  `except RuntimeError` alone let them through uncaught).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
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

    def test_existing_tracking_issue_dispatch_http_error_reports_clean_message(
        self, config_file: Path
    ) -> None:
        """A network failure in `dispatch_milestone_chat` (httpx.HTTPError,
        not RuntimeError) must produce a clean one-line `error:` — not an
        unhandled traceback — so the TUI's `first_meaningful_stderr_line`
        toast has something actionable to show."""
        runner = CliRunner()
        with patch(
            "coord.milestone_chat.dispatch_milestone_chat",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            result = runner.invoke(
                main,
                ["milestone", "chat", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert result.exc_info is None or not issubclass(
            result.exc_info[0], httpx.HTTPError
        ), "httpx.HTTPError must be caught, not propagate as an unhandled exception"
        assert "error: dispatch failed" in result.output

    def test_existing_tracking_issue_dispatch_value_error_reports_clean_message(
        self, config_file: Path
    ) -> None:
        """`dispatch()` raises ValueError for e.g. a missing repo_path — also
        not a RuntimeError, also deserves a clean message."""
        runner = CliRunner()
        with patch(
            "coord.milestone_chat.dispatch_milestone_chat",
            side_effect=ValueError("No repo_path configured for 'api' on machine 'laptop'"),
        ):
            result = runner.invoke(
                main,
                ["milestone", "chat", "api", "100", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "error: dispatch failed — No repo_path configured" in result.output

    def test_new_milestone_dispatch_http_error_reports_clean_message(
        self, config_file: Path
    ) -> None:
        """Same coverage as above, for the `--new` branch (`dispatch_new_
        milestone_chat`) — this is the exact path the #1017 smoke test drove
        via the Plans panel's bare `C` keybinding."""
        runner = CliRunner()
        with patch(
            "coord.milestone_chat.dispatch_new_milestone_chat",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            result = runner.invoke(
                main,
                ["milestone", "chat", "api", "--new", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "error: dispatch failed" in result.output
