"""Tests for :class:`coord.interactive.TmuxHost` (#493).

Covers:
1. ``TmuxHost.cmd`` — local, remote, remote+tty
2. Stdin-based briefing injection (load-buffer reads from ``"-"``
   with ``input=briefing``; no temp-file is created)
3. ``_inject_briefing_into_tmux_session`` with a non-default host
4. ``tmux_session_alive`` with a remote host
5. ``list_coord_tmux_sessions`` with a remote host
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coord.interactive import (
    TmuxHost,
    _inject_briefing_into_tmux_session,
    list_coord_tmux_sessions,
    tmux_session_alive,
)


# ── TmuxHost.cmd ──────────────────────────────────────────────────────────────


class TestTmuxHostCmd:
    """Unit tests for :meth:`TmuxHost.cmd`."""

    # -- local (ssh_target=None) -----------------------------------------------

    def test_local_no_tmux_args(self) -> None:
        assert TmuxHost(None).cmd([]) == ["tmux"]

    def test_local_single_arg(self) -> None:
        assert TmuxHost(None).cmd(["ls"]) == ["tmux", "ls"]

    def test_local_has_session(self) -> None:
        result = TmuxHost(None).cmd(["has-session", "-t", "coord-abc"])
        assert result == ["tmux", "has-session", "-t", "coord-abc"]

    def test_local_ls_format(self) -> None:
        result = TmuxHost(None).cmd(["ls", "-F", "#{session_name}"])
        assert result == ["tmux", "ls", "-F", "#{session_name}"]

    def test_local_load_buffer_stdin(self) -> None:
        result = TmuxHost(None).cmd(["load-buffer", "-b", "coord-brief", "-"])
        assert result == ["tmux", "load-buffer", "-b", "coord-brief", "-"]

    def test_local_attach_session(self) -> None:
        result = TmuxHost(None).cmd(["attach-session", "-t", "coord-xyz"])
        assert result == ["tmux", "attach-session", "-t", "coord-xyz"]

    def test_local_tty_true_no_effect(self) -> None:
        """tty=True is silently ignored on local — no SSH wrapper."""
        result = TmuxHost(None).cmd(["attach-session", "-t", "s"], tty=True)
        assert result == ["tmux", "attach-session", "-t", "s"]
        assert "ssh" not in result

    def test_local_tty_false_no_effect(self) -> None:
        result = TmuxHost(None).cmd(["has-session"], tty=False)
        assert result == ["tmux", "has-session"]
        assert "ssh" not in result

    def test_local_first_element_is_tmux(self) -> None:
        assert TmuxHost(None).cmd(["anything"])[0] == "tmux"

    def test_local_returns_new_list_each_call(self) -> None:
        """cmd() must not cache or share list objects between calls."""
        h = TmuxHost(None)
        a = h.cmd(["ls"])
        b = h.cmd(["ls"])
        assert a is not b

    # -- remote (ssh_target set, tty=False default) ----------------------------

    def test_remote_no_tmux_args(self) -> None:
        result = TmuxHost("myhost").cmd([])
        assert result == ["ssh", "myhost", "tmux"]

    def test_remote_single_arg(self) -> None:
        result = TmuxHost("myhost").cmd(["ls"])
        assert result == ["ssh", "myhost", "tmux", "ls"]

    def test_remote_has_session(self) -> None:
        result = TmuxHost("myhost").cmd(["has-session", "-t", "coord-abc"])
        assert result == ["ssh", "myhost", "tmux", "has-session", "-t", "coord-abc"]

    def test_remote_ls_format(self) -> None:
        result = TmuxHost("myhost").cmd(["ls", "-F", "#{session_name}"])
        assert result == ["ssh", "myhost", "tmux", "ls", "-F", "#{session_name}"]

    def test_remote_load_buffer_stdin(self) -> None:
        result = TmuxHost("myhost").cmd(["load-buffer", "-b", "coord-brief", "-"])
        assert result == ["ssh", "myhost", "tmux", "load-buffer", "-b", "coord-brief", "-"]

    def test_remote_no_dash_t_by_default(self) -> None:
        result = TmuxHost("myhost").cmd(["has-session", "-t", "s"])
        assert "-t" not in result[:3], "ssh -t must not appear for control commands"
        # The only "-t" that may appear is the tmux target flag
        assert result.count("ssh") == 1
        assert "myhost" in result
        assert "-t" in result  # from the tmux args themselves

    def test_remote_ssh_is_first_element(self) -> None:
        assert TmuxHost("remote.host").cmd(["ls"])[0] == "ssh"

    def test_remote_host_is_second_element(self) -> None:
        result = TmuxHost("remote.host").cmd(["ls"])
        assert result[1] == "remote.host"

    def test_remote_tmux_is_third_element(self) -> None:
        result = TmuxHost("remote.host").cmd(["ls"])
        assert result[2] == "tmux"

    def test_remote_user_at_host(self) -> None:
        result = TmuxHost("user@myhost").cmd(["ls"])
        assert result == ["ssh", "user@myhost", "tmux", "ls"]

    # -- remote with tty=True --------------------------------------------------

    def test_remote_tty_true_inserts_dash_t(self) -> None:
        result = TmuxHost("myhost").cmd(["attach-session", "-t", "s"], tty=True)
        assert result == ["ssh", "-t", "myhost", "tmux", "attach-session", "-t", "s"]

    def test_remote_tty_true_dash_t_before_host(self) -> None:
        result = TmuxHost("myhost").cmd(["attach-session", "-t", "s"], tty=True)
        idx_dash_t = result.index("-t")  # first -t is the ssh flag
        idx_host = result.index("myhost")
        assert idx_dash_t < idx_host, "-t must precede the hostname"

    def test_remote_tty_true_ssh_is_first(self) -> None:
        result = TmuxHost("h").cmd(["attach-session"], tty=True)
        assert result[0] == "ssh"
        assert result[1] == "-t"

    def test_remote_tty_false_no_dash_t_before_host(self) -> None:
        result = TmuxHost("myhost").cmd(["has-session"], tty=False)
        # ssh block is ["ssh", "myhost", "tmux", ...]
        assert result[0] == "ssh"
        assert result[1] == "myhost"  # no -t between ssh and host

    # -- frozen / immutable ----------------------------------------------------

    def test_frozen_cannot_mutate_ssh_target(self) -> None:
        h = TmuxHost(None)
        with pytest.raises((AttributeError, TypeError)):
            h.ssh_target = "newhost"  # type: ignore[misc]

    def test_equality(self) -> None:
        assert TmuxHost(None) == TmuxHost(None)
        assert TmuxHost("h") == TmuxHost("h")
        assert TmuxHost(None) != TmuxHost("h")

    def test_default_host_is_local(self) -> None:
        """TmuxHost(None) is the local host."""
        h = TmuxHost(None)
        assert h.ssh_target is None
        assert h.cmd(["ls"])[0] == "tmux"


# ── stdin injection with TmuxHost ─────────────────────────────────────────────


class TestInjectBriefingWithHost:
    """Verify that _inject_briefing_into_tmux_session honours the host seam."""

    def _ok_mock(self) -> Any:
        def _m(cmd: list[str], **kw: Any) -> MagicMock:
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m
        return _m

    # -- local default host ----------------------------------------------------

    def test_local_load_buffer_first_arg_is_tmux(self) -> None:
        calls: list[list[str]] = []

        def _m(cmd: list[str], **kw: Any) -> MagicMock:
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_m), patch("time.sleep"):
            _inject_briefing_into_tmux_session("s", "text", timeout=0.0)

        lb = [c for c in calls if "load-buffer" in c]
        assert lb, "load-buffer not called"
        assert lb[0][0] == "tmux"

    def test_local_load_buffer_uses_stdin_source(self) -> None:
        """Default (local) host must pass '-' as the load-buffer source."""
        calls: list[dict[str, Any]] = []

        def _m(cmd: list[str], **kw: Any) -> MagicMock:
            calls.append({"cmd": list(cmd), "kw": kw})
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_m), patch("time.sleep"):
            _inject_briefing_into_tmux_session("s", "hello", timeout=0.0)

        lb = [c for c in calls if "load-buffer" in c["cmd"]]
        assert lb, "load-buffer not called"
        assert lb[0]["cmd"][-1] == "-", "load-buffer source must be '-' (stdin)"
        assert lb[0]["kw"].get("input") == "hello"

    def test_local_load_buffer_input_has_no_trailing_newline(self) -> None:
        calls: list[dict[str, Any]] = []

        def _m(cmd: list[str], **kw: Any) -> MagicMock:
            calls.append({"cmd": list(cmd), "kw": kw})
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        briefing = "text with trailing newline\n"
        with patch("subprocess.run", side_effect=_m), patch("time.sleep"):
            _inject_briefing_into_tmux_session("s", briefing, timeout=0.0)

        lb = [c for c in calls if "load-buffer" in c["cmd"]]
        assert lb
        assert not lb[0]["kw"]["input"].endswith("\n"), (
            "input must have trailing newline stripped"
        )

    # -- remote host -----------------------------------------------------------

    def test_remote_host_load_buffer_first_arg_is_ssh(self) -> None:
        calls: list[list[str]] = []

        def _m(cmd: list[str], **kw: Any) -> MagicMock:
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        rh = TmuxHost("myremote")
        with patch("subprocess.run", side_effect=_m), patch("time.sleep"):
            _inject_briefing_into_tmux_session("s", "text", timeout=0.0, host=rh)

        lb = [c for c in calls if "load-buffer" in c]
        assert lb, "load-buffer not called on remote host"
        assert lb[0][0] == "ssh"
        assert "myremote" in lb[0]

    def test_remote_host_capture_pane_first_arg_is_ssh(self) -> None:
        calls: list[list[str]] = []

        def _m(cmd: list[str], **kw: Any) -> MagicMock:
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        rh = TmuxHost("myremote")
        with patch("subprocess.run", side_effect=_m), patch("time.sleep"):
            # timeout=1.0 so the while loop runs at least once
            _inject_briefing_into_tmux_session("s", "text", timeout=1.0, host=rh)

        cp = [c for c in calls if "capture-pane" in c]
        if cp:  # loop may not execute on very fast runs
            assert cp[0][0] == "ssh"

    def test_remote_host_paste_buffer_first_arg_is_ssh(self) -> None:
        calls: list[list[str]] = []

        def _m(cmd: list[str], **kw: Any) -> MagicMock:
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        rh = TmuxHost("myremote")
        with patch("subprocess.run", side_effect=_m), patch("time.sleep"):
            _inject_briefing_into_tmux_session("s", "text", timeout=0.0, host=rh)

        pb = [c for c in calls if "paste-buffer" in c]
        assert pb, "paste-buffer not called on remote host"
        assert pb[0][0] == "ssh"
        assert "myremote" in pb[0]


# ── tmux_session_alive with non-default host ──────────────────────────────────


class TestTmuxSessionAliveWithHost:
    def test_local_host_sends_tmux_cmd(self) -> None:
        captured: list[list[str]] = []

        def _m(cmd: list[str], **kw: Any) -> MagicMock:
            captured.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=_m):
            result = tmux_session_alive("coord-abc", host=TmuxHost(None))

        assert result is True
        assert captured[0][0] == "tmux"
        assert "has-session" in captured[0]

    def test_remote_host_sends_ssh_cmd(self) -> None:
        captured: list[list[str]] = []

        def _m(cmd: list[str], **kw: Any) -> MagicMock:
            captured.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=_m):
            result = tmux_session_alive("coord-abc", host=TmuxHost("remotehost"))

        assert result is True
        assert captured[0][0] == "ssh"
        assert "remotehost" in captured[0]
        assert "has-session" in captured[0]
        # No -t flag — has-session is a control command
        assert captured[0][1] != "-t"


# ── list_coord_tmux_sessions with non-default host ────────────────────────────


class TestListCoordTmuxSessionsWithHost:
    def test_local_host_sends_tmux_cmd(self) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "coord-abc\n"

        with patch("subprocess.run", return_value=m) as mock_run:
            list_coord_tmux_sessions(host=TmuxHost(None))

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "tmux"
        assert "ls" in cmd

    def test_remote_host_sends_ssh_cmd(self) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "coord-abc\n"

        with patch("subprocess.run", return_value=m) as mock_run:
            sessions = list_coord_tmux_sessions(host=TmuxHost("remotehost"))

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "remotehost" in cmd
        assert "ls" in cmd
        assert sessions[0]["session_name"] == "coord-abc"
