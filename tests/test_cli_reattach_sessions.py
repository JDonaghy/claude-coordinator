"""Tests for ``coord sessions`` and ``coord reattach`` commands (#487).

Also covers ``_inject_briefing_into_tmux_session`` — the quiescence-based
briefing injection helper that was previously untested.

Reviewer findings addressed (PR #490):
- 🔴 Missing tests for ``coord reattach``
- 🔴 Missing tests for ``coord sessions``
- 🟡 ``_inject_briefing_into_tmux_session`` untested
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main

from .conftest import output_and_stderr


# ── Shared fixtures ────────────────────────────────────────────────────────────

# Minimal coordinator.yml with one repo + one machine (enough for reattach to
# reconstruct base_branch and repo_path).
_CONFIG_YAML = """\
repos:
  - name: myrepo
    github: acme/myrepo
    default_branch: main
machines:
  - name: mymachine
    host: mymachine.tailnet
    repos: [myrepo]
    repo_paths:
      myrepo: /tmp/myrepo
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(_CONFIG_YAML)
    return p


def _insert_assignment(conn, assignment_id: str, **overrides: Any) -> None:
    """Insert a minimal assignment row directly into the in-memory DB."""
    vals: dict[str, Any] = {
        "assignment_id": assignment_id,
        "machine_name": "mymachine",
        "repo_name": "myrepo",
        "repo_github": "acme/myrepo",
        "issue_number": 42,
        "issue_title": "Test issue",
        "status": "running",
    }
    vals.update(overrides)
    conn.execute(
        """INSERT INTO assignments
           (assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status)
           VALUES (:assignment_id, :machine_name, :repo_name, :repo_github,
                   :issue_number, :issue_title, :status)""",
        vals,
    )
    conn.commit()


# ── coord sessions ─────────────────────────────────────────────────────────────


class TestSessionsCmd:
    """``coord sessions`` and ``coord sessions --json``."""

    # -- JSON schema -----------------------------------------------------------

    def test_json_empty_when_no_sessions(self) -> None:
        """No sessions → ``{"sessions": []}``."""
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=[]):
            result = CliRunner().invoke(main, ["sessions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"sessions": []}

    def test_json_top_level_key_is_sessions(self) -> None:
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=[]):
            result = CliRunner().invoke(main, ["sessions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "sessions" in data

    def test_json_all_five_schema_keys_present(self, coord_db: Any) -> None:
        """Every session object contains the five keys the TUI depends on."""
        raw = [{"session_name": "coord-abc123"}]
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=raw):
            result = CliRunner().invoke(main, ["sessions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["sessions"]) == 1
        s = data["sessions"][0]
        for key in ("session_name", "assignment_id", "issue_number", "repo_name", "issue_title"):
            assert key in s, f"missing required JSON key {key!r}"

    def test_json_assignment_id_stripped_from_prefix(self) -> None:
        """``assignment_id`` is the part after ``coord-``."""
        raw = [{"session_name": "coord-xyz-789"}]
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=raw):
            result = CliRunner().invoke(main, ["sessions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["sessions"][0]["assignment_id"] == "xyz-789"

    def test_json_session_name_preserved(self) -> None:
        """``session_name`` in JSON matches the raw tmux name exactly."""
        raw = [{"session_name": "coord-myaid"}]
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=raw):
            result = CliRunner().invoke(main, ["sessions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["sessions"][0]["session_name"] == "coord-myaid"

    def test_json_metadata_populated_from_db(self, coord_db: Any) -> None:
        """When the assignment exists in the DB, its metadata appears in JSON."""
        _insert_assignment(
            coord_db,
            "my-aid",
            issue_number=99,
            repo_name="myrepo",
            repo_github="acme/myrepo",
            issue_title="My specific issue",
        )
        raw = [{"session_name": "coord-my-aid"}]
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=raw):
            result = CliRunner().invoke(main, ["sessions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        s = data["sessions"][0]
        assert s["issue_number"] == 99
        assert s["repo_name"] == "myrepo"
        assert s["issue_title"] == "My specific issue"
        assert s["assignment_id"] == "my-aid"

    def test_json_nulls_when_no_db_match(self, coord_db: Any) -> None:
        """When the assignment is not in the DB, metadata fields are null."""
        raw = [{"session_name": "coord-unknown-aid"}]
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=raw):
            result = CliRunner().invoke(main, ["sessions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        s = data["sessions"][0]
        assert s["issue_number"] is None
        assert s["repo_name"] is None
        assert s["issue_title"] is None

    def test_json_multiple_sessions(self, coord_db: Any) -> None:
        """All sessions appear in the JSON array."""
        raw = [
            {"session_name": "coord-aid-1"},
            {"session_name": "coord-aid-2"},
        ]
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=raw):
            result = CliRunner().invoke(main, ["sessions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["sessions"]) == 2
        ids = {s["assignment_id"] for s in data["sessions"]}
        assert ids == {"aid-1", "aid-2"}

    # -- Text output -----------------------------------------------------------

    def test_text_no_sessions_message(self) -> None:
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=[]):
            result = CliRunner().invoke(main, ["sessions"])
        assert result.exit_code == 0
        assert "No running interactive sessions" in result.output

    def test_text_includes_reattach_hint(self, coord_db: Any) -> None:
        raw = [{"session_name": "coord-aid-42"}]
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=raw):
            result = CliRunner().invoke(main, ["sessions"])
        assert result.exit_code == 0
        assert "coord reattach" in result.output


# ── coord reattach ─────────────────────────────────────────────────────────────


class TestReattachCmd:
    """``coord reattach <assignment_id>``."""

    # -- Pre-flight checks -----------------------------------------------------

    def test_tmux_not_available_exits_1(self, config_file: Path) -> None:
        """Exit code 1 and error message when tmux is absent."""
        with patch("coord.interactive.tmux_available", return_value=False):
            result = CliRunner().invoke(
                main, ["reattach", "aid-123", "--config", str(config_file)]
            )
        assert result.exit_code == 1
        assert "tmux is not available" in output_and_stderr(result)

    def test_session_not_alive_before_attach_exits_0(self, config_file: Path) -> None:
        """When the session is already dead, exit 0 with an informative message."""
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main, ["reattach", "aid-dead", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        assert "not alive" in result.output

    # -- Attach subprocess -----------------------------------------------------

    def test_attach_calls_correct_tmux_command(self, config_file: Path) -> None:
        """subprocess.run is invoked with ``tmux attach-session -t coord-<id>``."""
        captured: list[list[str]] = []

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            captured.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            return m

        alive_seq = iter([True, True])  # alive before attach; alive after (detached)

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n: next(alive_seq, True)), \
             patch("subprocess.run", side_effect=_mock_run):
            CliRunner().invoke(
                main, ["reattach", "aid-xyz", "--config", str(config_file)]
            )

        attach_cmds = [c for c in captured if "attach-session" in c]
        assert attach_cmds, "no tmux attach-session call found"
        cmd = attach_cmds[0]
        assert cmd[0] == "tmux"
        assert "attach-session" in cmd
        assert "coord-aid-xyz" in cmd

    # -- Session still alive after detach (Ctrl-b d) ---------------------------

    def test_session_alive_after_detach_prints_reattach_hint(
        self, config_file: Path
    ) -> None:
        """When user detaches (Ctrl-b d), session stays alive → reattach message shown."""
        attach_result = MagicMock()
        attach_result.returncode = 0

        alive_seq = iter([True, True])  # alive before; alive after (just detached)

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n: next(alive_seq, True)), \
             patch("subprocess.run", return_value=attach_result):
            result = CliRunner().invoke(
                main, ["reattach", "aid-running", "--config", str(config_file)]
            )

        assert result.exit_code == 0
        assert "running" in result.output or "reattach" in result.output.lower()

    def test_session_alive_after_detach_does_not_call_finalize(
        self, config_file: Path
    ) -> None:
        """No finalize call when session is still alive after attach returns."""
        attach_result = MagicMock()
        attach_result.returncode = 0

        alive_seq = iter([True, True])

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n: next(alive_seq, True)), \
             patch("subprocess.run", return_value=attach_result), \
             patch("coord.interactive.finalize_interactive_exit") as mock_fin:
            CliRunner().invoke(
                main, ["reattach", "aid-running", "--config", str(config_file)]
            )
        mock_fin.assert_not_called()

    # -- Session ended: metadata in DB -----------------------------------------

    def test_session_ended_with_metadata_calls_finalize(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """finalize_interactive_exit is called with the correct kwargs from the DB."""
        _insert_assignment(
            coord_db,
            "aid-done",
            issue_number=77,
            repo_name="myrepo",
            repo_github="acme/myrepo",
            machine_name="mymachine",
        )

        attach_result = MagicMock()
        attach_result.returncode = 0

        alive_seq = iter([True, False])  # alive before; dead after (session ended)

        fake_result = MagicMock()
        fake_result.already_recorded = False
        fake_result.terminal_status = "done"
        fake_result.commits_ahead = 3
        fake_result.push_ok = True

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n: next(alive_seq, False)), \
             patch("subprocess.run", return_value=attach_result), \
             patch("coord.interactive.finalize_interactive_exit", return_value=fake_result) as mock_fin:
            result = CliRunner().invoke(
                main, ["reattach", "aid-done", "--config", str(config_file)]
            )

        mock_fin.assert_called_once()
        kwargs = mock_fin.call_args[1]
        assert kwargs["assignment_id"] == "aid-done"
        assert kwargs["issue_number"] == 77
        assert kwargs["repo_name"] == "myrepo"
        assert result.exit_code == 0

    def test_session_ended_backstop_output_shown(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """Backstop status line is printed when finalize runs normally."""
        _insert_assignment(
            coord_db, "aid-backstop",
            issue_number=5, repo_name="myrepo",
            repo_github="acme/myrepo", machine_name="mymachine",
        )

        alive_seq = iter([True, False])

        fake_result = MagicMock()
        fake_result.already_recorded = False
        fake_result.terminal_status = "done"
        fake_result.commits_ahead = 1
        fake_result.push_ok = True

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n: next(alive_seq, False)), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("coord.interactive.finalize_interactive_exit", return_value=fake_result):
            result = CliRunner().invoke(
                main, ["reattach", "aid-backstop", "--config", str(config_file)]
            )

        assert result.exit_code == 0
        assert "backstop" in result.output

    def test_session_ended_already_recorded(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """When finalize returns already_recorded=True, a specific message is shown."""
        _insert_assignment(
            coord_db, "aid-recorded",
            issue_number=5, repo_name="myrepo",
            repo_github="acme/myrepo", machine_name="mymachine",
        )

        alive_seq = iter([True, False])

        fake_result = MagicMock()
        fake_result.already_recorded = True
        fake_result.terminal_status = "done"
        fake_result.push_ok = True

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n: next(alive_seq, False)), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("coord.interactive.finalize_interactive_exit", return_value=fake_result):
            result = CliRunner().invoke(
                main, ["reattach", "aid-recorded", "--config", str(config_file)]
            )

        assert result.exit_code == 0
        assert "already recorded" in result.output

    # -- Session ended: no metadata in DB --------------------------------------

    def test_session_ended_no_metadata_skips_finalize(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """When DB has no row for the assignment, finalize is NOT called."""
        alive_seq = iter([True, False])

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n: next(alive_seq, False)), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("coord.interactive.finalize_interactive_exit") as mock_fin:
            result = CliRunner().invoke(
                main, ["reattach", "aid-missing", "--config", str(config_file)]
            )

        mock_fin.assert_not_called()
        combined = output_and_stderr(result)
        # The "metadata not found" message must surface in some output stream
        assert "metadata not found" in combined or "skipping" in combined

    def test_session_ended_no_metadata_prints_warning_on_stderr(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """The 'metadata not found' message is emitted (may be on stderr)."""
        alive_seq = iter([True, False])

        runner = CliRunner()
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n: next(alive_seq, False)), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("coord.interactive.finalize_interactive_exit"):
            result = runner.invoke(
                main, ["reattach", "aid-ghost", "--config", str(config_file)]
            )

        combined = output_and_stderr(result)
        assert "metadata not found" in combined or "skipping" in combined

    # -- DB exception swallowing -----------------------------------------------

    def test_db_exception_does_not_crash_reattach(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """A DB error in the metadata lookup is swallowed — reattach still runs.

        ``cli.reattach`` does a deferred ``from coord.state import get_connection``
        inside the try-block, so the patch target must be
        ``coord.state.get_connection`` (where the function actually lives in the
        module namespace at lookup time), NOT ``coord.db.get_connection``
        (which the deferred import bypasses entirely).
        """
        alive_seq = iter([True, False])  # alive before, dead after attach

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n: next(alive_seq, False)), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("coord.state.get_connection", side_effect=RuntimeError("DB down")), \
             patch("coord.interactive.finalize_interactive_exit") as mock_fin:
            result = CliRunner().invoke(
                main, ["reattach", "aid-dberr", "--config", str(config_file)]
            )

        # The DB exception is swallowed; metadata stays None so finalize is
        # NOT called (the `if repo_name_val and …` guard fails) and a
        # "metadata not found" warning is emitted instead.
        assert result.exit_code == 0
        mock_fin.assert_not_called()
        combined = output_and_stderr(result)
        assert "metadata not found" in combined or "skipping" in combined


# ── _inject_briefing_into_tmux_session ─────────────────────────────────────────


class TestInjectBriefingIntoTmuxSession:
    """Unit tests for the quiescence-loop briefing injector."""

    def _make_run_mock(self, content_sequence: list[str] | None = None) -> Any:
        """Return a subprocess.run mock that simulates capture-pane output."""
        call_count = [0]
        seq = content_sequence or []

        def _mock(cmd: list[str], **kwargs: Any) -> MagicMock:
            m = MagicMock()
            m.returncode = 0
            if "capture-pane" in cmd:
                idx = call_count[0]
                call_count[0] += 1
                m.stdout = seq[idx] if idx < len(seq) else (seq[-1] if seq else "")
            else:
                m.stdout = ""
            return m

        return _mock

    # -- Trivial cases ---------------------------------------------------------

    def test_empty_briefing_returns_true_no_subprocess(self) -> None:
        """Empty / whitespace-only briefing returns True without calling subprocess."""
        from coord.interactive import _inject_briefing_into_tmux_session

        with patch("subprocess.run") as mock_run:
            result = _inject_briefing_into_tmux_session("coord-ses", "   ")
        assert result is True
        mock_run.assert_not_called()

    def test_newline_only_briefing_returns_true_no_subprocess(self) -> None:
        from coord.interactive import _inject_briefing_into_tmux_session

        with patch("subprocess.run") as mock_run:
            result = _inject_briefing_into_tmux_session("coord-ses", "\n\n")
        assert result is True
        mock_run.assert_not_called()

    # -- Injection always happens ----------------------------------------------

    def test_injects_after_immediate_timeout(self) -> None:
        """Even when timeout=0 (deadline already passed), injection still runs."""
        from coord.interactive import _inject_briefing_into_tmux_session

        with patch("subprocess.run", side_effect=self._make_run_mock()), \
             patch("time.sleep"):
            result = _inject_briefing_into_tmux_session(
                "coord-ses", "hello world", timeout=0.0
            )
        assert result is True

    def test_load_buffer_called(self) -> None:
        """``tmux load-buffer -b coord-brief <path>`` is invoked."""
        from coord.interactive import _inject_briefing_into_tmux_session

        calls: list[list[str]] = []

        def _mock(cmd: list[str], **kwargs: Any) -> MagicMock:
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_mock), patch("time.sleep"):
            _inject_briefing_into_tmux_session("coord-ses", "my briefing", timeout=0.0)

        load_calls = [c for c in calls if "load-buffer" in c]
        assert load_calls, "load-buffer was not called"
        assert "coord-brief" in load_calls[0]

    def test_paste_buffer_called_with_session_name(self) -> None:
        """``tmux paste-buffer -p -t <session> -b coord-brief`` is invoked."""
        from coord.interactive import _inject_briefing_into_tmux_session

        calls: list[list[str]] = []

        def _mock(cmd: list[str], **kwargs: Any) -> MagicMock:
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_mock), patch("time.sleep"):
            _inject_briefing_into_tmux_session("coord-ses", "my briefing", timeout=0.0)

        paste_calls = [c for c in calls if "paste-buffer" in c]
        assert paste_calls, "paste-buffer was not called"
        assert "coord-ses" in paste_calls[0]
        assert "-p" in paste_calls[0]  # bracketed-paste flag
        assert "coord-brief" in paste_calls[0]

    # -- Quiescence path -------------------------------------------------------

    def test_injects_after_quiescence_detected(self) -> None:
        """Injection happens after content stabilises for _READY_QUIESCE_S.

        The mock drives exactly two capture-pane iterations:
          iter 1 — content changes (None → "stable") → quiescent_since is set.
          iter 2 — content same → now - quiescent_since >= _READY_QUIESCE_S → break.
        time.monotonic() is called 5 times total (see call sequence in comments).
        """
        from coord.interactive import _inject_briefing_into_tmux_session, _READY_QUIESCE_S

        calls: list[list[str]] = []

        # Content is the same on both capture-pane calls so the elif path
        # fires and the quiescence condition is evaluated.
        cap_counter = [0]
        cap_content = ["stable content", "stable content"]

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            if "capture-pane" in cmd:
                idx = cap_counter[0]
                cap_counter[0] += 1
                m.stdout = cap_content[idx] if idx < len(cap_content) else cap_content[-1]
            else:
                m.stdout = ""
            return m

        # time.monotonic() call order (5 calls total):
        #   0 → base:           deadline = base + 60.0  (far future)
        #   1 → base + 0.1:     while-check iter 1 → True (enter loop)
        #   2 → base + 0.5:     now inside iter 1; content changed → quiescent_since = base + 0.5
        #   3 → base + 0.6:     while-check iter 2 → True
        #   4 → base + 0.5 + _READY_QUIESCE_S + 0.1:
        #                       now inside iter 2; same content; now - quiescent_since
        #                       = _READY_QUIESCE_S + 0.1 >= _READY_QUIESCE_S → break
        base = 1000.0
        mono_values = [
            base,
            base + 0.1,
            base + 0.5,
            base + 0.6,
            base + 0.5 + _READY_QUIESCE_S + 0.1,
        ]
        mono_counter = [0]

        def _mono() -> float:
            idx = mono_counter[0]
            mono_counter[0] += 1
            # After the list is exhausted return a value past the deadline so
            # the while-condition exits naturally rather than looping forever.
            if idx < len(mono_values):
                return mono_values[idx]
            return base + 999.0  # past deadline → loop exits

        with patch("subprocess.run", side_effect=_mock_run), \
             patch("time.sleep"), \
             patch("coord.interactive.time.monotonic", side_effect=_mono):
            result = _inject_briefing_into_tmux_session(
                "coord-quiescent", "my brief", timeout=60.0
            )

        assert result is True
        load_calls = [c for c in calls if "load-buffer" in c]
        assert load_calls, "load-buffer not called — injection did not happen"

    # -- Subprocess errors -----------------------------------------------------

    def test_returns_false_when_temp_file_creation_fails(self) -> None:
        """``False`` is returned when NamedTemporaryFile raises OSError."""
        from coord.interactive import _inject_briefing_into_tmux_session

        with patch("tempfile.NamedTemporaryFile", side_effect=OSError("disk full")):
            result = _inject_briefing_into_tmux_session("coord-ses", "hello")
        assert result is False

    def test_capture_pane_error_breaks_loop_but_still_injects(self) -> None:
        """SubprocessError from capture-pane breaks the quiescence loop but injection still runs."""
        from coord.interactive import _inject_briefing_into_tmux_session

        inject_calls: list[list[str]] = []

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            if "capture-pane" in cmd:
                raise subprocess_error()
            inject_calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        import subprocess as _sp

        subprocess_error = _sp.SubprocessError

        with patch("subprocess.run", side_effect=_mock_run), patch("time.sleep"):
            result = _inject_briefing_into_tmux_session(
                "coord-ses", "hello", timeout=2.0
            )

        assert result is True
        assert any("load-buffer" in c for c in inject_calls)
        assert any("paste-buffer" in c for c in inject_calls)

    # -- Temp file cleanup -----------------------------------------------------

    def test_temp_file_deleted_after_successful_injection(self) -> None:
        """The temp file written by load-buffer is deleted in the finally block."""
        from coord.interactive import _inject_briefing_into_tmux_session

        unlinked: list[str] = []

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_mock_run), \
             patch("time.sleep"), \
             patch("os.unlink", side_effect=lambda p: unlinked.append(p)):
            _inject_briefing_into_tmux_session("coord-clean", "hello world", timeout=0.0)

        assert len(unlinked) == 1, f"expected one unlink, got {unlinked!r}"
        assert unlinked[0].endswith(".txt"), (
            f"expected a .txt temp file path, got {unlinked[0]!r}"
        )

    def test_temp_file_deleted_even_when_subprocess_raises(self) -> None:
        """Cleanup runs even if subprocess.run raises during injection."""
        from coord.interactive import _inject_briefing_into_tmux_session

        unlinked: list[str] = []

        import subprocess as _sp

        call_count = [0]

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            call_count[0] += 1
            if call_count[0] >= 2:  # fail on load-buffer / paste-buffer
                raise _sp.SubprocessError("network error")
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_mock_run), \
             patch("time.sleep"), \
             patch("os.unlink", side_effect=lambda p: unlinked.append(p)):
            _inject_briefing_into_tmux_session("coord-clean2", "hello", timeout=0.0)

        # unlink should have run (finally block always executes)
        assert len(unlinked) >= 1
