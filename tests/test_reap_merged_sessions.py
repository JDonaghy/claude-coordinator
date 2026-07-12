"""Tests for _reap_merged_sessions_tick (#1110).

Black-box coverage at the tick-function level.  The DB is the autouse
in-memory fixture from conftest.py; tmux, _kill_session, and
finalize_interactive_exit are mocked so no external processes are invoked.

PATCHING NOTE: ``_reap_merged_sessions_tick`` uses LOCAL imports (PLC0415
pattern throughout the codebase), so functions must be patched at their
SOURCE modules — not at ``coord.serve_app.*``:

  - list_coord_tmux_sessions  → coord.interactive.list_coord_tmux_sessions
  - finalize_interactive_exit → coord.interactive.finalize_interactive_exit
  - _kill_session             → coord.diagnose._kill_session

This is the standard unittest.mock rule: patch where the name is LOOKED UP,
not where it is defined.  Since each is re-imported on every function call,
patching the source is equivalent to patching the reference.

Test matrix:
1. merged+detached → killed + finalized (the happy path)
2. merged+attached → NOT killed (operator still present)
3. not-merged (status='done') → NOT killed
4. merged non-merge-type (type='work') → NOT killed
5. flag off (auto_reap_merged=False) → no-op
6. no live tmux session → skipped (already gone)
7. _kill_session returns False → not in reaped list
8. ToS guardrail: no pane-text read / only _kill_session used for kill
9. One operational audit row written per reap
10. Empty board → empty return, no tmux probes
"""

from __future__ import annotations

import sqlite3
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ── Config helpers ────────────────────────────────────────────────────────────

_CONFIG_YAML = """\
repos:
  - name: myrepo
    github: acme/myrepo
    default_branch: main
machines:
  - name: localmachine
    host: localmachine.tailnet
    repos: [myrepo]
    repo_paths:
      myrepo: ~/src/myrepo
merge:
  auto_reap_merged: true
"""

_CONFIG_YAML_REAP_OFF = """\
repos:
  - name: myrepo
    github: acme/myrepo
    default_branch: main
machines:
  - name: localmachine
    host: localmachine.tailnet
    repos: [myrepo]
    repo_paths:
      myrepo: ~/src/myrepo
merge:
  auto_reap_merged: false
"""


def _load_config(yaml_text: str) -> Any:
    from coord.config import load as _load_cfg

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        f.flush()
        return _load_cfg(f.name)


def _insert_assignment(
    conn: sqlite3.Connection,
    assignment_id: str,
    *,
    status: str = "merged",
    atype: str = "merge",
    provider_name: str = "claude-pty",
    machine_name: str = "localmachine",
    repo_name: str = "myrepo",
    issue_number: int = 42,
) -> None:
    """Insert an assignment row into the in-memory DB (all columns needed)."""
    conn.execute(
        """INSERT INTO assignments
           (assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, type, provider_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            assignment_id,
            machine_name,
            repo_name,
            f"acme/{repo_name}",
            issue_number,
            "Test issue",
            status,
            atype,
            provider_name,
        ),
    )
    conn.commit()


def _fake_sessions(*, attached: bool) -> list[dict]:
    """Return a list_coord_tmux_sessions result for coord-test-aid-N."""
    return [
        {
            "session_name": "coord-test-aid-1",
            "pane_dead": "0",
            "attached": attached,
        }
    ]


# ── _reap_merged_sessions_tick tests ─────────────────────────────────────────


class TestReapMergedSessionsTick:
    """Black-box unit tests for the merged-session reaper tick."""

    # Common patch targets — _reap_merged_sessions_tick imports these locally
    # so they must be patched at their SOURCE modules (not coord.serve_app.*).
    _P_SESSIONS = "coord.interactive.list_coord_tmux_sessions"
    _P_FINALIZE = "coord.interactive.finalize_interactive_exit"
    _P_KILL = "coord.diagnose._kill_session"
    _P_AUDIT = "coord.audit.record_audit"

    def _aid_session(self, aid: str, *, attached: bool) -> list[dict]:
        return [
            {
                "session_name": f"coord-{aid}",
                "pane_dead": "0",
                "attached": attached,
            }
        ]

    def test_merged_detached_kills_and_finalizes(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """merged+detached → _kill_session called, finalize called."""
        from coord.serve_app import _reap_merged_sessions_tick

        _insert_assignment(coord_db, "test-aid-1", status="merged", atype="merge")
        cfg = _load_config(_CONFIG_YAML)

        finalize_mock = MagicMock(return_value=MagicMock())
        kill_mock = MagicMock(return_value=True)
        sessions_mock = MagicMock(
            return_value=self._aid_session("test-aid-1", attached=False)
        )

        with (
            patch("socket.gethostname", return_value="localmachine"),
            patch(self._P_SESSIONS, sessions_mock),
            patch(self._P_FINALIZE, finalize_mock),
            patch(self._P_KILL, kill_mock),
            patch(self._P_AUDIT),  # suppress DB write in audit
        ):
            reaped = _reap_merged_sessions_tick(cfg)

        assert reaped == ["test-aid-1"], (
            f"Expected ['test-aid-1'] reaped; got {reaped}"
        )
        kill_mock.assert_called_once()
        finalize_mock.assert_called_once()
        # The kill must be via _kill_session, not keystrokes — guaranteed by
        # the fact that only kill_mock was patched and it was called.

    def test_merged_attached_not_killed(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """merged+attached → session is skipped (operator still present)."""
        from coord.serve_app import _reap_merged_sessions_tick

        _insert_assignment(coord_db, "test-aid-2", status="merged", atype="merge")
        cfg = _load_config(_CONFIG_YAML)

        kill_mock = MagicMock(return_value=True)
        sessions_mock = MagicMock(
            return_value=self._aid_session("test-aid-2", attached=True)
        )

        with (
            patch("socket.gethostname", return_value="localmachine"),
            patch(self._P_SESSIONS, sessions_mock),
            patch(self._P_FINALIZE, MagicMock()),
            patch(self._P_KILL, kill_mock),
        ):
            reaped = _reap_merged_sessions_tick(cfg)

        assert reaped == [], f"Expected no reaps for attached session; got {reaped}"
        kill_mock.assert_not_called()

    def test_done_not_killed(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """not-merged (status='done') → session is NOT triggered."""
        from coord.serve_app import _reap_merged_sessions_tick

        _insert_assignment(coord_db, "test-aid-3", status="done", atype="merge")
        cfg = _load_config(_CONFIG_YAML)

        kill_mock = MagicMock(return_value=True)
        sessions_mock = MagicMock(
            return_value=self._aid_session("test-aid-3", attached=False)
        )

        with (
            patch(self._P_SESSIONS, sessions_mock),
            patch(self._P_FINALIZE, MagicMock()),
            patch(self._P_KILL, kill_mock),
        ):
            reaped = _reap_merged_sessions_tick(cfg)

        assert reaped == [], f"Expected no reaps for non-merged status; got {reaped}"
        kill_mock.assert_not_called()
        # list_coord_tmux_sessions is never called — candidates list is empty.
        sessions_mock.assert_not_called()

    def test_merged_work_type_not_killed(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """merged + type='work' (non-merge type) → NOT reaped."""
        from coord.serve_app import _reap_merged_sessions_tick

        _insert_assignment(
            coord_db, "test-aid-4", status="merged", atype="work",
            provider_name="claude-pty",
        )
        cfg = _load_config(_CONFIG_YAML)

        kill_mock = MagicMock(return_value=True)
        sessions_mock = MagicMock(
            return_value=self._aid_session("test-aid-4", attached=False)
        )

        with (
            patch(self._P_SESSIONS, sessions_mock),
            patch(self._P_FINALIZE, MagicMock()),
            patch(self._P_KILL, kill_mock),
        ):
            reaped = _reap_merged_sessions_tick(cfg)

        assert reaped == [], (
            f"Expected no reaps for non-merge type; got {reaped}"
        )
        kill_mock.assert_not_called()
        sessions_mock.assert_not_called()

    def test_flag_off_is_noop(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """auto_reap_merged=False → tick is a no-op regardless of board state."""
        from coord.serve_app import _reap_merged_sessions_tick

        _insert_assignment(coord_db, "test-aid-5", status="merged", atype="merge")
        cfg = _load_config(_CONFIG_YAML_REAP_OFF)

        assert not cfg.merge.auto_reap_merged, (
            "Config should have auto_reap_merged=False"
        )

        kill_mock = MagicMock(return_value=True)
        sessions_mock = MagicMock(
            return_value=self._aid_session("test-aid-5", attached=False)
        )

        with (
            patch(self._P_SESSIONS, sessions_mock),
            patch(self._P_FINALIZE, MagicMock()),
            patch(self._P_KILL, kill_mock),
        ):
            reaped = _reap_merged_sessions_tick(cfg)

        assert reaped == [], f"Expected no reaps when flag is off; got {reaped}"
        kill_mock.assert_not_called()
        sessions_mock.assert_not_called()

    def test_no_session_in_tmux_skips(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """merged+detached but no live tmux session → skipped (already gone)."""
        from coord.serve_app import _reap_merged_sessions_tick

        _insert_assignment(coord_db, "test-aid-6", status="merged", atype="merge")
        cfg = _load_config(_CONFIG_YAML)

        kill_mock = MagicMock(return_value=True)
        # Empty list = no live tmux sessions
        sessions_mock = MagicMock(return_value=[])

        with (
            patch("socket.gethostname", return_value="localmachine"),
            patch(self._P_SESSIONS, sessions_mock),
            patch(self._P_FINALIZE, MagicMock()),
            patch(self._P_KILL, kill_mock),
        ):
            reaped = _reap_merged_sessions_tick(cfg)

        assert reaped == [], f"Expected no reaps when no tmux session exists; got {reaped}"
        kill_mock.assert_not_called()

    def test_kill_failure_not_reaped(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """merged+detached but _kill_session returns False → not in reaped list."""
        from coord.serve_app import _reap_merged_sessions_tick

        _insert_assignment(coord_db, "test-aid-7", status="merged", atype="merge")
        cfg = _load_config(_CONFIG_YAML)

        kill_mock = MagicMock(return_value=False)  # kill fails
        sessions_mock = MagicMock(
            return_value=self._aid_session("test-aid-7", attached=False)
        )

        with (
            patch("socket.gethostname", return_value="localmachine"),
            patch(self._P_SESSIONS, sessions_mock),
            patch(self._P_FINALIZE, MagicMock()),
            patch(self._P_KILL, kill_mock),
            patch(self._P_AUDIT),
        ):
            reaped = _reap_merged_sessions_tick(cfg)

        assert reaped == [], f"Expected empty reap list on kill failure; got {reaped}"
        kill_mock.assert_called_once()

    def test_no_pane_text_read_only_kill_session_used(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """ToS guardrail: no pane text is read; only _kill_session is used to kill."""
        from coord.serve_app import _reap_merged_sessions_tick

        _insert_assignment(coord_db, "test-aid-8", status="merged", atype="merge")
        cfg = _load_config(_CONFIG_YAML)

        subprocess_mock = MagicMock()
        kill_mock = MagicMock(return_value=True)
        sessions_mock = MagicMock(
            return_value=self._aid_session("test-aid-8", attached=False)
        )

        with (
            patch("socket.gethostname", return_value="localmachine"),
            patch(self._P_SESSIONS, sessions_mock),
            patch(self._P_FINALIZE, MagicMock()),
            patch(self._P_KILL, kill_mock),
            patch(self._P_AUDIT),
            # Ensure subprocess.run is NOT called (no keystroke injection).
            # _kill_session is mocked, so subprocess.run inside it is never reached.
            patch("subprocess.run", subprocess_mock),
        ):
            reaped = _reap_merged_sessions_tick(cfg)

        assert reaped == ["test-aid-8"]
        # subprocess.run must NOT be called — kill must go through _kill_session only.
        subprocess_mock.assert_not_called()
        kill_mock.assert_called_once()

    def test_audit_row_written_on_reap(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """One operational audit row is written per reaped session."""
        from coord.serve_app import _reap_merged_sessions_tick

        _insert_assignment(coord_db, "test-aid-9", status="merged", atype="merge")
        cfg = _load_config(_CONFIG_YAML)

        audit_calls: list[dict] = []

        def _capture_audit(**kwargs: Any) -> None:
            audit_calls.append(kwargs)

        with (
            patch("socket.gethostname", return_value="localmachine"),
            patch(self._P_SESSIONS,
                  MagicMock(return_value=self._aid_session("test-aid-9", attached=False))),
            patch(self._P_FINALIZE, MagicMock()),
            patch(self._P_KILL, MagicMock(return_value=True)),
            patch(self._P_AUDIT, _capture_audit),
        ):
            reaped = _reap_merged_sessions_tick(cfg)

        assert reaped == ["test-aid-9"]
        assert len(audit_calls) == 1, f"Expected 1 audit row; got {audit_calls}"
        row = audit_calls[0]
        assert row["tier"] == "operational"
        assert row["category"] == "session"
        assert row["event_type"] == "reap_merged_session"
        assert row["actor"] == "daemon"
        assert row["assignment_id"] == "test-aid-9"

    def test_empty_board_returns_empty(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """No assignments in DB → empty return, no tmux probes."""
        from coord.serve_app import _reap_merged_sessions_tick

        cfg = _load_config(_CONFIG_YAML)
        sessions_mock = MagicMock()

        with patch(self._P_SESSIONS, sessions_mock):
            reaped = _reap_merged_sessions_tick(cfg)

        assert reaped == []
        sessions_mock.assert_not_called()


# ── Config parsing tests ───────────────────────────────────────────────────────


class TestAutoReapMergedConfig:
    """Unit tests for the merge.auto_reap_merged config flag."""

    def test_default_true_when_merge_block_absent(self) -> None:
        """auto_reap_merged defaults to True when no merge: block exists."""
        yaml = """\
repos:
  - name: r
    github: a/r
machines:
  - name: m
    host: m.tailnet
    repos: [r]
"""
        cfg = _load_config(yaml)
        assert cfg.merge.auto_reap_merged is True

    def test_explicit_true(self) -> None:
        """merge.auto_reap_merged: true is parsed correctly."""
        cfg = _load_config(_CONFIG_YAML)
        assert cfg.merge.auto_reap_merged is True

    def test_explicit_false(self) -> None:
        """merge.auto_reap_merged: false is parsed correctly."""
        cfg = _load_config(_CONFIG_YAML_REAP_OFF)
        assert cfg.merge.auto_reap_merged is False

    def test_invalid_type_raises(self) -> None:
        """merge.auto_reap_merged: 1 (integer) raises ConfigError."""
        from coord.config import ConfigError

        yaml = """\
repos:
  - name: r
    github: a/r
machines:
  - name: m
    host: m.tailnet
    repos: [r]
merge:
  auto_reap_merged: 1
"""
        with pytest.raises(ConfigError, match="boolean"):
            _load_config(yaml)


# ── dispatch_workers type='merge' tests ───────────────────────────────────────


class TestMergeAssignmentType:
    """Verify type='merge' is used for interactive merge sessions."""

    def test_merge_type_in_claim_work_types(self) -> None:
        """'merge' is in _WORK_TYPES so has_active_work_followup blocks review."""
        from coord.claim import has_active_work_followup
        from coord.models import Assignment, Board

        a = Assignment(
            assignment_id="merge-aid",
            machine_name="m",
            repo_name="r",
            issue_number=1,
            issue_title="t",
            status="running",
            type="merge",
        )
        board = Board(active=[a], completed=[])
        assert has_active_work_followup(board, repo_name="r", issue_number=1) is True

    def test_merge_type_in_diagnose_stage_types(self) -> None:
        """'merge' is listed in STAGE_ASSIGNMENT_TYPES['merge']."""
        from coord.diagnose import STAGE_ASSIGNMENT_TYPES

        assert "merge" in STAGE_ASSIGNMENT_TYPES["merge"], (
            f"'merge' missing from STAGE_ASSIGNMENT_TYPES['merge']: "
            f"{STAGE_ASSIGNMENT_TYPES['merge']}"
        )
