"""Tests for the coord init interactive setup wizard."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import load as load_config


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def fake_git_repo(tmp_path: Path) -> Path:
    """Create a fake git repo directory with a .git dir."""
    repo = tmp_path / "src" / "my-project"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    return repo


def _mock_subprocess_run(
    remote_url: str = "git@github.com:acme/my-project.git",
    default_branch_ref: str = "refs/remotes/origin/main",
    fail_pkg_config: bool = True,
):
    """Return a side_effect function that handles known git/pkg-config calls."""

    def side_effect(cmd, **kwargs):
        cmd_str = " ".join(str(c) for c in cmd)

        # pkg-config --exists gtk4
        if "pkg-config" in cmd_str:
            if fail_pkg_config:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0)

        # git remote get-url origin
        if "remote" in cmd_str and "get-url" in cmd_str:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=remote_url + "\n", stderr=""
            )

        # git symbolic-ref refs/remotes/origin/HEAD
        if "symbolic-ref" in cmd_str:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=default_branch_ref + "\n", stderr=""
            )

        # Default: fail
        raise subprocess.CalledProcessError(1, cmd)

    return side_effect


class TestInitHappyPath:
    """Happy path: single machine with one repo discovered."""

    def test_generates_valid_config(self, runner: CliRunner, tmp_path: Path):
        """Full init flow: detect machine, discover repo, configure, validate."""
        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "testbox",       # Machine name
                "python",        # Capabilities
                "all",           # Which repos
                "main",          # Default branch
                # No dependencies prompt (only 1 repo)
                "",              # Build command (skip)
                "",              # Test command (skip)
                "N",             # Add another machine? No
                "2",             # Max workers
                "30",            # Stagger seconds
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run()
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Exit code {result.exit_code}, output:\n{result.output}"
        assert "Created coordinator.yml" in result.output
        assert "1 repo(s)" in result.output
        assert "1 machine(s)" in result.output

        # Validate the generated config parses
        config_path = tmp_path / "coordinator.yml"
        assert config_path.exists()
        cfg = load_config(config_path)
        assert len(cfg.repos) == 1
        assert cfg.repos[0].name == "my-project"
        assert cfg.repos[0].github == "acme/my-project"
        assert len(cfg.machines) == 1
        assert cfg.machines[0].name == "testbox"
        assert "python" in cfg.machines[0].capabilities

    def test_with_build_and_test_commands(self, runner: CliRunner, tmp_path: Path):
        """Repos can have build and test commands."""
        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "testbox",             # Machine name
                "python",              # Capabilities
                "all",                 # Which repos
                "main",                # Default branch
                # No dependencies prompt (only 1 repo)
                "pip install -e .",    # Build command
                "pytest",              # Test command
                "N",                   # Add another machine?
                "2",                   # Max workers
                "30",                  # Stagger seconds
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run()
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        cfg = load_config(tmp_path / "coordinator.yml")
        assert cfg.repos[0].build_command == "pip install -e ."
        assert cfg.repos[0].test_command == "pytest"


class TestExistingConfig:
    """Test overwrite/decline flows for existing coordinator.yml."""

    def test_decline_overwrite(self, runner: CliRunner, tmp_path: Path):
        """User says N to overwrite — file stays unchanged."""
        config_path = tmp_path / "coordinator.yml"
        original = "# original content\n"
        config_path.write_text(original)

        with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
            result = runner.invoke(main, ["init"], input="N\n")

        assert result.exit_code == 0
        assert "Aborted" in result.output
        assert config_path.read_text() == original

    def test_accept_overwrite(self, runner: CliRunner, tmp_path: Path):
        """User says y to overwrite — new file is written."""
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text("# old\n")

        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "y",             # Overwrite
                "testbox",       # Machine name
                "python",        # Capabilities
                "all",           # Which repos
                "main",          # Default branch
                # No dependencies prompt (only 1 repo)
                "",              # Build command
                "",              # Test command
                "N",             # Add another machine?
                "2",             # Max workers
                "30",            # Stagger seconds
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run()
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        assert "Created coordinator.yml" in result.output
        assert config_path.read_text() != "# old\n"


class TestCapabilityDetection:
    """Test that capabilities are detected based on available tools."""

    def test_detects_rust_python_docker(self, runner: CliRunner, tmp_path: Path):
        """When cargo, python3, docker are on PATH, they show up as capabilities."""
        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "testbox",                      # Machine name
                "rust,python,docker",           # Accept detected capabilities
                "all",                          # Which repos
                "main",                         # Default branch
                # No dependencies prompt (only 1 repo)
                "",                             # Build command
                "",                             # Test command
                "N",                            # Add another machine?
                "2",                            # Max workers
                "30",                           # Stagger seconds
            ]
        )

        def which_side_effect(name):
            found = {"cargo": "/usr/bin/cargo", "python3": "/usr/bin/python3", "docker": "/usr/bin/docker"}
            return found.get(name)

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = which_side_effect
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run(fail_pkg_config=True)
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        assert "Detected capabilities: rust, python, docker" in result.output

    def test_detects_gtk_via_pkg_config(self, runner: CliRunner, tmp_path: Path):
        """When pkg-config --exists gtk4 succeeds, gtk is detected."""
        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "testbox",            # Machine name
                "gtk,python",         # Accept detected capabilities
                "all",                # Which repos
                "main",               # Default branch
                # No dependencies prompt (only 1 repo)
                "",                   # Build command
                "",                   # Test command
                "N",                  # Add another machine?
                "2",                  # Max workers
                "30",                 # Stagger seconds
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run(fail_pkg_config=False)
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        assert "Detected capabilities: gtk, python" in result.output

    def test_no_capabilities_detected(self, runner: CliRunner, tmp_path: Path):
        """When nothing is on PATH, user can still type capabilities manually."""
        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "testbox",         # Machine name
                "custom",          # User types custom capability
                "all",             # Which repos
                "main",            # Default branch
                # No dependencies prompt (only 1 repo)
                "",                # Build command
                "",                # Test command
                "N",               # Add another machine?
                "2",               # Max workers
                "30",              # Stagger seconds
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which", return_value=None):
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run()
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        assert "No capabilities auto-detected" in result.output
        cfg = load_config(tmp_path / "coordinator.yml")
        assert cfg.machines[0].capabilities == ["custom"]


class TestNoReposFound:
    """Test graceful handling when no repos are found."""

    def test_no_repos_writes_minimal_config(self, runner: CliRunner, tmp_path: Path):
        """When no git repos are found, write a minimal config."""
        # No .git in cwd, no ~/src/
        input_lines = "\n".join(
            [
                "testbox",       # Machine name
                "python",        # Capabilities
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run()
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        assert "No git repos" in result.output
        assert "0 repos" in result.output
        assert (tmp_path / "coordinator.yml").exists()


class TestGeneratedConfigValidates:
    """Ensure the generated YAML round-trips through coord.config.load()."""

    def test_single_repo_validates(self, runner: CliRunner, tmp_path: Path):
        """Generated config for a single repo passes load() validation."""
        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "testbox",
                "python",
                "all",
                "main",
                # No dependencies prompt (only 1 repo)
                "make build",
                "make test",
                "N",
                "3",
                "15",
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run()
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"

        cfg = load_config(tmp_path / "coordinator.yml")
        assert cfg.repos[0].name == "my-project"
        assert cfg.repos[0].build_command == "make build"
        assert cfg.repos[0].test_command == "make test"
        assert cfg.concurrency.max_workers == 3
        assert cfg.concurrency.stagger_seconds == 15

    def test_multiple_repos_with_deps_validates(self, runner: CliRunner, tmp_path: Path):
        """Generated config for multiple repos with dependencies validates."""
        # Create two repos
        for name in ("api", "shared"):
            repo_dir = tmp_path / "src" / name
            repo_dir.mkdir(parents=True)
            (repo_dir / ".git").mkdir()

        call_count = {"n": 0}

        def multi_repo_subprocess(cmd, **kwargs):
            cmd_str = " ".join(str(c) for c in cmd)

            if "pkg-config" in cmd_str:
                raise subprocess.CalledProcessError(1, cmd)

            if "remote" in cmd_str and "get-url" in cmd_str:
                # Figure out which repo based on the path in the command
                if "api" in cmd_str:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout="git@github.com:acme/api.git\n", stderr=""
                    )
                else:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout="git@github.com:acme/shared.git\n", stderr=""
                    )

            if "symbolic-ref" in cmd_str:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="refs/remotes/origin/main\n", stderr=""
                )

            raise subprocess.CalledProcessError(1, cmd)

        input_lines = "\n".join(
            [
                "testbox",       # Machine name
                "python",        # Capabilities
                "all",           # Which repos
                # First repo (api)
                "main",          # Default branch
                "shared",        # Dependencies
                "",              # Build command
                "",              # Test command
                # Second repo (shared)
                "main",          # Default branch
                "none",          # Dependencies
                "",              # Build command
                "",              # Test command
                "N",             # Add another machine?
                "2",             # Max workers
                "30",            # Stagger seconds
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = multi_repo_subprocess
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        assert "2 repo(s)" in result.output

        cfg = load_config(tmp_path / "coordinator.yml")
        assert len(cfg.repos) == 2
        repo_names = [r.name for r in cfg.repos]
        assert "api" in repo_names
        assert "shared" in repo_names
        # Check that api depends on shared
        api_repo = cfg.repo("api")
        assert api_repo is not None
        assert "shared" in api_repo.depends_on


class TestMultipleMachines:
    """Test adding additional remote machines."""

    def test_add_second_machine(self, runner: CliRunner, tmp_path: Path):
        """User adds a second machine during init."""
        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "testbox",           # Machine name
                "python",            # Capabilities
                "all",               # Which repos
                "main",              # Default branch
                # No dependencies prompt (only 1 repo)
                "",                  # Build command
                "",                  # Test command
                "y",                 # Add another machine? Yes
                "server",            # Second machine name
                "server.tailnet",    # Tailscale hostname
                "python,docker",     # Capabilities
                "all",               # Which repos
                "~/src/my-project",  # Path to repo on server
                "N",                 # Add another machine? No
                "2",                 # Max workers
                "30",                # Stagger seconds
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run()
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            with patch("coord.cli.httpx.get") as mock_httpx:
                                mock_httpx.return_value = MagicMock(status_code=200)
                                result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        assert "2 machine(s)" in result.output
        assert "reachable" in result.output

        cfg = load_config(tmp_path / "coordinator.yml")
        assert len(cfg.machines) == 2
        assert cfg.machines[1].name == "server"
        assert cfg.machines[1].host == "server.tailnet"
        assert "docker" in cfg.machines[1].capabilities

    def test_unreachable_machine(self, runner: CliRunner, tmp_path: Path):
        """Second machine is unreachable — reported but init still succeeds."""
        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "testbox",           # Machine name
                "python",            # Capabilities
                "all",               # Which repos
                "main",              # Default branch
                # No dependencies prompt (only 1 repo)
                "",                  # Build command
                "",                  # Test command
                "y",                 # Add another machine?
                "server",            # Second machine name
                "server.tailnet",    # Tailscale hostname
                "python",            # Capabilities
                "all",               # Which repos
                "~/src/my-project",  # Path to repo
                "N",                 # Add another machine?
                "2",                 # Max workers
                "30",                # Stagger seconds
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run()
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            with patch("coord.cli.httpx.get") as mock_httpx:
                                mock_httpx.side_effect = Exception("connection refused")
                                result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        assert "not reachable" in result.output


class TestGitHubRemoteParsing:
    """Test _parse_github_remote with various URL formats."""

    def test_ssh_with_git_suffix(self):
        from coord.cli import _parse_github_remote

        assert _parse_github_remote("git@github.com:acme/api.git") == "acme/api"

    def test_ssh_without_git_suffix(self):
        from coord.cli import _parse_github_remote

        assert _parse_github_remote("git@github.com:acme/api") == "acme/api"

    def test_https_with_git_suffix(self):
        from coord.cli import _parse_github_remote

        assert _parse_github_remote("https://github.com/acme/api.git") == "acme/api"

    def test_https_without_git_suffix(self):
        from coord.cli import _parse_github_remote

        assert _parse_github_remote("https://github.com/acme/api") == "acme/api"

    def test_non_github_returns_none(self):
        from coord.cli import _parse_github_remote

        assert _parse_github_remote("git@gitlab.com:acme/api.git") is None

    def test_invalid_url_returns_none(self):
        from coord.cli import _parse_github_remote

        assert _parse_github_remote("not-a-url") is None


class TestHttpsRemoteDiscovery:
    """Test that HTTPS remotes are discovered correctly."""

    def test_https_remote(self, runner: CliRunner, tmp_path: Path):
        """Repos with HTTPS remotes are discovered."""
        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "testbox",
                "python",
                "all",
                "main",
                # No dependencies prompt (only 1 repo)
                "",
                "",
                "N",
                "2",
                "30",
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run(
                        remote_url="https://github.com/acme/my-project.git"
                    )
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        cfg = load_config(tmp_path / "coordinator.yml")
        assert cfg.repos[0].github == "acme/my-project"


class TestDevelopBranch:
    """Test detection of non-main default branches."""

    def test_develop_branch_detected(self, runner: CliRunner, tmp_path: Path):
        """When origin/HEAD points to develop, that's the default."""
        repo_dir = tmp_path / "src" / "my-project"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".git").mkdir()

        input_lines = "\n".join(
            [
                "testbox",
                "python",
                "all",
                "develop",        # Accept the detected develop branch
                # No dependencies prompt (only 1 repo)
                "",
                "",
                "N",
                "2",
                "30",
            ]
        )

        with patch("coord.cli.socket.gethostname", return_value="testbox"):
            with patch("coord.cli.shutil.which") as mock_which:
                mock_which.side_effect = lambda x: "/usr/bin/python3" if x == "python3" else None
                with patch("coord.cli.subprocess.run") as mock_run:
                    mock_run.side_effect = _mock_subprocess_run(
                        default_branch_ref="refs/remotes/origin/develop"
                    )
                    with patch("coord.cli.os.getcwd", return_value=str(tmp_path)):
                        with patch("coord.cli.Path.home", return_value=tmp_path):
                            result = runner.invoke(main, ["init"], input=input_lines)

        assert result.exit_code == 0, f"Output:\n{result.output}"
        cfg = load_config(tmp_path / "coordinator.yml")
        assert cfg.repos[0].default_branch == "develop"
