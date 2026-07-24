"""``coord sync`` wiring for the durable issue_comments backfill (#873).

`coord sync` already fetches open issues + upserts them into the local
cache; this covers the additional opportunistic issue_comments backfill —
scoped to issues that already have an assignment (active or archived), not
every open issue in the repo.
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


def _issue(number: int) -> dict:
    return {
        "number": number, "title": f"issue {number}", "body": "",
        "labels": [], "milestone": None, "assignees": [],
    }


def test_sync_backfills_comments_only_for_assigned_open_issues(
    config_file: Path, coord_db
) -> None:
    open_issues = [_issue(1), _issue(2), _issue(3)]
    with patch(
        "coord.github_ops.get_open_issues", return_value=open_issues,
    ), patch(
        "coord.state.list_issue_numbers_with_assignments",
        # issue 2 has assignment history, issue 3 does not (and issue 4 has
        # history but is no longer open — must not be synced either).
        return_value={2, 4},
    ), patch("coord.state.sync_issue_comments", return_value=1) as mock_sync:
        runner = CliRunner()
        result = runner.invoke(main, ["sync", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    mock_sync.assert_called_once_with("api", 2, repo_github="acme/api")


def test_sync_skips_comment_backfill_when_no_issue_has_assignments(
    config_file: Path, coord_db
) -> None:
    open_issues = [_issue(1)]
    with patch(
        "coord.github_ops.get_open_issues", return_value=open_issues,
    ), patch(
        "coord.state.list_issue_numbers_with_assignments", return_value=set(),
    ), patch("coord.state.sync_issue_comments") as mock_sync:
        runner = CliRunner()
        result = runner.invoke(main, ["sync", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
    mock_sync.assert_not_called()


def test_sync_comment_backfill_failure_does_not_fail_the_command(
    config_file: Path, coord_db
) -> None:
    open_issues = [_issue(1)]
    with patch(
        "coord.github_ops.get_open_issues", return_value=open_issues,
    ), patch(
        "coord.state.list_issue_numbers_with_assignments", return_value={1},
    ), patch(
        "coord.state.sync_issue_comments", side_effect=RuntimeError("gh boom"),
    ):
        runner = CliRunner()
        result = runner.invoke(main, ["sync", "--config", str(config_file)])

    assert result.exit_code == 0, result.output
