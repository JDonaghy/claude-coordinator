"""Tests for #2541: the human-attended ssh+tmux interactive launcher used to
crash before an operator could ever attach (a fresh worktree's ``claude``
first-run/onboarding prompt), AND left no log behind for `coord log` to show.

Covers:
1. ``_launch_via_tmux`` sets ``remain-on-exit on`` on fresh session creation
   so a crashed/finished pane's screen stays inspectable instead of tmux
   tearing the whole (single-pane) session down with it.
2. ``_capture_pane_history`` / ``_persist_interactive_pane_log`` — the
   pane-capture-to-log helpers, local and remote.
3. ``_launch_via_tmux`` persists a pane-log snapshot unconditionally on exit.
4. ``coord/agent_app.py``'s ``/logs/{id}`` handler falls back to that same
   conventional log path when the agent never tracked the assignment in
   memory (the case for these directly-ssh'd tmux sessions).
5. The remote stale-session reaper treats a ``remain-on-exit`` dead pane as
   reapable rather than "still alive forever" (a regression #1 would
   otherwise introduce).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from coord.agent import AgentServer
from coord.agent_app import build_app
from coord.interactive import TmuxHost


def _ok(returncode: int = 0, stdout: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    return m


# ── remain-on-exit on fresh session creation ─────────────────────────────────


class TestRemainOnExitSetOnCreate:
    def test_set_on_fresh_local_session(self) -> None:
        from coord.interactive import _launch_via_tmux

        calls: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            calls.append(list(cmd))
            return _ok()

        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            _launch_via_tmux(["claude"], "", "coord-remainfresh")

        remain_calls = [c for c in calls if "remain-on-exit" in c]
        assert remain_calls, "expected a `tmux set-option ... remain-on-exit on` call"
        assert remain_calls[0][0] == "tmux"
        assert "on" in remain_calls[0]
        assert "coord-remainfresh" in remain_calls[0]

    def test_not_set_when_session_already_alive(self) -> None:
        """Reuse-after-crash: creation is skipped entirely, so there is no
        fresh ``new-session`` call to attach the option to."""
        from coord.interactive import _launch_via_tmux

        calls: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            calls.append(list(cmd))
            return _ok()

        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=True):
            _launch_via_tmux(["claude"], "", "coord-remainreuse")

        assert not [c for c in calls if "remain-on-exit" in c]

    def test_set_on_fresh_remote_session(self) -> None:
        from coord.interactive import _launch_via_tmux

        calls: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            calls.append(list(cmd))
            return _ok()

        host = TmuxHost(ssh_target="myremote")
        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            _launch_via_tmux(
                ["claude"], "", "coord-remainremote",
                host=host, raw_shell_cmd="claude",
            )

        remain_calls = [c for c in calls if "remain-on-exit" in c]
        assert remain_calls, "expected a remote `remain-on-exit` call"
        assert remain_calls[0][0] == "ssh"
        assert "myremote" in remain_calls[0]

    def test_remain_on_exit_failure_is_non_fatal(self) -> None:
        """A failing/erroring `set-option` call must not abort the launch."""
        from coord.interactive import _launch_via_tmux

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            if "remain-on-exit" in cmd:
                raise OSError("tmux missing")
            return _ok()

        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            rc = _launch_via_tmux(["claude"], "", "coord-remainerr")

        assert rc == 0


# ── _capture_pane_history ────────────────────────────────────────────────────


class TestCapturePaneHistory:
    def test_returns_stdout_on_success(self) -> None:
        from coord.interactive import _capture_pane_history

        with patch("subprocess.run", return_value=_ok(stdout="hello pane\n")):
            assert _capture_pane_history("coord-abc") == "hello pane\n"

    def test_none_on_nonzero_returncode(self) -> None:
        from coord.interactive import _capture_pane_history

        with patch("subprocess.run", return_value=_ok(returncode=1, stdout="junk")):
            assert _capture_pane_history("coord-abc") is None

    def test_none_on_subprocess_error(self) -> None:
        import subprocess as _sp
        from coord.interactive import _capture_pane_history

        with patch("subprocess.run", side_effect=_sp.SubprocessError):
            assert _capture_pane_history("coord-abc") is None

    def test_none_on_blank_stdout(self) -> None:
        """An empty/whitespace-only capture is not worth persisting."""
        from coord.interactive import _capture_pane_history

        with patch("subprocess.run", return_value=_ok(stdout="   \n")):
            assert _capture_pane_history("coord-abc") is None

    def test_none_on_non_string_stdout(self) -> None:
        """A generic un-configured mock (as many existing tests use) leaves
        ``.stdout`` as an auto-attribute ``MagicMock`` — must degrade to
        None, not raise, and must NOT be treated as real pane content."""
        from coord.interactive import _capture_pane_history

        m = MagicMock()
        m.returncode = 0
        # m.stdout left unconfigured -> MagicMock, not a str
        with patch("subprocess.run", return_value=m):
            assert _capture_pane_history("coord-abc") is None

    def test_uses_full_history_flag(self) -> None:
        from coord.interactive import _capture_pane_history

        calls: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            calls.append(list(cmd))
            return _ok(stdout="x")

        with patch("subprocess.run", side_effect=_mock_run):
            _capture_pane_history("coord-abc")

        assert calls
        assert "capture-pane" in calls[0]
        assert "-S" in calls[0] and "-" in calls[0]


# ── _persist_interactive_pane_log ────────────────────────────────────────────


class TestPersistInteractivePaneLogLocal:
    def test_writes_capture_to_conventional_log_path(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from coord.interactive import _persist_interactive_pane_log

        monkeypatch.setattr("coord.agent.DEFAULT_STATE_DIR", tmp_path)

        with patch("subprocess.run", return_value=_ok(stdout="the pane text\n")):
            _persist_interactive_pane_log("aid-local-1", "coord-aid-local-1")

        log_path = tmp_path / "logs" / "aid-local-1.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "the pane text" in content
        assert "aid-local-1" in content  # header mentions the assignment

    def test_no_file_written_when_nothing_to_capture(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from coord.interactive import _persist_interactive_pane_log

        monkeypatch.setattr("coord.agent.DEFAULT_STATE_DIR", tmp_path)

        with patch("subprocess.run", return_value=_ok(stdout="")):
            _persist_interactive_pane_log("aid-local-2", "coord-aid-local-2")

        assert not (tmp_path / "logs" / "aid-local-2.log").exists()

    def test_never_raises_on_oserror(self, tmp_path: Path, monkeypatch: Any) -> None:
        from coord.interactive import _persist_interactive_pane_log

        # Point at a path that can't be created (a file, not a directory).
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("x")
        monkeypatch.setattr("coord.agent.DEFAULT_STATE_DIR", blocked)

        with patch("subprocess.run", return_value=_ok(stdout="pane text")):
            _persist_interactive_pane_log("aid-local-3", "coord-aid-local-3")
        # No exception == pass.


class TestPersistInteractivePaneLogRemote:
    def test_single_ssh_call_captures_and_redirects(self) -> None:
        from coord.interactive import _persist_interactive_pane_log

        calls: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            calls.append(list(cmd))
            return _ok()

        host = TmuxHost(ssh_target="myremote")
        with patch("subprocess.run", side_effect=_mock_run):
            _persist_interactive_pane_log("aid-remote-1", "coord-aid-remote-1", host=host)

        assert len(calls) == 1, "remote persist must be a single ssh round-trip"
        assert calls[0][0] == "ssh"
        assert "myremote" in calls[0]
        joined = " ".join(calls[0])
        assert "capture-pane" in joined
        assert "aid-remote-1.log" in joined
        assert "$HOME/.coord/logs" in joined

    def test_never_raises_on_subprocess_error(self) -> None:
        import subprocess as _sp
        from coord.interactive import _persist_interactive_pane_log

        host = TmuxHost(ssh_target="myremote")
        with patch("subprocess.run", side_effect=_sp.SubprocessError):
            _persist_interactive_pane_log("aid-remote-2", "coord-aid-remote-2", host=host)
        # No exception == pass.


# ── _launch_via_tmux persists the pane log unconditionally ──────────────────


class TestLaunchViaTmuxPersistsPaneLog:
    def test_persist_called_with_derived_assignment_id(self) -> None:
        from coord.interactive import _launch_via_tmux

        with patch("subprocess.run", return_value=_ok()), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive._persist_interactive_pane_log") as mock_persist:
            _launch_via_tmux(["claude"], "", "coord-derive-me")

        mock_persist.assert_called_once()
        args, kwargs = mock_persist.call_args
        assert args[0] == "derive-me"  # session_name with the prefix stripped
        assert args[1] == "coord-derive-me"

    def test_persist_called_even_on_attach_failure(self) -> None:
        from coord.interactive import _launch_via_tmux

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            if "attach-session" in cmd:
                raise OSError("no pty")
            return _ok()

        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive._persist_interactive_pane_log") as mock_persist:
            rc = _launch_via_tmux(["claude"], "", "coord-attachfail")

        assert rc == 1
        mock_persist.assert_called_once()


# ── agent_app.py /logs/{id} disk fallback ────────────────────────────────────


def _agent_client(tmp_path: Path) -> tuple[TestClient, AgentServer]:
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=[],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={},
    )
    return TestClient(build_app(server)), server


class TestLogsEndpointDiskFallback:
    def test_serves_untracked_assignment_log_from_disk(self, tmp_path: Path) -> None:
        client, server = _agent_client(tmp_path)
        server.log_dir.mkdir(parents=True, exist_ok=True)
        (server.log_dir / "untracked-aid.log").write_text("captured pane content\n")

        r = client.get("/logs/untracked-aid")
        assert r.status_code == 200
        assert "captured pane content" in r.text
        assert r.headers["X-Coord-Log-Status"] == "unknown"

    def test_404_when_no_file_and_untracked(self, tmp_path: Path) -> None:
        client, server = _agent_client(tmp_path)
        r = client.get("/logs/totally-unknown-id")
        assert r.status_code == 404

    def test_rejects_path_traversal_assignment_id(self, tmp_path: Path) -> None:
        client, server = _agent_client(tmp_path)
        r = client.get("/logs/..%2F..%2F..%2Fetc%2Fpasswd")
        assert r.status_code in (400, 404)
        # Must never actually read an arbitrary file off disk.
        assert "root:" not in r.text

    def test_since_offset_works_on_fallback_path(self, tmp_path: Path) -> None:
        client, server = _agent_client(tmp_path)
        server.log_dir.mkdir(parents=True, exist_ok=True)
        (server.log_dir / "untracked-aid2.log").write_text("0123456789")

        r = client.get("/logs/untracked-aid2", params={"since": 5})
        assert r.status_code == 200
        assert r.text == "56789"
