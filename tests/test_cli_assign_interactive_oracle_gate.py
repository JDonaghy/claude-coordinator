"""#1138: `coord assign --interactive` (the human-attended work launcher)
must be hard-gated by the same issue-level oracle-loop readiness check as
the headless `coord assign`/`coord approve` paths — a human at the keyboard
can miss the same "no acceptance slice authored yet" gap a `claude -p`
worker would.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main

_LOCAL_HOST = socket.gethostname().split(".")[0]

CONFIG_YAML = f"""\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: {_LOCAL_HOST}
    host: {_LOCAL_HOST}.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
acceptance:
  drivers:
    api:
      kind: cli-pytest
      run: pytest
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _repo_file_contract_only(repo: str, path: str, branch: str | None = None) -> str:
    """Gate A satisfied (contract.md exists); no manifest anywhere ->
    no authored slice for any issue."""
    if path.endswith("contract.md"):
        return "contract body"
    raise RuntimeError("404")


class TestInteractiveWorkOracleGate:
    def test_refuses_with_no_authored_slice(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        launched: list[Any] = []

        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Usage Core", "milestone": {"number": 37}, "labels": []},
        ), \
             patch("coord.github_ops.get_repo_file", side_effect=_repo_file_contract_only), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch(
                 "coord.interactive.launch_human_attended_interactive",
                 side_effect=lambda *a, **k: launched.append(True) or 0,
             ) as mock_local, \
             patch("coord.interactive._launch_via_tmux") as mock_remote:
            result = CliRunner().invoke(
                main,
                [
                    "assign", _LOCAL_HOST, "api", "1118",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )

        assert result.exit_code != 0
        assert "no acceptance slice yet" in result.output
        mock_local.assert_not_called()
        mock_remote.assert_not_called()
        assert launched == []

    def test_dispatches_when_slice_authored(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        def _repo_file(repo: str, path: str, branch: str | None = None) -> str:
            if path.endswith("contract.md"):
                return "contract body"
            if path.endswith("manifest.yml"):
                return "tests:\n  ms37::a: 1118\n"
            raise RuntimeError("404")

        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Usage Core", "milestone": {"number": 37}, "labels": []},
        ), \
             patch("coord.github_ops.get_repo_file", side_effect=_repo_file), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.agent.setup_interactive_worktree",
                   return_value=(Path("/tmp/wt"), "issue-1118-usage-core")), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch(
                 "coord.interactive.launch_human_attended_interactive",
                 return_value=0,
             ) as mock_local, \
             patch("coord.interactive._launch_via_tmux") as mock_remote, \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "assign", _LOCAL_HOST, "api", "1118",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )

        assert result.exit_code == 0, result.output
        mock_local.assert_called_once()
        mock_remote.assert_not_called()


class TestInteractiveWorkEpicGuard:
    """#1314: `coord assign --interactive` must be gated by the same
    epic-target guard as the headless path — a human at the keyboard can
    just as easily dispatch `type="work"` directly against a tracking
    issue's own number as a `claude -p` worker would."""

    def _repo_file_slice_authored(
        self, repo: str, path: str, branch: str | None = None,
    ) -> str:
        """Both gates satisfied (contract.md + manifest) so the refusal in
        this test class is attributable ONLY to the epic-target guard, not
        the #1138 oracle-readiness gate."""
        if path.endswith("contract.md"):
            return "contract body"
        if path.endswith("manifest.yml"):
            return "tests:\n  ms37::a: 1120\n"
        raise RuntimeError("404")

    def test_refuses_work_dispatch_against_epic_issue(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        launched: list[Any] = []

        with patch(
            "coord.github_ops.get_issue",
            return_value={
                "title": "Milestone 38 tracking issue",
                "milestone": {"number": 37},
                "labels": [{"name": "epic"}],
            },
        ), \
             patch(
                 "coord.github_ops.get_repo_file",
                 side_effect=self._repo_file_slice_authored,
             ), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch(
                 "coord.interactive.launch_human_attended_interactive",
                 side_effect=lambda *a, **k: launched.append(True) or 0,
             ) as mock_local, \
             patch("coord.interactive._launch_via_tmux") as mock_remote:
            result = CliRunner().invoke(
                main,
                [
                    "assign", _LOCAL_HOST, "api", "1120",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )

        assert result.exit_code != 0
        assert "epic" in result.output
        mock_local.assert_not_called()
        mock_remote.assert_not_called()
        assert launched == []

    def test_oracle_exempt_label_overrides_epic_guard(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        with patch(
            "coord.github_ops.get_issue",
            return_value={
                "title": "Milestone 38 tracking issue",
                "milestone": {"number": 37},
                "labels": [{"name": "epic"}, {"name": "oracle:exempt"}],
            },
        ), \
             patch(
                 "coord.github_ops.get_repo_file",
                 side_effect=self._repo_file_slice_authored,
             ), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.agent.setup_interactive_worktree",
                   return_value=(Path("/tmp/wt"), "issue-1120-tracking")), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch(
                 "coord.interactive.launch_human_attended_interactive",
                 return_value=0,
             ) as mock_local, \
             patch("coord.interactive._launch_via_tmux") as mock_remote, \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "assign", _LOCAL_HOST, "api", "1120",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )

        assert result.exit_code == 0, result.output
        mock_local.assert_called_once()
        mock_remote.assert_not_called()
