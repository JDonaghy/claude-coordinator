"""#762: board projection cap + archival housekeeping.

These are black-box tests on the real read path (``SqliteStore.board_projection``)
and the real archival sweep (``coord.housekeeping.sweep``) — the wire/storage the
TUI and daemon actually use — not just the pure keep-set helper.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from coord.dao import SqliteStore, compute_board_keep_ids
from coord.db import _ensure_schema

NOW = time.time()
RECENT = NOW - 2 * 86400      # 2 days ago  → inside the 14d board window
OLD = NOW - 40 * 86400        # 40 days ago → outside both windows


def _ins_assignment(
    conn: sqlite3.Connection,
    aid: str,
    *,
    status: str,
    issue: int,
    repo: str = "r",
    atype: str = "work",
    dispatched_at: float | None = None,
    finished_at: float | None = None,
    review_of: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, dispatched_at, finished_at, "
        "review_of_assignment_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, "m", repo, issue, f"#{issue}", status, atype,
         dispatched_at, finished_at, review_of),
    )


def _ins_issue(conn: sqlite3.Connection, number: int, state: str, repo: str = "r") -> None:
    conn.execute(
        "INSERT INTO issues (repo_name, number, state) VALUES (?,?,?)",
        (repo, number, state),
    )


def _ins_merge_queue(conn: sqlite3.Connection, aid: str, issue: int, repo: str = "r") -> None:
    conn.execute(
        "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, branch, "
        "target_branch, issue_number, issue_title) VALUES (?,?,?,?,?,?,?)",
        (aid, repo, "gh/r", f"issue-{issue}", "main", issue, f"#{issue}"),
    )


# ── pure keep-set helper ──────────────────────────────────────────────────────

def test_compute_keep_ids_rules():
    cutoff = NOW - 14 * 86400
    index = [
        {"assignment_id": "active", "repo_name": "r", "issue_number": 1,
         "status": "running", "dispatched_at": OLD, "finished_at": None,
         "review_of_assignment_id": None},
        {"assignment_id": "recent", "repo_name": "r", "issue_number": 2,
         "status": "merged", "dispatched_at": RECENT, "finished_at": RECENT,
         "review_of_assignment_id": None},
        {"assignment_id": "old_closed", "repo_name": "r", "issue_number": 3,
         "status": "merged", "dispatched_at": OLD, "finished_at": OLD,
         "review_of_assignment_id": None},
        {"assignment_id": "old_queued", "repo_name": "r", "issue_number": 4,
         "status": "done", "dispatched_at": OLD, "finished_at": OLD,
         "review_of_assignment_id": None},
        {"assignment_id": "old_open_latest", "repo_name": "r", "issue_number": 5,
         "status": "done", "dispatched_at": OLD, "finished_at": OLD,
         "review_of_assignment_id": None},
        {"assignment_id": "review_of_recent", "repo_name": "r", "issue_number": 2,
         "status": "done", "dispatched_at": OLD, "finished_at": OLD,
         "review_of_assignment_id": "recent"},
    ]
    keep = compute_board_keep_ids(
        index,
        merge_queue_ids={"old_queued"},
        open_issue_keys={("r", 5)},
        cutoff=cutoff,
    )
    assert "active" in keep            # non-terminal
    assert "recent" in keep            # within window
    assert "old_queued" in keep        # referenced by merge_queue
    assert "old_open_latest" in keep   # latest assignment of an open issue
    assert "review_of_recent" in keep  # review-linked to a kept row (closure)
    assert "old_closed" not in keep    # stale terminal, closed issue → dropped


def test_compute_keep_ids_cutoff_none_keeps_all():
    index = [
        {"assignment_id": "a", "repo_name": "r", "issue_number": 1, "status": "merged",
         "dispatched_at": OLD, "finished_at": OLD, "review_of_assignment_id": None},
    ]
    keep = compute_board_keep_ids(index, set(), set(), cutoff=None)
    assert keep == {"a"}


# ── DAO board_projection (real read path, file-backed) ────────────────────────

@pytest.fixture
def file_db(tmp_path):
    path = tmp_path / "coord.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    yield path, conn
    conn.close()


def test_board_projection_caps_old_terminal(file_db, monkeypatch):
    monkeypatch.setenv("COORD_BOARD_RETENTION_DAYS", "14")
    path, conn = file_db
    _ins_assignment(conn, "active", status="running", issue=1, dispatched_at=OLD)
    _ins_assignment(conn, "recent", status="merged", issue=2,
                    dispatched_at=RECENT, finished_at=RECENT)
    _ins_assignment(conn, "old_closed", status="merged", issue=3,
                    dispatched_at=OLD, finished_at=OLD)
    _ins_issue(conn, 3, "closed")
    conn.commit()

    proj = SqliteStore(path).board_projection()
    ids = {a["assignment_id"] for a in proj["assignments"]}
    assert ids == {"active", "recent"}
    assert "old_closed" not in ids


def test_board_projection_keeps_open_issue_latest(file_db, monkeypatch):
    monkeypatch.setenv("COORD_BOARD_RETENTION_DAYS", "14")
    path, conn = file_db
    # Old terminal work, but the issue is still OPEN → its latest assignment stays.
    _ins_assignment(conn, "old_open", status="done", issue=7,
                    dispatched_at=OLD, finished_at=OLD)
    _ins_issue(conn, 7, "open")
    conn.commit()
    proj = SqliteStore(path).board_projection()
    assert {a["assignment_id"] for a in proj["assignments"]} == {"old_open"}


def test_board_projection_cap_disabled(file_db, monkeypatch):
    monkeypatch.setenv("COORD_BOARD_RETENTION_DAYS", "0")
    path, conn = file_db
    _ins_assignment(conn, "old_closed", status="merged", issue=3,
                    dispatched_at=OLD, finished_at=OLD)
    _ins_issue(conn, 3, "closed")
    conn.commit()
    proj = SqliteStore(path).board_projection()
    assert {a["assignment_id"] for a in proj["assignments"]} == {"old_closed"}


# ── housekeeping sweep (real write path, in-memory via coord_db) ──────────────

def test_housekeeping_archives_old_terminal(coord_db, monkeypatch):
    monkeypatch.setenv("COORD_ARCHIVE_RETENTION_DAYS", "30")
    from coord import housekeeping

    conn = coord_db
    _ins_assignment(conn, "active", status="running", issue=1, dispatched_at=OLD)
    _ins_assignment(conn, "recent", status="merged", issue=2,
                    dispatched_at=RECENT, finished_at=RECENT)
    _ins_assignment(conn, "old_closed", status="merged", issue=3,
                    dispatched_at=OLD, finished_at=OLD)
    _ins_issue(conn, 3, "closed")
    conn.commit()

    # dry-run reports but moves nothing
    dry = housekeeping.sweep(dry_run=True)
    assert dry["archived_assignments"] == 1 and dry["dry_run"] is True
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 3

    res = housekeeping.sweep()
    assert res["archived_assignments"] == 1
    live = {r[0] for r in conn.execute("SELECT assignment_id FROM assignments")}
    arch = {r[0] for r in conn.execute("SELECT assignment_id FROM assignments_archive")}
    assert live == {"active", "recent"}      # protected rows stay
    assert arch == {"old_closed"}            # stale terminal moved
    # conservation: nothing lost
    assert len(live) + len(arch) == 3


# ── daemon: gzip + /housekeeping endpoint ─────────────────────────────────────

class _FatStore:
    """Minimal CoordStore whose board is comfortably over the gzip threshold."""

    def board_projection(self) -> dict:
        return {
            "schema_version": 1,
            "round_number": 0,
            "assignments": [{"assignment_id": f"a{i}", "pad": "x" * 80} for i in range(60)],
            "machines": [],
            "merge_queue": [],
            "proposals": [],
            "issues": [],
            "plans": {},
            "notifications": [],
            "board_meta": {},
        }


def test_board_response_is_gzipped(valid_config_path):
    from starlette.testclient import TestClient

    from coord.config import load as load_config
    from coord.serve_app import build_app

    app = build_app(_FatStore(), load_config(valid_config_path))  # type: ignore[arg-type]
    with TestClient(app) as cli:
        resp = cli.get("/board", headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        # httpx transparently decodes, so the parsed body is still intact.
        assert len(resp.json()["assignments"]) == 60


def test_housekeeping_endpoint_routes_to_sweep(tmp_path, valid_config_path, monkeypatch):
    monkeypatch.setenv("COORD_ARCHIVE_RETENTION_DAYS", "30")
    from starlette.testclient import TestClient

    from coord import db
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    # Daemon runs the sweep in a threadpool, so the DB connection must be
    # thread-shareable (a file DB with check_same_thread=False, like production —
    # the autouse :memory: fixture conn is single-thread only).
    path = tmp_path / "coord.db"
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    _ins_assignment(conn, "old_closed", status="merged", issue=3,
                    dispatched_at=OLD, finished_at=OLD)
    _ins_issue(conn, 3, "closed")
    conn.commit()

    app = build_app(SqliteStore(path), load_config(valid_config_path))
    with TestClient(app) as cli:
        dry = cli.post("/housekeeping", json={"dry_run": True}).json()
        assert dry["archived_assignments"] == 1 and dry["dry_run"] is True
        live = cli.post("/housekeeping", json={}).json()
        assert live["archived_assignments"] == 1
    assert {r[0] for r in conn.execute("SELECT assignment_id FROM assignments_archive")} == {"old_closed"}


def test_housekeeping_never_archives_referenced(coord_db, monkeypatch):
    monkeypatch.setenv("COORD_ARCHIVE_RETENTION_DAYS", "30")
    from coord import housekeeping

    conn = coord_db
    # Old terminal, but queued for merge → must NOT be archived.
    _ins_assignment(conn, "old_queued", status="done", issue=4,
                    dispatched_at=OLD, finished_at=OLD)
    _ins_merge_queue(conn, "old_queued", 4)
    _ins_issue(conn, 4, "closed")
    conn.commit()

    res = housekeeping.sweep()
    assert res["archived_assignments"] == 0
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1
