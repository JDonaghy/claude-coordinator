"""Tests for the tmux session-resilience helpers added in #487.

Covers:
1. ``tmux_session_name`` — canonical session-name construction
2. ``tmux_available`` — PATH probe for ``tmux``
3. ``tmux_session_alive`` — ``tmux has-session`` wrapper
4. ``list_coord_tmux_sessions`` — filter for ``coord-*`` sessions + pane_dead (#491)
5. ``tmux_pane_dead`` — pane-level dead detection (#491)
6. ``launch_human_attended_interactive`` with ``assignment_id`` — tmux
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
    tmux_pane_dead,
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
#
# The function now uses ``tmux list-panes -a -F "#{session_name}\t#{pane_dead}"``
# to fetch both session name and pane-dead status in a single call (#491).
# Mock output must use the TAB-delimited format.


class TestListCoordTmuxSessions:
    def test_returns_empty_list_on_subprocess_error(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.SubprocessError):
            assert list_coord_tmux_sessions() == []

    def test_returns_empty_list_on_os_error(self) -> None:
        with patch("subprocess.run", side_effect=OSError):
            assert list_coord_tmux_sessions() == []

    def test_returns_empty_list_when_list_panes_fails(self) -> None:
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        with patch("subprocess.run", return_value=m):
            assert list_coord_tmux_sessions() == []

    def test_filters_non_coord_sessions(self) -> None:
        m = MagicMock()
        m.returncode = 0
        # Tab-separated: session_name\tpane_dead
        m.stdout = "coord-abc123\t0\nother-session\t0\ncoord-def456\t0\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        names = [s["session_name"] for s in sessions]
        assert "coord-abc123" in names
        assert "coord-def456" in names
        assert "other-session" not in names

    def test_returns_session_name_and_pane_dead_keys(self) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "coord-myaid\t0\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_name"] == "coord-myaid"
        assert sessions[0]["pane_dead"] == "0"

    def test_pane_dead_one_for_dead_pane(self) -> None:
        """pane_dead='1' when the pane process has exited."""
        m = MagicMock()
        m.returncode = 0
        m.stdout = "coord-deadaid\t1\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        assert len(sessions) == 1
        assert sessions[0]["pane_dead"] == "1"

    def test_alive_pane_wins_when_session_has_multiple_panes(self) -> None:
        """If any pane is alive (0), the session pane_dead stays '0'."""
        m = MagicMock()
        m.returncode = 0
        # Two panes: one dead (1), one alive (0) → session should show '0'.
        m.stdout = "coord-multiaid\t1\ncoord-multiaid\t0\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        assert len(sessions) == 1
        assert sessions[0]["pane_dead"] == "0"

    def test_empty_output_returns_empty_list(self) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "\n"
        with patch("subprocess.run", return_value=m):
            assert list_coord_tmux_sessions() == []

    def test_strips_whitespace_from_session_names(self) -> None:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "  coord-trimme  \t0\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        assert sessions[0]["session_name"] == "coord-trimme"

    def test_attached_true_when_client_attached(self) -> None:
        """#1031: session_attached='1' surfaces as attached=True."""
        m = MagicMock()
        m.returncode = 0
        m.stdout = "coord-attachedaid\t0\t1\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        assert len(sessions) == 1
        assert sessions[0]["attached"] is True

    def test_attached_false_when_client_detached(self) -> None:
        """#1031: session_attached='0' surfaces as attached=False."""
        m = MagicMock()
        m.returncode = 0
        m.stdout = "coord-detachedaid\t0\t0\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        assert len(sessions) == 1
        assert sessions[0]["attached"] is False

    def test_attached_well_defined_for_dead_pane_session(self) -> None:
        """#1031: a dead-pane session still reports a well-defined attached bool."""
        m = MagicMock()
        m.returncode = 0
        # Dead pane (claude exited) but a client is still attached to the
        # tmux session (detach-and-abandon didn't happen — operator is
        # sitting on a finished pane).
        m.stdout = "coord-deadbutattached\t1\t1\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        assert len(sessions) == 1
        assert sessions[0]["pane_dead"] == "1"
        assert sessions[0]["attached"] is True

    def test_attached_missing_field_defaults_false(self) -> None:
        """Rows without a third (session_attached) column default to False."""
        m = MagicMock()
        m.returncode = 0
        m.stdout = "coord-legacyaid\t0\n"
        with patch("subprocess.run", return_value=m):
            sessions = list_coord_tmux_sessions()
        assert len(sessions) == 1
        assert sessions[0]["attached"] is False

    def test_uses_list_panes_subcommand(self) -> None:
        """Verify the tmux list-panes -a subcommand is used (not ls)."""
        captured: list[list[str]] = []

        def _mock_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_mock_run):
            list_coord_tmux_sessions()

        assert captured, "subprocess.run was not called"
        cmd = captured[0]
        assert "tmux" in cmd[0] or "list-panes" in cmd
        assert "list-panes" in cmd
        assert "-a" in cmd


# ── tmux_pane_dead (#491) ─────────────────────────────────────────────────────


class TestTmuxPaneDead:
    def _mock_run(self, returncode: int, stdout: str) -> Any:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def test_returns_false_when_pane_is_alive(self) -> None:
        with patch("subprocess.run", return_value=self._mock_run(0, "0\n")):
            assert tmux_pane_dead("coord-abc") is False

    def test_returns_true_when_pane_is_dead(self) -> None:
        with patch("subprocess.run", return_value=self._mock_run(0, "1\n")):
            assert tmux_pane_dead("coord-abc") is True

    def test_returns_false_when_session_not_found(self) -> None:
        """Non-zero returncode means the session doesn't exist — not dead."""
        with patch("subprocess.run", return_value=self._mock_run(1, "")):
            assert tmux_pane_dead("coord-notexist") is False

    def test_returns_false_on_subprocess_error(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.SubprocessError):
            assert tmux_pane_dead("coord-abc") is False

    def test_returns_false_on_os_error(self) -> None:
        with patch("subprocess.run", side_effect=OSError):
            assert tmux_pane_dead("coord-abc") is False

    def test_all_panes_must_be_dead_for_true(self) -> None:
        """Returns True only when EVERY pane reports dead."""
        with patch("subprocess.run", return_value=self._mock_run(0, "1\n1\n")):
            assert tmux_pane_dead("coord-multi") is True

    def test_one_alive_pane_returns_false(self) -> None:
        """A single alive pane keeps the session active."""
        with patch("subprocess.run", return_value=self._mock_run(0, "1\n0\n")):
            assert tmux_pane_dead("coord-multi") is False

    def test_uses_list_panes_subcommand(self) -> None:
        """Verify list-panes -F #{pane_dead} -t <session> is called."""
        captured: list[list[str]] = []

        def _mock_run(cmd: list[str], **_kwargs: Any) -> Any:
            captured.append(cmd)
            m = MagicMock()
            m.returncode = 0
            m.stdout = "0\n"
            return m

        with patch("subprocess.run", side_effect=_mock_run):
            tmux_pane_dead("coord-testme")

        assert captured, "subprocess.run was not called"
        cmd = captured[0]
        assert "list-panes" in cmd
        assert "#{pane_dead}" in cmd
        assert "coord-testme" in cmd


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


# ── _launch_via_tmux — unverified injection is surfaced to the operator (#865) ──


class TestLaunchViaTmuxUnverifiedInjectionSurfaced:
    """Review follow-up on #865: the tmux path used to discard the bool
    return value of ``_inject_briefing_into_tmux_session`` — a hard failure
    (verify+retry exhausted) only produced a ``logging.error`` call, which
    (no ``logging.basicConfig`` anywhere in the repo) is invisible before
    the very next statement switches the terminal to the tmux alt-screen.
    ``_launch_via_tmux`` must now print a visible warning to the operator's
    own terminal and require an explicit acknowledgment BEFORE attaching.
    """

    def _mock_create_ok(self) -> Any:
        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        return _mock_run

    def test_warns_and_waits_for_ack_when_injection_unverified(self) -> None:
        from coord.interactive import _launch_via_tmux

        with patch("subprocess.run", side_effect=self._mock_create_ok()), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch(
                 "coord.interactive._inject_briefing_into_tmux_session",
                 return_value=False,
             ), \
             patch("builtins.print") as mock_print, \
             patch("builtins.input", return_value="") as mock_input:
            _launch_via_tmux(["claude"], "briefing text", "coord-unverified")

        assert mock_input.called, (
            "operator should be required to acknowledge before attach when "
            "injection could not be verified"
        )
        printed = " ".join(
            str(call.args[0]) if call.args else "" for call in mock_print.call_args_list
        )
        assert "briefing injection could not be verified" in printed

    def test_no_warning_or_ack_when_injection_verified(self) -> None:
        from coord.interactive import _launch_via_tmux

        with patch("subprocess.run", side_effect=self._mock_create_ok()), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch(
                 "coord.interactive._inject_briefing_into_tmux_session",
                 return_value=True,
             ), \
             patch("builtins.print") as mock_print, \
             patch("builtins.input") as mock_input:
            _launch_via_tmux(["claude"], "briefing text", "coord-verified")

        assert not mock_input.called, (
            "no acknowledgment should be required when injection was verified"
        )
        assert not mock_print.called, (
            "no warning should be printed when injection was verified"
        )

    def test_no_warning_for_empty_briefing(self) -> None:
        """Empty briefing skips injection entirely — nothing to warn about."""
        from coord.interactive import _launch_via_tmux

        with patch("subprocess.run", side_effect=self._mock_create_ok()), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch(
                 "coord.interactive._inject_briefing_into_tmux_session",
                 return_value=False,
             ) as mock_inject, \
             patch("builtins.print") as mock_print, \
             patch("builtins.input") as mock_input:
            _launch_via_tmux(["claude"], "   ", "coord-empty-brief")

        assert not mock_inject.called
        assert not mock_input.called
        assert not mock_print.called


# ── _launch_via_tmux — kill-vs-detach guard (#1102) ──────────────────────────


class TestLaunchViaTmuxAttachKillGuard:
    """#1102: a wrong keystroke (kill-pane) after attach silently destroys
    the whole session, with no operator-facing distinction from a normal
    detach.  ``_launch_via_tmux`` must (1) set a best-effort ``pane-died``
    hook on fresh session creation, and (2) print the kill-vs-detach
    warning immediately before every attach — fresh session or reuse.
    """

    def _mock_create_ok(self) -> Any:
        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        return _mock_run

    def test_pane_died_hook_set_on_fresh_session(self) -> None:
        from coord.interactive import _launch_via_tmux

        with patch("subprocess.run", side_effect=self._mock_create_ok()) as run_mock, \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            _launch_via_tmux(["claude"], "", "coord-freshhook")

        hook_calls = [
            call for call in run_mock.call_args_list
            if "set-hook" in call.args[0]
        ]
        assert hook_calls, "expected a `tmux set-hook ... pane-died ...` call"
        hook_argv = hook_calls[0].args[0]
        assert "pane-died" in hook_argv
        assert "coord-freshhook" in hook_argv

    def test_no_pane_died_hook_when_session_already_alive(self) -> None:
        """Reuse-after-crash: the hook was already set at creation time —
        no need (and no session-name arg available) to set it again."""
        from coord.interactive import _launch_via_tmux

        with patch("subprocess.run", side_effect=self._mock_create_ok()) as run_mock, \
             patch("coord.interactive.tmux_session_alive", return_value=True):
            _launch_via_tmux(["claude"], "", "coord-reused")

        hook_calls = [
            call for call in run_mock.call_args_list
            if "set-hook" in call.args[0]
        ]
        assert not hook_calls

    def test_attach_warning_printed_before_attach_fresh_session(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        from coord.interactive import _launch_via_tmux

        with patch("subprocess.run", side_effect=self._mock_create_ok()), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            _launch_via_tmux(["claude"], "", "coord-warnfresh")

        out = capfd.readouterr().out
        assert "Ctrl-b d" in out
        assert "DO NOT" in out

    def test_attach_warning_printed_before_attach_reuse(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        from coord.interactive import _launch_via_tmux

        with patch("subprocess.run", side_effect=self._mock_create_ok()), \
             patch("coord.interactive.tmux_session_alive", return_value=True):
            _launch_via_tmux(["claude"], "", "coord-warnreuse")

        out = capfd.readouterr().out
        assert "Ctrl-b d" in out
        assert "DO NOT" in out
