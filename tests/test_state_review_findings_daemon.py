"""#877: ``load_assignment_review_findings`` must read the canonical DAEMON board
on a thin client — NOT the empty/stale local DB.

This is the #547 root cause: the review verdict + findings were captured on the
daemon board, but the verdict-relay backstop read the local DB (empty on a thin
client), missed them, and opened a blank editor.  These tests exercise the REAL
local-vs-daemon routing (they do not monkeypatch the function under test)."""

from __future__ import annotations

import json

import coord.client as cc
from coord.db import get_connection
from coord.state import load_assignment_review_findings


class _FakeSvc:
    url = "http://daemon:7435"
    token = "t"


def _seed_local(assignment_id: str, verdict: str, body: str) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, issue_number, issue_title, "
        " review_findings) VALUES (?, ?, ?, ?, ?, ?)",
        (
            assignment_id,
            "precision",
            "claude-coordinator",
            877,
            "t",
            json.dumps({"verdict": verdict, "body": body}),
        ),
    )
    conn.commit()


def _patch_point_endpoint(monkeypatch, payload: dict) -> None:
    """#1336: the findings read now prefers GET /assignment/{id}; serve both the
    point endpoint and the collection fallback from the same fake payload."""
    monkeypatch.setattr(
        cc,
        "fetch_assignment",
        lambda svc, aid, **kw: next(
            (
                a
                for a in payload.get("assignments", [])
                if a.get("assignment_id") == aid
            ),
            None,
        ),
    )
    monkeypatch.setattr(cc, "fetch_board_payload", lambda svc, **kw: payload)


def _daemon_payload(assignment_id: str, verdict: str, body: str) -> dict:
    return {
        "assignments": [
            {
                "assignment_id": assignment_id,
                "review_findings": json.dumps({"verdict": verdict, "body": body}),
            }
        ]
    }


def test_reads_daemon_board_when_board_service_set(monkeypatch, coord_db) -> None:
    """Thin-client mode: findings live on the daemon; the local DB has NO row.
    The read must return the daemon's findings, not fall through to local None."""
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    _patch_point_endpoint(
        monkeypatch, _daemon_payload("rev-877", "request-changes", "FULL body")
    )
    assert load_assignment_review_findings("rev-877") == (
        "request-changes",
        "FULL body",
    )


def test_daemon_findings_win_over_stale_local(monkeypatch, coord_db) -> None:
    """A stale local row must NOT shadow the canonical daemon value."""
    _seed_local("rev-877", "approve", "stale local body")
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    _patch_point_endpoint(
        monkeypatch, _daemon_payload("rev-877", "request-changes", "fresh daemon")
    )
    assert load_assignment_review_findings("rev-877") == (
        "request-changes",
        "fresh daemon",
    )


def test_daemon_canonical_absent_returns_none_not_local(monkeypatch, coord_db) -> None:
    """When board_service is set and the daemon has no such row, the answer is
    None (daemon is canonical) — we do NOT silently read a stale local row."""
    _seed_local("rev-877", "approve", "stale local body")
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
    _patch_point_endpoint(monkeypatch, {"assignments": []})
    assert load_assignment_review_findings("rev-877") is None


def test_falls_back_to_local_when_no_board_service(coord_db) -> None:
    """Daemon host (board_service unset by the autouse fixture): read local DB."""
    _seed_local("rev-local", "request-changes", "local body")
    assert load_assignment_review_findings("rev-local") == (
        "request-changes",
        "local body",
    )


def test_daemon_unreachable_falls_back_to_local(monkeypatch, coord_db) -> None:
    """If the daemon fetch raises, fall back to the local DB (best-effort)."""
    _seed_local("rev-877", "approve", "local body")
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())

    def _boom(*a, **k):
        raise RuntimeError("daemon down")

    monkeypatch.setattr(cc, "fetch_assignment", _boom)
    monkeypatch.setattr(cc, "fetch_board_payload", _boom)
    assert load_assignment_review_findings("rev-877") == ("approve", "local body")
