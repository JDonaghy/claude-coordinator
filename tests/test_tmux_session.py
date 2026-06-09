"""Tests for the tmux session-resilience helpers added in #487.

Covers:
1. ``tmux_session_name`` — canonical session-name construction
2. ``tmux_available`` — PATH probe for ``tmux``
3. ``tmux_session_alive`` — ``tmux has-session`` wrapper
4. ``list_coord_tmux_sessions`` — filter for ``coord-*`` sessions
5. ``launch_human_attended_interactive`` with ``assignment_id`` — tmux
   path taken when tmux is available, PTY fallback when unavailable.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coord.interactive import (
    TMUX_SESSION_PREFIX,
    list_coord_tmux_sessions,
    tmux_available,
    tmux_session_alive,
    tmux_session_name,
)


# ── tmux_session_name ─────────────────────────────────────────────────────────


class TestTmuxSessionName:
    def test_prefix_applied(self) -> None:
        assert tmux_session_name("abc123") == "coord-abc123"

    def test_uses_module_constant(self) -> None:
        aid = "xyzzy-99"
        name = tmux_session_name(aid)
        assert name.startswith(TMUX_SESSION_PREFIX)
        assert name.endswith(aid)

    def test_no_extra_separators(self) -> None:
        name = tmux_session_name("my-assignment-id")
        # Should be exactly TMUX_SESSION_PREFIX + assignment_id, no extra chars.
        assert name == f"{TMUX_SESSION_PREFIX}my-assignment-id"

    def test_empty_string_produces_bare_prefix(self) -> None:
        name = tmux_session_name("")
        assert name == TMUX_SESSION_PREFIX

    def test_round_trip_via_strip(self) -> None:
        """The assignment_id is recoverable by stripping the prefix."""
        aid = "round-trip-42"
        name = tmux_session_name(aid)
        recovered = name[len(TMUX_SESSION_PREFIX):]
        assert recovered == aid


# ── tmux_available ────────────────────────────────────────────────────────────


class TestTmuxAvailable:
    def test_returns_true_when_which_finds_tmux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
        assert tmux_available() is True

    def test_returns_false_when_which_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: None)
        assert tmux_available() is False

    def test_returns_bool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda _name: "/bin/tmux")
        result = tmux_available()
        assert isinstance(result, bool)


# ── tmux_session_alive ────────────────────────────────────────────────────────


class TestTmuxSessionAlive:
    def _make_completed_proc(self, returncode: int) -> Any:
        m = MagicMock()
        m.returncode = returncode
        return m

    def test_alive_when_returncode_zero(self) -> None:
        with patch("subprocess.run", return_value=self._make_completed_proc(0)):
            assert tmux_session_alive("coord-abc") is True

    def test_not_alive_when_returncode_one(self) -> None:
        with patch("subprocess.run", return_value=self._make_completed_proc(1)):
            assert tmux_session_alive("coord-abc") is False

    def test_not_alive_on_subprocess_error(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.SubprocessError):
            assert tmux_session_alive("coord-abc") is False

    def test_not_alive_on_os_error(self) -> None:
        with patch("subprocess.run", side_effect=OSError("tmux not found")):
            assert tmux_session_alive("coord-abc") is False

    def test_uses_has_session_subcommand(self) -> None:
        """Verify the correct tmux subcommand is invoked."""
        captured: list[list[str]] = []

        def _mock_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=_mock_run):
            tmux_session_alive("coord-testme")

        assert captured, "subprocess.run was not called"
        assert captured[0][0] == "tmux"
        assert "has-session" in captured[0]
        assert "coord-testme" in captured[0]

    def test_returns_bool_not_int(self) -> None:
        with patch("subprocess.run", return_value=self._make_completed_proc(0)):
            result = tmux_session_alive("coord-x")
            assert isinstance(result, bool)


# ── list_coord_tmux_sessions ──────────────────────────────────────────────────


class TestListCoordTmuxSessions:
    def test_returns_empty_list_on_subprocess_error(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.SubprocessError):
            assert list_coord_tmux_sessions() == []

    def test_returns_empty_list_on_os_error(self) -> None:
        with patch("subprocess.run", side_effect=OSError):
            assert list_coord_tmux_sessions() == []

    def test_returns_empty_list_when_tmux_ls_fails(self) -> None:
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        with patch("subprocess.run", return_value=m):
            assert list_coord_tmux_sessions() == []

    def test_filters_non_coord_sessions(self) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "coord-abc123\nother-session\ncoord-def456\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        names = [s["session_name"] for s in sessions]
        assert "coord-abc123" in names
        assert "coord-def456" in names
        assert "other-session" not in names

    def test_returns_session_name_key(self) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "coord-myaid\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_name"] == "coord-myaid"

    def test_empty_tmux_output_returns_empty_list(self) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "\n"
        with patch("subprocess.run", return_value=m):
            assert list_coord_tmux_sessions() == []

    def test_strips_whitespace_from_session_names(self) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "  coord-trimme  \n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        assert sessions[0]["session_name"] == "coord-trimme"


# ── launch_human_attended_interactive — tmux path selection ──────────────────


class TestLaunchTmuxPathSelection:
    """Verify that the correct launch path (tmux vs PTY) is chosen."""

    def test_tmux_path_taken_when_available_and_assignment_id(self) -> None:
        """When tmux is available and assignment_id is set, _launch_via_tmux is called."""
        from coord.interactive import launch_human_attended_interactive

        launched_via: list[str] = []

        def _fake_tmux(_argv: Any, _briefing: Any, _session_name: Any, **_kw: Any) -> int:
            launched_via.append("tmux")
            return 0

        def _fake_pty(_argv: Any, _briefing: Any, **_kw: Any) -> int:
            launched_via.append("pty")
            return 0

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux), \
             patch("coord.interactive._launch_via_pty", side_effect=_fake_pty):
            rc = launch_human_attended_interactive(
                ["claude"], "test briefing", assignment_id="aid-123"
            )

        assert rc == 0
        assert launched_via == ["tmux"], f"expected tmux, got {launched_via}"

    def test_pty_fallback_when_no_assignment_id(self) -> None:
        """Without assignment_id, PTY relay is used even if tmux is available."""
        from coord.interactive import launch_human_attended_interactive

        launched_via: list[str] = []

        def _fake_pty(_argv: Any, _briefing: Any, **_kw: Any) -> int:
            launched_via.append("pty")
            return 0

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive._launch_via_pty", side_effect=_fake_pty):
            rc = launch_human_attended_interactive(["claude"], "briefing")

        assert rc == 0
        assert launched_via == ["pty"]

    def test_pty_fallback_when_tmux_unavailable(self) -> None:
        """When tmux is not on PATH, PTY relay is used even with assignment_id."""
        from coord.interactive import launch_human_attended_interactive

        launched_via: list[str] = []

        def _fake_pty(_argv: Any, _briefing: Any, **_kw: Any) -> int:
            launched_via.append("pty")
            return 0

        with patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive._launch_via_pty", side_effect=_fake_pty):
            rc = launch_human_attended_interactive(
                ["claude"], "briefing", assignment_id="aid-456"
            )

        assert rc == 0
        assert launched_via == ["pty"]

    def test_pty_fallback_when_tmux_session_creation_fails(self) -> None:
        """When _launch_via_tmux returns None, fall through to _launch_via_pty."""
        from coord.interactive import launch_human_attended_interactive

        launched_via: list[str] = []

        def _fake_tmux(_argv: Any, _briefing: Any, _session_name: Any, **_kw: Any) -> None:
            launched_via.append("tmux-fail")
            return None  # Signals session creation failed

        def _fake_pty(_argv: Any, _briefing: Any, **_kw: Any) -> int:
            launched_via.append("pty")
            return 0

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux), \
             patch("coord.interactive._launch_via_pty", side_effect=_fake_pty):
            rc = launch_human_attended_interactive(
                ["claude"], "briefing", assignment_id="aid-789"
            )

        assert rc == 0
        assert "tmux-fail" in launched_via
        assert "pty" in launched_via

    def test_exit_code_propagated_from_tmux_path(self) -> None:
        """The exit code returned by _launch_via_tmux is propagated to the caller."""
        from coord.interactive import launch_human_attended_interactive

        def _fake_tmux(_argv: Any, _briefing: Any, _session_name: Any, **_kw: Any) -> int:
            return 42

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux):
            rc = launch_human_attended_interactive(
                ["claude"], "briefing", assignment_id="aid-ec"
            )

        assert rc == 42

    def test_session_named_with_assignment_id(self) -> None:
        """The tmux session name includes the assignment_id."""
        from coord.interactive import launch_human_attended_interactive

        captured_session_names: list[str] = []

        def _fake_tmux(_argv: Any, _briefing: Any, session_name: str, **_kw: Any) -> int:
            captured_session_names.append(session_name)
            return 0

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux):
            launch_human_attended_interactive(
                ["claude"], "briefing", assignment_id="my-specific-aid"
            )

        assert len(captured_session_names) == 1
        assert "my-specific-aid" in captured_session_names[0]
        assert captured_session_names[0].startswith(TMUX_SESSION_PREFIX)

    def test_briefing_echoed_before_tmux_attach(self) -> None:
        """The operator-readable briefing echo happens before _launch_via_tmux."""
        import os as _os

        from coord.interactive import launch_human_attended_interactive

        echo_order: list[str] = []
        original_write = _os.write

        def _spy_write(fd: int, data: bytes) -> int:
            if b"seeded briefing" in data:
                echo_order.append("echo")
            return original_write(fd, data)

        def _fake_tmux(_argv: Any, _briefing: Any, _session_name: Any, **_kw: Any) -> int:
            echo_order.append("tmux")
            return 0

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux), \
             patch("os.write", side_effect=_spy_write):
            launch_human_attended_interactive(
                ["claude"], "A real briefing that matters.", assignment_id="echo-aid"
            )

        # The echo should have happened before tmux (it may not appear if
        # fd_out isn't a real fd in tests, but if it did appear it must
        # precede "tmux").
        tmux_idx = echo_order.index("tmux") if "tmux" in echo_order else -1
        if "echo" in echo_order:
            echo_idx = echo_order.index("echo")
            assert echo_idx < tmux_idx, (
                f"expected echo before tmux, got order: {echo_order}"
            )

    def test_no_briefing_echo_for_empty_briefing(self) -> None:
        """Empty briefing does not produce echo output on the tmux path."""
        import os as _os

        from coord.interactive import launch_human_attended_interactive

        wrote_echo = False
        original_write = _os.write

        def _spy_write(fd: int, data: bytes) -> int:
            nonlocal wrote_echo
            if b"seeded briefing" in data:
                wrote_echo = True
            return original_write(fd, data)

        def _fake_tmux(_argv: Any, _briefing: Any, _session_name: Any, **_kw: Any) -> int:
            return 0

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux), \
             patch("os.write", side_effect=_spy_write):
            launch_human_attended_interactive(
                ["claude"], "   ", assignment_id="empty-brief"
            )

        assert not wrote_echo, "no echo should happen for whitespace-only briefing"
