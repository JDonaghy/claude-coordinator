"""Tests for ``coord assign --interactive`` on a REMOTE machine (#494 / #486b).

Covers:
1. Local vs remote detection — target == local hostname → local path;
   target != local hostname → remote path.
2. Remote path builds ``TmuxHost(ssh_target=machine.host)`` and calls
   ``_launch_via_tmux`` with it.
3. Remote argv uses absolute ``~/.local/bin/claude`` (NOT bare ``claude``).
4. ``COORD_ASSIGNMENT_ID`` appears inline in the raw shell command.
5. Worktree setup command (cd, git fetch, git worktree add, cd) is present
   in the raw shell command.
6. Local path is NOT affected: ``launch_human_attended_interactive`` is
   called, NOT ``_launch_via_tmux`` directly.
7. ``--dry-run`` on remote shows remote worktree info; on local shows local.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.interactive import TmuxHost


# ── Config helpers ────────────────────────────────────────────────────────────

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
  - name: remotebox
    host: remotebox.tailnet
    repos: [api]
    repo_paths:
      api: ~/src/api
"""

CONFIG_YAML_REMOTE_ONLY = """\
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
  - name: precision
    host: precision.tailnet
    repos: [api]
    repo_paths:
      api: ~/src/api
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def remote_config_file(tmp_path: Path) -> Path:
    """Config where the current machine is neither 'laptop' nor 'precision'."""
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_REMOTE_ONLY)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok_run(*args: Any, **kwargs: Any) -> MagicMock:
    """Default subprocess.run mock that returns returncode=0."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = ""
    m.stderr = ""
    return m


# ── Local vs remote selection ─────────────────────────────────────────────────


class TestLocalVsRemoteSelection:
    """Verify that the correct launch path is chosen based on hostname."""

    def test_local_machine_uses_launch_human_attended(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When target is the local machine, launch_human_attended_interactive
        is called — NOT _launch_via_tmux directly."""
        launched_local: list[bool] = []

        def _fake_local(argv: Any, briefing: Any, **kw: Any) -> int:
            launched_local.append(True)
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "Fix it"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.agent.setup_interactive_worktree",
                   return_value=(Path("/tmp/wt"), "issue-42-fix-it")), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive.launch_human_attended_interactive",
                   side_effect=_fake_local) as mock_local, \
             patch("coord.interactive._launch_via_tmux") as mock_remote, \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "assign", _LOCAL_HOST, "api", "42",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 0, result.output
        mock_local.assert_called_once()
        mock_remote.assert_not_called()

    def test_remote_machine_uses_tmux_launch(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """When target is NOT the local machine, _launch_via_tmux is called
        with a non-None TmuxHost.ssh_target."""
        captured_host: list[TmuxHost] = []
        captured_raw_cmd: list[str] = []

        def _fake_tmux_launch(
            argv: Any, briefing: Any, session_name: Any, *,
            cwd: Any = None, host: TmuxHost = TmuxHost(None),
            raw_shell_cmd: Any = None,
        ) -> int:
            captured_host.append(host)
            captured_raw_cmd.append(raw_shell_cmd or "")
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "Fix it"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux",
                   side_effect=_fake_tmux_launch) as mock_tmux, \
             patch("coord.interactive.launch_human_attended_interactive") as mock_local, \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "42",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 0, result.output
        mock_local.assert_not_called()
        mock_tmux.assert_called_once()

        # TmuxHost must have the remote machine's host as ssh_target
        assert len(captured_host) == 1
        assert captured_host[0].ssh_target == "precision.tailnet"

    def test_remote_host_is_set_from_config(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """The ssh_target on the TmuxHost is the machine.host from coordinator.yml."""
        captured_host: list[TmuxHost] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_host.append(kw.get("host", TmuxHost(None)))
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "1",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_host, "TmuxHost not captured"
        assert captured_host[0].ssh_target == "laptop.tailnet"


# ── Remote argv uses absolute claude path ─────────────────────────────────────


class TestRemoteClaudePath:
    """argv[0] must be the absolute remote path, not bare 'claude'."""

    def test_absolute_claude_path_in_raw_cmd(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """~/.local/bin/claude appears in the raw shell command sent to tmux."""
        captured_raw: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "7",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw, "raw_shell_cmd not captured"
        assert "~/.local/bin/claude" in captured_raw[0], (
            f"Expected '~/.local/bin/claude' in raw_shell_cmd; got: {captured_raw[0]!r}"
        )

    def test_bare_claude_not_in_argv0_for_remote(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """The raw command must NOT start with bare 'claude' (PATH lookup would
        fail on remote login shell)."""
        captured_raw: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "7",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw
        raw = captured_raw[0]
        # The final command part should contain '~/.local/bin/claude', not
        # start with a bare 'claude' word.
        assert " claude " not in raw or "/.local/bin/claude" in raw, (
            f"bare 'claude' found in raw_shell_cmd: {raw!r}"
        )


# ── COORD_ASSIGNMENT_ID in remote command ─────────────────────────────────────


class TestCoordAssignmentIdInRemoteCmd:
    """COORD_ASSIGNMENT_ID must be set inline in the remote shell command."""

    def test_assignment_id_in_raw_shell_cmd(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        captured_raw: list[str] = []
        captured_aid: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            # Extract the assignment_id from the session name (coord-<aid>)
            if sname.startswith("coord-"):
                captured_aid.append(sname[len("coord-"):])
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "5",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw and captured_aid
        aid = captured_aid[0]
        raw = captured_raw[0]
        assert f"COORD_ASSIGNMENT_ID={aid}" in raw, (
            f"Expected 'COORD_ASSIGNMENT_ID={aid}' in raw_shell_cmd; got: {raw!r}"
        )


# ── Worktree setup in remote command ─────────────────────────────────────────


class TestRemoteWorktreeCommand:
    """The raw_shell_cmd must contain the worktree setup steps."""

    def test_raw_cmd_contains_git_worktree_add(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        captured_raw: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "Implement feature"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "99",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw
        raw = captured_raw[0]
        assert "git worktree add" in raw, (
            f"Expected 'git worktree add' in raw_shell_cmd; got: {raw!r}"
        )
        assert "git fetch" in raw, (
            f"Expected 'git fetch' in raw_shell_cmd; got: {raw!r}"
        )

    def test_raw_cmd_uses_issue_branch_name(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """Branch name mirrors setup_interactive_worktree: issue-N-<slug>."""
        captured_raw: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            return 0

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Add logging"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "77",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw
        raw = captured_raw[0]
        # Branch should follow issue-N-slug pattern
        assert "issue-77-" in raw, (
            f"Expected 'issue-77-<slug>' in raw_shell_cmd; got: {raw!r}"
        )

    def test_raw_cmd_worktree_path_contains_assignment_id(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """The worktree path is ~/.coord/worktrees/<assignment_id>."""
        captured_raw: list[str] = []
        captured_aid: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            if sname.startswith("coord-"):
                captured_aid.append(sname[len("coord-"):])
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "3",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw and captured_aid
        aid = captured_aid[0]
        raw = captured_raw[0]
        assert f".coord/worktrees/{aid}" in raw, (
            f"Expected '.coord/worktrees/{aid}' in raw_shell_cmd; got: {raw!r}"
        )

    def test_raw_cmd_uses_home_not_unexpanded_tilde(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """Paths in the worktree command use $HOME (not '~') so that
        tilde expansion works when the command passes through single-quoting."""
        captured_raw: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "3",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw
        raw = captured_raw[0]
        # The .coord/worktrees path should come from $HOME, not literal ~
        assert "$HOME/.coord/worktrees" in raw, (
            f"Expected '$HOME/.coord/worktrees' in raw_shell_cmd; got: {raw!r}"
        )


# ── Remote dry-run ────────────────────────────────────────────────────────────


class TestRemoteDryRun:
    def test_dry_run_remote_shows_remote_worktree_info(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Add feature X"}):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "42",
                    "--config", str(remote_config_file),
                    "--interactive", "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output
        # Should mention the remote machine's host or "remote"
        assert "precision.tailnet" in result.output or "remote" in result.output.lower()

    def test_dry_run_remote_shows_branch_name(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Add feature X"}):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "42",
                    "--config", str(remote_config_file),
                    "--interactive", "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        # Branch name should contain the issue number
        assert "issue-42-" in result.output


# ── Session-still-alive after remote launch ───────────────────────────────────


class TestRemoteSessionAlive:
    """When the remote session is still alive after attach returns, print a
    reattach hint referencing ssh -t ... tmux attach-session."""

    def test_still_alive_shows_ssh_reattach_hint(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            # Simulate user detaching (attach returns 0 but session alive)
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux",
                   side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=True):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "1",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "ssh -t" in result.output or "tmux attach-session" in result.output, (
            f"Expected reattach hint in output; got: {result.output!r}"
        )
        assert "precision.tailnet" in result.output, (
            f"Expected remote host in reattach hint; got: {result.output!r}"
        )

    def test_session_ended_shows_finalize_note(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """When the remote session ends (not alive), the output should mention
        that remote finalize is deferred."""

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux",
                   side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "1",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "remote session ended" in result.output or "report-result" in result.output


# ── Launch failure handling ───────────────────────────────────────────────────


class TestRemoteLaunchFailure:
    def test_tmux_launch_failure_exits_nonzero(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """When _launch_via_tmux returns None (session creation failed),
        assign should exit with a non-zero code and print an error."""

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", return_value=None), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "1",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code != 0
        assert "error" in result.output.lower() or "could not create" in result.output


# ── _launch_via_tmux raw_shell_cmd parameter ─────────────────────────────────


class TestLaunchViaTmuxRawShellCmd:
    """Unit tests for the new raw_shell_cmd parameter in _launch_via_tmux."""

    def test_raw_shell_cmd_used_instead_of_argv(self) -> None:
        """When raw_shell_cmd is provided, it is used as the tmux command
        instead of shlex.join(argv)."""
        from coord.interactive import _launch_via_tmux

        captured_create_cmds: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            captured_create_cmds.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        raw = "cd ~/repo && COORD_ASSIGNMENT_ID=abc ~/bin/claude --flag"
        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive._inject_briefing_into_tmux_session",
                   return_value=True):
            _launch_via_tmux(
                ["claude", "--other"],
                "briefing",
                "coord-abc",
                raw_shell_cmd=raw,
            )

        # Find the new-session call
        create_calls = [c for c in captured_create_cmds if "new-session" in c]
        assert create_calls, "new-session not called"
        create_cmd = create_calls[0]
        # For local (TmuxHost(None)): shell_cmd is raw, which is the last element
        assert raw in create_cmd, (
            f"Expected raw_shell_cmd in create_cmd; got: {create_cmd!r}"
        )
        # argv[0] 'claude' must NOT appear as the command (raw overrides argv)
        # The raw command should be there, not shlex.join(['claude', '--other'])
        assert "claude --other" not in " ".join(create_cmd), (
            "shlex.join(argv) was used instead of raw_shell_cmd"
        )

    def test_no_raw_shell_cmd_falls_back_to_argv(self) -> None:
        """Without raw_shell_cmd, shlex.join(argv) is used as before."""
        import shlex
        from coord.interactive import _launch_via_tmux

        captured_create_cmds: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            captured_create_cmds.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive._inject_briefing_into_tmux_session",
                   return_value=True):
            _launch_via_tmux(["echo", "hello world"], "b", "coord-test")

        create_calls = [c for c in captured_create_cmds if "new-session" in c]
        assert create_calls
        create_cmd = create_calls[0]
        expected_shell_cmd = shlex.join(["echo", "hello world"])
        assert expected_shell_cmd in create_cmd, (
            f"Expected shlex.join output {expected_shell_cmd!r} in {create_cmd!r}"
        )

    def test_remote_host_sends_single_ssh_string(self) -> None:
        """For remote hosts, the create command is ['ssh', host, single_string]
        — not ['ssh', host, 'tmux', 'new-session', ..., cmd_split_by_ssh]."""
        from coord.interactive import _launch_via_tmux

        captured_cmds: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            captured_cmds.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        raw = "cd ~/repo && echo hello"
        rhost = TmuxHost("myhost")
        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive._inject_briefing_into_tmux_session",
                   return_value=True):
            _launch_via_tmux(
                ["claude"],
                "b",
                "coord-xyz",
                host=rhost,
                raw_shell_cmd=raw,
            )

        create_calls = [c for c in captured_cmds if "new-session" in " ".join(c)]
        assert create_calls, f"new-session not found in calls: {captured_cmds!r}"
        create_cmd = create_calls[0]

        # Remote: must be exactly ['ssh', 'myhost', '<tmux_cmd_string>']
        assert create_cmd[0] == "ssh", f"Expected 'ssh', got {create_cmd[0]!r}"
        assert create_cmd[1] == "myhost", f"Expected 'myhost', got {create_cmd[1]!r}"
        # Third element is the single shell command string
        assert len(create_cmd) == 3, (
            f"Expected exactly 3 elements ['ssh', host, cmd_str]; got {create_cmd!r}"
        )
        # The shell command string must contain both 'tmux new-session' and
        # the quoted raw_shell_cmd.
        cmd_str = create_cmd[2]
        assert "tmux" in cmd_str and "new-session" in cmd_str, (
            f"tmux new-session not in cmd_str: {cmd_str!r}"
        )

    def test_remote_raw_cmd_quoted_in_single_string(self) -> None:
        """The raw_shell_cmd must appear inside shlex.quote() in the
        single SSH command string so the remote shell treats it as one arg."""
        import shlex
        from coord.interactive import _launch_via_tmux

        captured_cmds: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            captured_cmds.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        raw = "cd $HOME/repo && COORD_ASSIGNMENT_ID=abc $HOME/.local/bin/claude --flag"
        rhost = TmuxHost("myhost")
        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive._inject_briefing_into_tmux_session",
                   return_value=True):
            _launch_via_tmux(["claude"], "b", "coord-xyz", host=rhost, raw_shell_cmd=raw)

        create_calls = [c for c in captured_cmds if "new-session" in " ".join(c)]
        assert create_calls
        cmd_str = create_calls[0][2]

        # shlex.quote(raw) should be present in the single ssh cmd string
        quoted_raw = shlex.quote(raw)
        assert quoted_raw in cmd_str, (
            f"shlex.quote(raw_shell_cmd) not found in ssh cmd string.\n"
            f"Expected: {quoted_raw!r}\nIn: {cmd_str!r}"
        )
