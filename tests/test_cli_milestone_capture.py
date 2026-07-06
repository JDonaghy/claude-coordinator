"""Black-box tests for ``coord milestone capture`` (#977).

Coverage targets (mirroring test_cli_milestone_assign.py's CLI-level
class — the three underlying seam functions this command composes
already have their own coverage in test_milestone_seam.py /
test_cli_issue_create_label.py / test_cli_milestone_assign.py):

- happy path: write_milestone -> create_issue -> assign_issue_milestone
  called in order with the right arguments; success message printed.
- unknown repo -> exit 2.
- each of the three seam calls raising -> exit 1 with a message naming
  the failed step.
- --body omitted uses the default stub note; --body passed through
  verbatim when given.
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


class TestMilestoneCaptureCli:
    def test_happy_path_composes_all_three_seams_in_order(
        self, config_file: Path
    ) -> None:
        with patch(
            "coord.state.write_milestone",
            return_value={"number": 9, "title": "Redesign onboarding"},
        ) as mock_write_ms, patch(
            "coord.state.create_issue",
            return_value={"number": 101, "url": "https://github.com/acme/api/issues/101"},
        ) as mock_create_issue, patch(
            "coord.state.assign_issue_milestone",
        ) as mock_assign:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "capture", "api",
                    "--title", "Redesign onboarding",
                    "--config", str(config_file),
                ],
            )

        assert result.exit_code == 0, result.output
        mock_write_ms.assert_called_once_with(
            "api", title="Redesign onboarding", repo_github="acme/api"
        )
        mock_create_issue.assert_called_once()
        create_args, create_kwargs = mock_create_issue.call_args
        assert create_args[:2] == ("api", "Redesign onboarding")
        # Default body note when --body is omitted.
        assert "no work order yet" in create_args[2]
        assert create_kwargs["repo_github"] == "acme/api"
        mock_assign.assert_called_once_with(
            "api", 101, 9, milestone_title="Redesign onboarding", repo_github="acme/api"
        )
        assert "#9" in result.output
        assert "#101" in result.output
        assert "no work order yet" in result.output

    def test_custom_body_passed_through(self, config_file: Path) -> None:
        with patch(
            "coord.state.write_milestone", return_value={"number": 1, "title": "t"}
        ), patch(
            "coord.state.create_issue",
            return_value={"number": 2, "url": "https://x"},
        ) as mock_create_issue, patch("coord.state.assign_issue_milestone"):
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "capture", "api",
                    "--title", "t",
                    "--body", "my custom body",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        create_args, _ = mock_create_issue.call_args
        assert create_args[2] == "my custom body"

    def test_unknown_repo_exits_2(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "milestone", "capture", "nope",
                "--title", "t",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 2

    def test_milestone_creation_failure_exits_1(self, config_file: Path) -> None:
        with patch(
            "coord.state.write_milestone", side_effect=RuntimeError("gh: network error")
        ), patch("coord.state.create_issue") as mock_create_issue, patch(
            "coord.state.assign_issue_milestone"
        ) as mock_assign:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "capture", "api",
                    "--title", "t",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()
        assert "milestone" in result.output.lower()
        mock_create_issue.assert_not_called()
        mock_assign.assert_not_called()

    def test_issue_creation_failure_exits_1_and_names_milestone(
        self, config_file: Path
    ) -> None:
        with patch(
            "coord.state.write_milestone", return_value={"number": 9, "title": "t"}
        ), patch(
            "coord.state.create_issue", side_effect=RuntimeError("gh: label not found")
        ), patch("coord.state.assign_issue_milestone") as mock_assign:
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "capture", "api",
                    "--title", "t",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()
        assert "#9" in result.output
        mock_assign.assert_not_called()

    def test_assign_failure_exits_1(self, config_file: Path) -> None:
        with patch(
            "coord.state.write_milestone", return_value={"number": 9, "title": "t"}
        ), patch(
            "coord.state.create_issue",
            return_value={"number": 101, "url": "https://x"},
        ), patch(
            "coord.state.assign_issue_milestone",
            side_effect=RuntimeError("gh: not found"),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "milestone", "capture", "api",
                    "--title", "t",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 1
        assert "error" in result.output.lower()
        assert "#9" in result.output
        assert "#101" in result.output
