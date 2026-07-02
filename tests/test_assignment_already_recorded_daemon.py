"""#883: _assignment_already_recorded must read the assignment status from the
canonical DAEMON board on a thin client — not the stale local DB.

On a thin client a reviewer's `coord report-result` writes the terminal status
to the daemon, so a local-only read stays `running` and the finalize backstop
fires on every interactive review even after a clean report-result. These tests
exercise the real local-vs-daemon routing (they do not stub the function under
test)."""

from __future__ import annotations

import coord.client as cc
from coord.db import get_connection
from coord.interactive import _assignment_already_recorded, _assignment_status


class _FakeSvc:
    url = "http://daemon:7435"
    token = "t"


def _seed_local(assignment_id: str, status: str) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (assignment_id, "elitebook", "claude-coordinator", 883, "t", status),
    )
    conn.commit()


def _use_daemon(monkeypatch, assignments: list[dict]) -> None:
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    monkeypatch.setattr(cc, "fetch_board_payload", lambda svc, **kw: {"assignments": assignments})


def test_daemon_terminal_status_wins_over_stale_local_running(monkeypatch, coord_db) -> None:
    """The #547 case: local row still says 'running', but the daemon has the
    terminal write. already_recorded must be True (so the backstop won't fire)."""
    _seed_local("rev-883", "running")  # stale local
    _use_daemon(monkeypatch, [{"assignment_id": "rev-883", "status": "done"}])
    assert _assignment_status("rev-883") == "done"
    assert _assignment_already_recorded("rev-883") is True


def test_daemon_running_is_not_recorded(monkeypatch, coord_db) -> None:
    _use_daemon(monkeypatch, [{"assignment_id": "rev-883", "status": "running"}])
    assert _assignment_already_recorded("rev-883") is False


def test_daemon_absent_row_is_not_recorded(monkeypatch, coord_db) -> None:
    """board_service set + daemon has no such row ⇒ not recorded (canonical),
    and we do NOT fall back to a stale local terminal row."""
    _seed_local("rev-883", "done")  # stale local terminal row
    _use_daemon(monkeypatch, [])  # daemon: no such assignment
    assert _assignment_already_recorded("rev-883") is False


def test_local_fallback_when_no_board_service(coord_db) -> None:
    """Daemon host (board_service unset by the autouse fixture): read local DB."""
    _seed_local("rev-local", "done")
    assert _assignment_already_recorded("rev-local") is True


def test_local_fallback_when_daemon_unreachable(monkeypatch, coord_db) -> None:
    _seed_local("rev-883", "done")
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())

    def _boom(*a, **k):
        raise RuntimeError("daemon down")

    monkeypatch.setattr(cc, "fetch_board_payload", _boom)
    assert _assignment_already_recorded("rev-883") is True


def test_empty_assignment_id_is_false(coord_db) -> None:
    assert _assignment_already_recorded("") is False
