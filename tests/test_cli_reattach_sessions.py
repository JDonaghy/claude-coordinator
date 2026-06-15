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
        "type": "work",
        "branch": None,
    }
    vals.update(overrides)
    conn.execute(
        """INSERT INTO assignments
           (assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, type, branch)
           VALUES (:assignment_id, :machine_name, :repo_name, :repo_github,
                   :issue_number, :issue_title, :status, :type, :branch)""",
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

    def test_json_metadata_from_daemon_on_thin_client(self, monkeypatch: Any) -> None:
        """#601: on a thin client (board_service set), session metadata is
        enriched from the daemon's board, not the retired local DB — otherwise
        issue_number/repo_name come back null and the TUI can't match the
        session to its issue row to offer reattach."""
        from coord import client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://d:7435"),
        )
        monkeypatch.setattr(
            cc, "fetch_board_payload",
            lambda svc, **k: {
                "assignments": [{
                    "assignment_id": "rev-aid", "issue_number": 494,
                    "repo_name": "vimcode", "issue_title": "[review] focus",
                    "machine_name": "precision",
                }]
            },
        )
        raw = [{"session_name": "coord-rev-aid"}]
        with patch("coord.interactive.list_coord_tmux_sessions", return_value=raw):
            result = CliRunner().invoke(main, ["sessions", "--json"])
        assert result.exit_code == 0, result.output
        s = json.loads(result.output)["sessions"][0]
        assert s["issue_number"] == 494
        assert s["repo_name"] == "vimcode"
        assert s["machine"] == "precision"

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

    # -- #486 Leg 4: remote discovery (--remote) -------------------------------

    _TWO_MACHINE_YAML = """\
repos:
  - name: myrepo
    github: acme/myrepo
    default_branch: main
machines:
  - name: local-box
    host: local-box.tailnet
    repos: [myrepo]
  - name: server
    host: server.tailnet
    repos: [myrepo]
"""

    def test_remote_flag_discovers_and_tags_remote_session(
        self, tmp_path: Path, coord_db: Any
    ) -> None:
        """`--remote` probes non-local machines over ssh+tmux and surfaces their
        sessions, tagged with the machine they live on (resolved from the DB)."""
        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(self._TWO_MACHINE_YAML)
        _insert_assignment(
            coord_db, "remote-aid",
            machine_name="server", repo_name="myrepo",
            repo_github="acme/myrepo", issue_number=7,
            issue_title="Remote work",
        )

        def fake_list(*, host: Any = None) -> list[dict[str, str]]:
            # Local probe (no/None ssh_target) → empty; the remote machine
            # ("server.tailnet") → one live session.
            if host is not None and getattr(host, "ssh_target", None):
                return [{"session_name": "coord-remote-aid"}]
            return []

        with patch("socket.gethostname", return_value="local-box"), \
             patch("coord.interactive.list_coord_tmux_sessions", side_effect=fake_list):
            result = CliRunner().invoke(
                main, ["sessions", "--json", "--remote", "--config", str(cfg)]
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["sessions"]) == 1
        s = data["sessions"][0]
        assert s["assignment_id"] == "remote-aid"
        assert s["machine"] == "server"
        assert s["issue_number"] == 7

    def test_remote_flag_survives_probe_failure(
        self, tmp_path: Path, coord_db: Any
    ) -> None:
        """A remote probe that raises must not crash the command — local
        sessions still list and exit is 0."""
        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(self._TWO_MACHINE_YAML)

        def fake_list(*, host: Any = None) -> list[dict[str, str]]:
            if host is not None and getattr(host, "ssh_target", None):
                raise RuntimeError("ssh blew up")
            return [{"session_name": "coord-local-aid"}]

        with patch("socket.gethostname", return_value="local-box"), \
             patch("coord.interactive.list_coord_tmux_sessions", side_effect=fake_list):
            result = CliRunner().invoke(
                main, ["sessions", "--json", "--remote", "--config", str(cfg)]
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        ids = {s["assignment_id"] for s in data["sessions"]}
        assert ids == {"local-aid"}


# ── coord reattach ─────────────────────────────────────────────────────────────


class TestReattachCmd:
    """``coord reattach <assignment_id>`` — LOCAL session reattach.

    The single configured machine (``mymachine``) is made to resolve as the
    local machine so these tests exercise the local tmux path (#486 Leg 4 made
    reattach locality-aware; the remote path is covered in TestReattachRemote).
    """

    @pytest.fixture(autouse=True)
    def _local_machine(self):
        with patch("socket.gethostname", return_value="mymachine"):
            yield

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

    def test_attach_calls_correct_tmux_command(
        self, config_file: Path, monkeypatch
    ) -> None:
        """Outside tmux, subprocess.run gets ``tmux attach-session -t coord-<id>``."""
        monkeypatch.delenv("TMUX", raising=False)  # ensure the non-nested path
        captured: list[list[str]] = []

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            captured.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            return m

        alive_seq = iter([True, True])  # alive before attach; alive after (detached)

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n, host=None: next(alive_seq, True)), \
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

    def test_attach_uses_switch_client_when_nested_in_tmux(
        self, config_file: Path, monkeypatch
    ) -> None:
        """Inside tmux ($TMUX set), reattach must use ``switch-client`` — a
        nested ``attach-session`` refuses ("sessions should be nested with
        care") and orphans the session."""
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
        captured: list[list[str]] = []

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            captured.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            return m

        alive_seq = iter([True, True])

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n, host=None: next(alive_seq, True)), \
             patch("subprocess.run", side_effect=_mock_run):
            CliRunner().invoke(
                main, ["reattach", "aid-xyz", "--config", str(config_file)]
            )

        switch_cmds = [c for c in captured if "switch-client" in c]
        assert switch_cmds, f"expected a switch-client call when nested; got {captured}"
        assert switch_cmds[0][0] == "tmux"
        assert "coord-aid-xyz" in switch_cmds[0]
        # And it must NOT have tried a nested attach-session.
        assert not [c for c in captured if "attach-session" in c]

    # -- Session still alive after detach (Ctrl-b d) ---------------------------

    def test_session_alive_after_detach_prints_reattach_hint(
        self, config_file: Path
    ) -> None:
        """When user detaches (Ctrl-b d), session stays alive → reattach message shown."""
        attach_result = MagicMock()
        attach_result.returncode = 0

        alive_seq = iter([True, True])  # alive before; alive after (just detached)

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n, host=None: next(alive_seq, True)), \
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
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n, host=None: next(alive_seq, True)), \
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
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n, host=None: next(alive_seq, False)), \
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
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n, host=None: next(alive_seq, False)), \
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
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n, host=None: next(alive_seq, False)), \
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
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n, host=None: next(alive_seq, False)), \
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
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n, host=None: next(alive_seq, False)), \
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
             patch("coord.interactive.tmux_session_alive", side_effect=lambda _n, host=None: next(alive_seq, False)), \
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


class TestReattachRemote:
    """#486 Leg 4: ``coord reattach`` to a session on a REMOTE machine.

    The local hostname differs from the configured machine, so ``mymachine``
    resolves as remote: the attach goes over ``ssh -t`` and the finalize is
    routed by assignment type (review → DB-only; fix → remote push-back)."""

    @pytest.fixture(autouse=True)
    def _remote_machine(self):
        # Any hostname that is NOT "mymachine"/"mymachine.tailnet".
        with patch("socket.gethostname", return_value="laptop"):
            yield

    def test_remote_review_attaches_over_ssh_and_finalizes_db_only(
        self, config_file: Path, coord_db: Any
    ) -> None:
        _insert_assignment(
            coord_db, "aid-rev",
            type="review", branch="issue-42-fix", machine_name="mymachine",
        )
        captured: list[list[str]] = []

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            captured.append(list(cmd))
            return MagicMock(returncode=0)

        alive_seq = iter([True, False])  # alive before; ended after attach
        fake = MagicMock(already_recorded=False, terminal_status="advisory")

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive",
                   side_effect=lambda _n, host=None: next(alive_seq, False)), \
             patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.finalize_interactive_exit",
                   return_value=fake) as mock_fin, \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_remote_fin:
            result = CliRunner().invoke(
                main, ["reattach", "aid-rev", "--config", str(config_file)]
            )

        assert result.exit_code == 0, output_and_stderr(result)
        # Attached over ssh -t to the remote tmux session.
        ssh_attach = [c for c in captured if "ssh" in c and "attach-session" in c]
        assert ssh_attach, f"expected an ssh attach; got {captured}"
        cmd = ssh_attach[0]
        assert cmd[0] == "ssh"
        assert "-t" in cmd
        assert "mymachine.tailnet" in cmd
        assert "coord-aid-rev" in cmd
        # Read-only review ⇒ DB-only finalize (no worktree), no remote push-back.
        mock_remote_fin.assert_not_called()
        mock_fin.assert_called_once()
        kwargs = mock_fin.call_args[1]
        assert kwargs["worktree_path"] is None
        assert kwargs["repo_path"] is None

    def test_remote_fix_pushes_back_via_remote_finalize(
        self, config_file: Path, coord_db: Any
    ) -> None:
        _insert_assignment(
            coord_db, "aid-fix",
            type="fix", branch="issue-42-fix", machine_name="mymachine",
        )
        alive_seq = iter([True, False])
        fake_remote = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=2, push_ok=True, push_error=None,
        )

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive",
                   side_effect=lambda _n, host=None: next(alive_seq, False)), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("coord.interactive.finalize_interactive_exit") as mock_fin, \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=fake_remote) as mock_remote_fin:
            result = CliRunner().invoke(
                main, ["reattach", "aid-fix", "--config", str(config_file)]
            )

        assert result.exit_code == 0, output_and_stderr(result)
        # A fix wrote in a remote worktree ⇒ push-back via the remote backstop.
        mock_fin.assert_not_called()
        mock_remote_fin.assert_called_once()
        kwargs = mock_remote_fin.call_args[1]
        assert kwargs["ssh_target"] == "mymachine.tailnet"
        assert kwargs["branch"] == "issue-42-fix"
        assert "aid-fix" in kwargs["remote_worktree_sh"]
        assert kwargs["remote_repo_sh"]  # resolved from repo_paths
        assert "remote backstop" in result.output

    def test_remote_work_pushes_back_via_remote_finalize(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """#486d: reattaching to a remote WORK session (not just fix) must push
        its commits back on session-end, using the recorded branch."""
        _insert_assignment(
            coord_db, "aid-work",
            type="work", branch="issue-42-classify-fix", machine_name="mymachine",
        )
        alive_seq = iter([True, False])
        fake_remote = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=3, push_ok=True, push_error=None,
        )

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive",
                   side_effect=lambda _n, host=None: next(alive_seq, False)), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("coord.interactive.finalize_interactive_exit") as mock_fin, \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=fake_remote) as mock_remote_fin:
            result = CliRunner().invoke(
                main, ["reattach", "aid-work", "--config", str(config_file)]
            )

        assert result.exit_code == 0, output_and_stderr(result)
        mock_fin.assert_not_called()
        mock_remote_fin.assert_called_once()
        kwargs = mock_remote_fin.call_args[1]
        assert kwargs["branch"] == "issue-42-classify-fix"
        assert kwargs["ssh_target"] == "mymachine.tailnet"

    def test_remote_fix_null_branch_derives_from_worktree_head(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """#557 defensive backstop: when a remote fix/work row has branch=NULL
        (pre-fix dispatch), _run_finalize derives the branch via
        `ssh … git rev-parse --abbrev-ref HEAD` and uses it for the
        remote push-back instead of falling to the DB-only path."""
        # Assignment recorded WITHOUT a branch (simulates old dispatch bug).
        _insert_assignment(
            coord_db, "aid-null-branch",
            type="work", branch=None, machine_name="mymachine",
        )
        alive_seq = iter([True, False])  # alive before attach; gone after
        fake_remote = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=2, push_ok=True, push_error=None,
        )

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            m = MagicMock(returncode=0)
            # The ssh git rev-parse probe: capture-pane calls don't reach here
            # (we mock tmux_session_alive); only the ssh attach and the branch
            # probe pass through subprocess.run in this path.
            if "rev-parse" in " ".join(cmd):
                m.stdout = "issue-42-derived-branch\n"
            else:
                m.stdout = ""
            return m

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive",
                   side_effect=lambda _n, host=None: next(alive_seq, False)), \
             patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.finalize_interactive_exit") as mock_fin, \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=fake_remote) as mock_remote_fin:
            result = CliRunner().invoke(
                main, ["reattach", "aid-null-branch", "--config", str(config_file)]
            )

        assert result.exit_code == 0, output_and_stderr(result)
        # Must have used the remote push-back, NOT the DB-only fallback.
        mock_remote_fin.assert_called_once()
        mock_fin.assert_not_called()
        kwargs = mock_remote_fin.call_args[1]
        assert kwargs["branch"] == "issue-42-derived-branch", (
            "branch should be derived from remote worktree HEAD, not left as None"
        )
        assert kwargs["ssh_target"] == "mymachine.tailnet"
        # Must emit the "derived from remote worktree HEAD" note.
        combined = result.output + (result.stderr or "")
        assert "derived from remote worktree HEAD" in combined, (
            "operator should see a note explaining the branch was derived"
        )

    def test_remote_work_null_branch_db_only_when_derive_fails(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """#557: when branch is NULL AND the ssh rev-parse probe fails (e.g.
        worktree already reaped), fall through gracefully to DB-only finalize
        — no crash, no unhandled exception."""
        _insert_assignment(
            coord_db, "aid-null-reaped",
            type="work", branch=None, machine_name="mymachine",
        )
        alive_seq = iter([True, False])
        fake_db = MagicMock(already_recorded=False, terminal_status="done")

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            m = MagicMock(returncode=0)
            if "rev-parse" in " ".join(cmd):
                # Simulate worktree already reaped — non-zero exit.
                m.returncode = 128
                m.stdout = ""
            else:
                m.stdout = ""
            return m

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive",
                   side_effect=lambda _n, host=None: next(alive_seq, False)), \
             patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.finalize_interactive_exit",
                   return_value=fake_db) as mock_fin, \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_remote_fin:
            result = CliRunner().invoke(
                main, ["reattach", "aid-null-reaped", "--config", str(config_file)]
            )

        assert result.exit_code == 0, output_and_stderr(result)
        # Branch could not be derived → falls back to DB-only.
        mock_remote_fin.assert_not_called()
        mock_fin.assert_called_once()
        # Still emits the "no branch recorded" note as before.
        combined = result.output + (result.stderr or "")
        assert "no branch recorded" in combined


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

    def test_load_buffer_reads_from_stdin(self) -> None:
        """``tmux load-buffer`` is called with ``"-"`` and ``input=briefing``."""
        from coord.interactive import _inject_briefing_into_tmux_session

        calls: list[dict[str, Any]] = []

        def _mock(cmd: list[str], **kwargs: Any) -> MagicMock:
            calls.append({"cmd": list(cmd), "kwargs": kwargs})
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_mock), patch("time.sleep"):
            result = _inject_briefing_into_tmux_session(
                "coord-ses", "my briefing text", timeout=0.0
            )

        assert result is True
        load_calls = [c for c in calls if "load-buffer" in c["cmd"]]
        assert load_calls, "load-buffer was not called"
        lc = load_calls[0]
        # The source argument must be "-" (stdin), NOT a temp file path.
        assert lc["cmd"][-1] == "-", (
            f"load-buffer last arg should be '-', got {lc['cmd'][-1]!r}"
        )
        # The briefing must be passed via the ``input`` kwarg, not a file.
        assert lc["kwargs"].get("input") == "my briefing text", (
            f"expected input='my briefing text', got {lc['kwargs'].get('input')!r}"
        )

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

    def test_no_temp_file_created_on_injection(self) -> None:
        """Briefing injection does NOT create any temporary files (stdin-only path)."""
        import tempfile as _tempfile

        from coord.interactive import _inject_briefing_into_tmux_session

        created_temp_files: list[str] = []
        original_ntf = _tempfile.NamedTemporaryFile

        def _spy_ntf(*args: Any, **kwargs: Any) -> Any:
            result = original_ntf(*args, **kwargs)
            created_temp_files.append(result.name)
            return result

        def _mock_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_mock_run), \
             patch("time.sleep"), \
             patch("tempfile.NamedTemporaryFile", side_effect=_spy_ntf):
            _inject_briefing_into_tmux_session("coord-clean", "hello world", timeout=0.0)

        assert created_temp_files == [], (
            f"stdin path must not create temp files; got: {created_temp_files!r}"
        )

    def test_load_buffer_input_strips_trailing_newline(self) -> None:
        """``input`` passed to load-buffer has trailing newline stripped."""
        from coord.interactive import _inject_briefing_into_tmux_session

        calls: list[dict[str, Any]] = []

        def _mock(cmd: list[str], **kwargs: Any) -> MagicMock:
            calls.append({"cmd": list(cmd), "kwargs": kwargs})
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        briefing = "some text\n\n"
        with patch("subprocess.run", side_effect=_mock), patch("time.sleep"):
            _inject_briefing_into_tmux_session("coord-nl", briefing, timeout=0.0)

        load_calls = [c for c in calls if "load-buffer" in c["cmd"]]
        assert load_calls
        # rstrip("\n") removes trailing newlines — matches briefing.rstrip("\n")
        assert load_calls[0]["kwargs"]["input"] == briefing.rstrip("\n")
