"""Tests for coord done — repo-level housekeeping commands."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import Config, HooksConfig
from coord.models import Machine, Repo


def _make_config(
    *,
    hostname: str = "mybox",
    repos: list[Repo] | None = None,
    machines: list[Machine] | None = None,
) -> Config:
    if repos is None:
        repos = []
    if machines is None:
        machines = []
    return Config(
        repos=repos,
        machines=machines,
        hooks=HooksConfig(),
    )


def _repo(name: str, housekeeping: list[str] | None = None) -> Repo:
    from coord.config import DEFAULT_DENY_COMMANDS
    from coord.models import WorkerPermissionsConfig

    return Repo(
        name=name,
        github=f"acme/{name}",
        housekeeping=housekeeping or [],
        worker_permissions=WorkerPermissionsConfig(deny=list(DEFAULT_DENY_COMMANDS)),
    )


def _machine(name: str, repo_paths: dict[str, str] | None = None) -> Machine:
    return Machine(
        name=name,
        host=f"{name}.tailnet",
        repos=list((repo_paths or {}).keys()),
        repo_paths=repo_paths or {},
    )


# ---------------------------------------------------------------------------
# Helpers to build a minimal coordinator.yml on disk
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, yaml_text: str) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(yaml_text)
    return p


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDoneHousekeeping:
    """coord done runs housekeeping commands for repos with local paths."""

    def test_runs_housekeeping_commands(self, tmp_path: Path, coord_db) -> None:
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()

        cfg_path = _write_config(
            tmp_path,
            f"repos:\n"
            f"  - name: myrepo\n"
            f"    github: acme/myrepo\n"
            f"    housekeeping:\n"
            f"      - echo hello\n"
            f"      - echo world\n"
            f"machines:\n"
            f"  - name: mybox\n"
            f"    host: mybox.tailnet\n"
            f"    repos: [myrepo]\n"
            f"    repo_paths:\n"
            f"      myrepo: {repo_path}\n",
        )

        runner = CliRunner()
        with (
            patch("coord.cli.socket.gethostname", return_value="mybox"),
            patch("coord.state.load_board", return_value=None),
            patch("coord.state.build_board") as mock_build,
            patch("coord.state.save_board"),
            patch("coord.cli.subprocess.run") as mock_run,
        ):
            from coord.models import Board

            mock_build.return_value = Board()
            mock_run.return_value = MagicMock(returncode=0, stderr="")

            result = runner.invoke(main, ["done", "--config", str(cfg_path)])

        assert result.exit_code == 0, result.output
        # subprocess.run called: git pull + 2 housekeeping commands
        assert mock_run.call_count == 3
        calls = mock_run.call_args_list
        assert calls[0][0][0] == ["git", "pull", "--ff-only"]
        assert calls[1][0][0] == "echo hello"
        assert calls[2][0][0] == "echo world"

    def test_skips_repos_without_housekeeping(self, tmp_path: Path, coord_db) -> None:
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()

        cfg_path = _write_config(
            tmp_path,
            f"repos:\n"
            f"  - name: myrepo\n"
            f"    github: acme/myrepo\n"
            f"machines:\n"
            f"  - name: mybox\n"
            f"    host: mybox.tailnet\n"
            f"    repos: [myrepo]\n"
            f"    repo_paths:\n"
            f"      myrepo: {repo_path}\n",
        )

        runner = CliRunner()
        with (
            patch("coord.cli.socket.gethostname", return_value="mybox"),
            patch("coord.state.load_board", return_value=None),
            patch("coord.state.build_board") as mock_build,
            patch("coord.state.save_board"),
            patch("coord.cli.subprocess.run") as mock_run,
        ):
            from coord.models import Board

            mock_build.return_value = Board()

            result = runner.invoke(main, ["done", "--config", str(cfg_path)])

        assert result.exit_code == 0, result.output
        mock_run.assert_not_called()

    def test_skips_repos_without_local_path(self, tmp_path: Path, coord_db) -> None:
        cfg_path = _write_config(
            tmp_path,
            "repos:\n"
            "  - name: myrepo\n"
            "    github: acme/myrepo\n"
            "    housekeeping:\n"
            "      - echo hi\n"
            "machines:\n"
            "  - name: mybox\n"
            "    host: mybox.tailnet\n"
            "    repos: [myrepo]\n",
            # Note: no repo_paths configured
        )

        runner = CliRunner()
        with (
            patch("coord.cli.socket.gethostname", return_value="mybox"),
            patch("coord.state.load_board", return_value=None),
            patch("coord.state.build_board") as mock_build,
            patch("coord.state.save_board"),
            patch("coord.cli.subprocess.run") as mock_run,
        ):
            from coord.models import Board

            mock_build.return_value = Board()

            result = runner.invoke(main, ["done", "--config", str(cfg_path)])

        assert result.exit_code == 0, result.output
        assert "no local path configured" in result.output
        mock_run.assert_not_called()

    def test_failed_housekeeping_does_not_block_other_repos(self, tmp_path: Path, coord_db) -> None:
        repo_a = tmp_path / "repo_a"
        repo_b = tmp_path / "repo_b"
        repo_a.mkdir()
        repo_b.mkdir()

        cfg_path = _write_config(
            tmp_path,
            f"repos:\n"
            f"  - name: repo_a\n"
            f"    github: acme/repo_a\n"
            f"    housekeeping:\n"
            f"      - failing-cmd\n"
            f"  - name: repo_b\n"
            f"    github: acme/repo_b\n"
            f"    housekeeping:\n"
            f"      - ok-cmd\n"
            f"machines:\n"
            f"  - name: mybox\n"
            f"    host: mybox.tailnet\n"
            f"    repos: [repo_a, repo_b]\n"
            f"    repo_paths:\n"
            f"      repo_a: {repo_a}\n"
            f"      repo_b: {repo_b}\n",
        )

        runner = CliRunner()

        def _run_side_effect(cmd, **kwargs):
            m = MagicMock()
            if cmd == ["git", "pull", "--ff-only"]:
                m.returncode = 0
                m.stderr = ""
            elif "failing-cmd" in str(cmd):
                m.returncode = 1
                m.stderr = "command not found"
            else:
                m.returncode = 0
                m.stderr = ""
            return m

        with (
            patch("coord.cli.socket.gethostname", return_value="mybox"),
            patch("coord.state.load_board", return_value=None),
            patch("coord.state.build_board") as mock_build,
            patch("coord.state.save_board"),
            patch("coord.cli.subprocess.run", side_effect=_run_side_effect) as mock_run,
        ):
            from coord.models import Board

            mock_build.return_value = Board()

            result = runner.invoke(main, ["done", "--config", str(cfg_path)])

        assert result.exit_code == 0, result.output
        # Both repos processed: 2 git pulls + 2 housekeeping commands = 4 calls
        assert mock_run.call_count == 4
        assert "Session ended" in result.output

    def test_git_pull_attempted_before_housekeeping(self, tmp_path: Path, coord_db) -> None:
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()

        cfg_path = _write_config(
            tmp_path,
            f"repos:\n"
            f"  - name: myrepo\n"
            f"    github: acme/myrepo\n"
            f"    housekeeping:\n"
            f"      - make install\n"
            f"machines:\n"
            f"  - name: mybox\n"
            f"    host: mybox.tailnet\n"
            f"    repos: [myrepo]\n"
            f"    repo_paths:\n"
            f"      myrepo: {repo_path}\n",
        )

        call_order: list[str] = []

        def _run_side_effect(cmd, **kwargs):
            if cmd == ["git", "pull", "--ff-only"]:
                call_order.append("git_pull")
            else:
                call_order.append(f"cmd:{cmd}")
            return MagicMock(returncode=0, stderr="")

        runner = CliRunner()
        with (
            patch("coord.cli.socket.gethostname", return_value="mybox"),
            patch("coord.state.load_board", return_value=None),
            patch("coord.state.build_board") as mock_build,
            patch("coord.state.save_board"),
            patch("coord.cli.subprocess.run", side_effect=_run_side_effect),
        ):
            from coord.models import Board

            mock_build.return_value = Board()
            runner.invoke(main, ["done", "--config", str(cfg_path)])

        assert call_order[0] == "git_pull", "git pull must be called before housekeeping commands"
        assert len(call_order) == 2

    def test_git_pull_failure_continues_to_housekeeping(self, tmp_path: Path, coord_db) -> None:
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()

        cfg_path = _write_config(
            tmp_path,
            f"repos:\n"
            f"  - name: myrepo\n"
            f"    github: acme/myrepo\n"
            f"    housekeeping:\n"
            f"      - make install\n"
            f"machines:\n"
            f"  - name: mybox\n"
            f"    host: mybox.tailnet\n"
            f"    repos: [myrepo]\n"
            f"    repo_paths:\n"
            f"      myrepo: {repo_path}\n",
        )

        def _run_side_effect(cmd, **kwargs):
            if cmd == ["git", "pull", "--ff-only"]:
                raise subprocess.CalledProcessError(1, cmd, stderr="diverged")
            return MagicMock(returncode=0, stderr="")

        runner = CliRunner()
        with (
            patch("coord.cli.socket.gethostname", return_value="mybox"),
            patch("coord.state.load_board", return_value=None),
            patch("coord.state.build_board") as mock_build,
            patch("coord.state.save_board"),
            patch("coord.cli.subprocess.run", side_effect=_run_side_effect) as mock_run,
        ):
            from coord.models import Board

            mock_build.return_value = Board()

            result = runner.invoke(main, ["done", "--config", str(cfg_path)])

        assert result.exit_code == 0, result.output
        # git pull + housekeeping command both attempted
        assert mock_run.call_count == 2
        assert "git pull failed" in result.output

    def test_unknown_machine_skips_housekeeping(self, tmp_path: Path, coord_db) -> None:
        cfg_path = _write_config(
            tmp_path,
            "repos:\n"
            "  - name: myrepo\n"
            "    github: acme/myrepo\n"
            "    housekeeping:\n"
            "      - echo hi\n"
            "machines:\n"
            "  - name: other-box\n"
            "    host: other-box.tailnet\n"
            "    repos: [myrepo]\n",
        )

        runner = CliRunner()
        with (
            patch("coord.cli.socket.gethostname", return_value="completely-unknown"),
            patch("coord.state.load_board", return_value=None),
            patch("coord.state.build_board") as mock_build,
            patch("coord.state.save_board"),
            patch("coord.cli.subprocess.run") as mock_run,
        ):
            from coord.models import Board

            mock_build.return_value = Board()

            result = runner.invoke(main, ["done", "--config", str(cfg_path)])

        assert result.exit_code == 0, result.output
        assert "Could not determine local machine" in result.output
        mock_run.assert_not_called()

    def test_nonexistent_repo_path_skipped(self, tmp_path: Path, coord_db) -> None:
        cfg_path = _write_config(
            tmp_path,
            f"repos:\n"
            f"  - name: myrepo\n"
            f"    github: acme/myrepo\n"
            f"    housekeeping:\n"
            f"      - echo hi\n"
            f"machines:\n"
            f"  - name: mybox\n"
            f"    host: mybox.tailnet\n"
            f"    repos: [myrepo]\n"
            f"    repo_paths:\n"
            f"      myrepo: {tmp_path}/does-not-exist\n",
        )

        runner = CliRunner()
        with (
            patch("coord.cli.socket.gethostname", return_value="mybox"),
            patch("coord.state.load_board", return_value=None),
            patch("coord.state.build_board") as mock_build,
            patch("coord.state.save_board"),
            patch("coord.cli.subprocess.run") as mock_run,
        ):
            from coord.models import Board

            mock_build.return_value = Board()

            result = runner.invoke(main, ["done", "--config", str(cfg_path)])

        assert result.exit_code == 0, result.output
        assert "does not exist" in result.output
        mock_run.assert_not_called()
