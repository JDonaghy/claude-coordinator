"""Tests for coord.db — schema creation, migration, connection override."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from coord import db as db_mod
from coord.db import _ensure_schema, override_connection, close, _migrate_gate_order


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_conn():
    """Each test in this file uses an in-memory DB via the coord_db fixture pattern."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    override_connection(conn)
    yield conn
    close()


# ── Schema creation ────────────────────────────────────────────────────────────

class TestSchemaCreation:
    EXPECTED_TABLES = {
        "schema_version",
        "assignments",
        "notifications",
        "proposals",
        "split_proposals",
        "split_chunks",
        "merge_queue",
        "plans",
        "sessions",
        "machines",
        "board_meta",
        "issue_comments",
    }

    def test_all_tables_exist(self, isolated_conn: sqlite3.Connection) -> None:
        rows = isolated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert self.EXPECTED_TABLES.issubset(names)

    def test_schema_version_row_inserted(self, isolated_conn: sqlite3.Connection) -> None:
        row = isolated_conn.execute("SELECT version FROM schema_version").fetchone()
        assert row is not None
        assert row["version"] == 1

    def test_indexes_exist(self, isolated_conn: sqlite3.Connection) -> None:
        rows = isolated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_assignments_status" in names
        assert "idx_assignments_machine" in names
        assert "idx_merge_queue_state" in names

    def test_idempotent_multiple_calls(self, isolated_conn: sqlite3.Connection) -> None:
        """Calling _ensure_schema again should not raise."""
        _ensure_schema(isolated_conn)  # second call
        rows = isolated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len(rows) >= len(self.EXPECTED_TABLES)


# ── issue_comments (#873) ────────────────────────────────────────────────────

class TestIssueCommentsSchema:
    def test_index_exists(self, isolated_conn: sqlite3.Connection) -> None:
        rows = isolated_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_issue_comments_issue" in names

    def test_gh_comment_id_unique(self, isolated_conn: sqlite3.Connection) -> None:
        isolated_conn.execute(
            "INSERT INTO issue_comments (gh_comment_id, repo_name, issue_number, body) "
            "VALUES (111, 'api', 1, 'first')"
        )
        isolated_conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            isolated_conn.execute(
                "INSERT INTO issue_comments (gh_comment_id, repo_name, issue_number, body) "
                "VALUES (111, 'api', 1, 'dupe')"
            )

    def test_multiple_null_gh_comment_ids_allowed(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """SQLite treats NULL as distinct under UNIQUE — rows whose comment
        id couldn't be resolved at capture-at-write time (rare) don't
        collide with each other."""
        isolated_conn.execute(
            "INSERT INTO issue_comments (repo_name, issue_number, body) "
            "VALUES ('api', 1, 'a')"
        )
        isolated_conn.execute(
            "INSERT INTO issue_comments (repo_name, issue_number, body) "
            "VALUES ('api', 1, 'b')"
        )
        isolated_conn.commit()
        count = isolated_conn.execute(
            "SELECT COUNT(*) c FROM issue_comments"
        ).fetchone()["c"]
        assert count == 2

    def test_body_ref_column_present_and_unused(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """The Azure-blob offload seam column exists but is never populated
        by current code — reserved for a future body_ref migration."""
        cols = {
            r[1] for r in isolated_conn.execute(
                "PRAGMA table_info(issue_comments)"
            ).fetchall()
        }
        assert "body_ref" in cols


# ── drive_queue deploy-gate columns (#1757) ───────────────────────────────────

# DQ-1 (#1753) shipped `drive_queue` WITHOUT the gate columns and merged on
# 2026-08-03, so the "fold them into the CREATE TABLE" window closed. The
# upgrade-in-place path below is therefore the one that runs on every existing
# ~/.coord/coord.db, and it is the one that must be tested — a fresh-DB-only
# test would pass while every real installation kept the old five-column-short
# table and every `coord drive-queue` read blew up on `no such column`.

_DQ1_ORIGINAL_TABLE = """
    CREATE TABLE drive_queue (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        repo_name     TEXT    NOT NULL,
        issue_number  INTEGER NOT NULL,
        position      INTEGER NOT NULL,
        machine       TEXT,
        after_json    TEXT    NOT NULL DEFAULT '[]',
        state         TEXT    NOT NULL DEFAULT 'waiting',
        attempts      INTEGER NOT NULL DEFAULT 0,
        deferrals     INTEGER NOT NULL DEFAULT 0,
        last_reason   TEXT    NOT NULL DEFAULT '',
        session_name  TEXT,
        launched_at   REAL,
        enqueued_at   REAL    NOT NULL,
        UNIQUE(repo_name, issue_number)
    )
"""

_GATE_COLUMNS = {
    "hold_after",
    "hold_reason",
    "resume_when",
    "hold_state",
    "hold_probes",
}


def _drive_queue_columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(drive_queue)").fetchall()}


class TestDriveQueueDeployGateColumns:
    def test_fresh_database_has_them_from_the_create(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        assert _GATE_COLUMNS <= _drive_queue_columns(isolated_conn)

    def test_existing_dq1_database_gains_them_in_place(self) -> None:
        """The real path: a coord.db created by DQ-1, upgraded by _ensure_schema.

        Built with DQ-1's ORIGINAL `CREATE TABLE` (not the current one) so this
        test keeps asserting the migration even after the CREATE grew the
        columns — otherwise `CREATE TABLE IF NOT EXISTS` would quietly make the
        migration untested.
        """
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_DQ1_ORIGINAL_TABLE)
        conn.execute(
            "INSERT INTO drive_queue "
            "(repo_name, issue_number, position, after_json, enqueued_at) "
            "VALUES ('api', 7, 0, '[]', 100.0)"
        )
        conn.commit()
        assert not (_GATE_COLUMNS & _drive_queue_columns(conn))

        _ensure_schema(conn)

        assert _GATE_COLUMNS <= _drive_queue_columns(conn)
        # The pre-existing row survives and reads as "no gate" — an upgraded
        # database must not spontaneously hold anybody's queue.
        row = conn.execute(
            "SELECT hold_after, hold_reason, resume_when, hold_state, hold_probes "
            "FROM drive_queue WHERE issue_number = 7"
        ).fetchone()
        assert row["hold_after"] == 0
        assert row["hold_reason"] == ""
        assert row["resume_when"] == ""
        assert row["hold_state"] == ""
        assert row["hold_probes"] == 0
        conn.close()

    def test_migration_is_idempotent(self) -> None:
        """Re-running _ensure_schema on an already-migrated DB must not raise."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_DQ1_ORIGINAL_TABLE)
        conn.commit()
        _ensure_schema(conn)
        _ensure_schema(conn)
        _ensure_schema(conn)
        assert _GATE_COLUMNS <= _drive_queue_columns(conn)
        conn.close()

    def test_state_accessors_read_the_upgraded_columns(self) -> None:
        """`_DRIVE_QUEUE_COLUMNS` names them, so a stale table would 500 here."""
        from coord import state

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(_DQ1_ORIGINAL_TABLE)
        conn.commit()
        _ensure_schema(conn)
        override_connection(conn)
        try:
            state._enqueue_drive_queue_local(
                "api", 9, hold_after=True, hold_reason="restart coord-serve"
            )
            entry = state._get_drive_queue_entry_local("api", 9)
        finally:
            close()
        assert entry["hold_after"] == 1
        assert entry["hold_reason"] == "restart coord-serve"
        assert entry["hold_state"] == "armed"


# ── override_connection ────────────────────────────────────────────────────────

class TestOverrideConnection:
    def test_override_makes_get_connection_return_override(self) -> None:
        from coord.db import get_connection

        fresh_conn = sqlite3.connect(":memory:")
        fresh_conn.row_factory = sqlite3.Row
        _ensure_schema(fresh_conn)
        override_connection(fresh_conn)
        try:
            assert get_connection() is fresh_conn
        finally:
            close()
            # Restore for other tests
            override_connection(sqlite3.connect(":memory:"))
            _ensure_schema(db_mod.get_connection())

    def test_close_resets_connection(self) -> None:
        from coord.db import get_connection

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        override_connection(conn)
        close()
        assert db_mod._conn is None
        # Restore
        _ensure_schema(sqlite3.connect(":memory:"))


# ── JSON migration ────────────────────────────────────────────────────────────

class TestJsonMigration:
    def _write_json(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_migration_imports_dispatched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When dispatched.json exists and assignments table is empty, it is migrated."""
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)

        dispatched = [
            {
                "assignment_id": "aaa",
                "machine_name": "laptop",
                "repo_name": "api",
                "repo_github": "acme/api",
                "issue_number": 1,
                "issue_title": "Fix auth",
                "files_likely": ["auth.py"],
                "briefing": "do it",
                "dispatched_at": 1000.0,
                "type": "work",
                "required_gates": [],
            }
        ]
        self._write_json(tmp_path / "dispatched.json", dispatched)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db_mod._maybe_migrate_json(conn)

        rows = conn.execute("SELECT * FROM assignments").fetchall()
        assert len(rows) == 1
        assert rows[0]["assignment_id"] == "aaa"
        assert rows[0]["machine_name"] == "laptop"

    def test_migration_imports_notified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)

        dispatched = [
            {
                "assignment_id": "bbb",
                "machine_name": "m", "repo_name": "api", "repo_github": "a/b",
                "issue_number": 2, "issue_title": "t", "files_likely": [],
                "briefing": "", "dispatched_at": 100.0, "type": "work",
                "required_gates": [],
            }
        ]
        notified = {"bbb": {"event": "completion", "posted_at": 200.0}}
        self._write_json(tmp_path / "dispatched.json", dispatched)
        self._write_json(tmp_path / "notified.json", notified)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db_mod._maybe_migrate_json(conn)

        n_rows = conn.execute("SELECT * FROM notifications").fetchall()
        assert len(n_rows) == 1
        assert n_rows[0]["assignment_id"] == "bbb"
        assert n_rows[0]["event"] == "completion"

    def test_migration_skipped_when_assignments_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_conn: sqlite3.Connection
    ) -> None:
        """Migration should not run when DB already has assignments."""
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)
        self._write_json(tmp_path / "dispatched.json", [])

        isolated_conn.execute(
            """INSERT INTO assignments
               (assignment_id, machine_name, repo_name, issue_number, issue_title)
               VALUES ('existing', 'm', 'r', 1, 't')"""
        )
        isolated_conn.commit()

        db_mod._maybe_migrate_json(isolated_conn)
        rows = isolated_conn.execute("SELECT * FROM assignments").fetchall()
        assert len(rows) == 1  # unchanged

    def test_migration_renames_json_to_bak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)
        self._write_json(tmp_path / "dispatched.json", [])

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db_mod._maybe_migrate_json(conn)

        assert not (tmp_path / "dispatched.json").exists()
        assert (tmp_path / "dispatched.json.bak").exists()

    def test_migration_skipped_when_no_dispatched_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_conn: sqlite3.Connection
    ) -> None:
        """If dispatched.json doesn't exist, migration is a no-op."""
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)
        # Don't create dispatched.json
        db_mod._maybe_migrate_json(isolated_conn)
        rows = isolated_conn.execute("SELECT * FROM assignments").fetchall()
        assert rows == []

    def test_migration_writes_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After migration, board_meta must contain a 'json_migrated' row."""
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)
        self._write_json(tmp_path / "dispatched.json", [])

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db_mod._maybe_migrate_json(conn)

        row = conn.execute(
            "SELECT value FROM board_meta WHERE key='json_migrated'"
        ).fetchone()
        assert row is not None, "json_migrated marker must be written after migration"
        # value should be a parseable float timestamp
        assert float(row["value"]) > 0

    def test_migration_does_not_retrigger_when_marker_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If json_migrated marker is present, migration must not run again — even when
        dispatched.json reappears and the assignments table is empty."""
        monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)

        # Plant the marker (simulates a prior successful migration)
        conn.execute(
            "INSERT INTO board_meta (key, value) VALUES ('json_migrated', '1000.0')"
        )
        conn.commit()

        # Simulate stale JSON file reappearing with data
        stale_dispatched = [
            {
                "assignment_id": "stale-001",
                "machine_name": "ghost",
                "repo_name": "api",
                "repo_github": "acme/api",
                "issue_number": 99,
                "issue_title": "Stale entry",
                "files_likely": [],
                "briefing": "",
                "dispatched_at": 9999.0,
                "type": "work",
                "required_gates": [],
            }
        ]
        self._write_json(tmp_path / "dispatched.json", stale_dispatched)

        # Assignments table is empty — the old guard would have triggered re-migration
        count_before = conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
        assert count_before == 0

        db_mod._maybe_migrate_json(conn)

        # Stale data must NOT have been imported
        rows = conn.execute("SELECT * FROM assignments").fetchall()
        assert len(rows) == 0, (
            "Migration re-triggered after marker was set; stale data was imported"
        )


# ── Gate-order migration (Test-before-Review reorder) ─────────────────────────

class TestMigrateGateOrder:
    """_migrate_gate_order rewrites the old default gate JSON in stored rows.

    Direction: the #520-era default ``["review", "test", "merge"]`` is rewritten
    to the new Test-before-Review default ``["test", "review", "merge"]``.
    """

    _OLD = '["review", "test", "merge"]'
    _NEW = '["test", "review", "merge"]'
    _CUSTOM = '["review", "merge"]'  # should never be touched

    def _insert_assignment(
        self,
        conn: sqlite3.Connection,
        aid: str,
        required_gates: str,
    ) -> None:
        conn.execute(
            "INSERT INTO assignments "
            "(assignment_id, machine_name, repo_name, issue_number, issue_title, required_gates) "
            "VALUES (?, 'm', 'r', 1, 't', ?)",
            (aid, required_gates),
        )
        conn.commit()

    def _insert_proposal(
        self,
        conn: sqlite3.Connection,
        pid: int,
        required_gates: str,
    ) -> None:
        conn.execute(
            "INSERT INTO proposals "
            "(id, machine_name, repo_name, issue_number, issue_title, required_gates) "
            "VALUES (?, 'm', 'r', 1, 't', ?)",
            (pid, required_gates),
        )
        conn.commit()

    def _set_board_meta(self, conn: sqlite3.Connection, value: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('pipeline_default_gates', ?)",
            (value,),
        )
        conn.commit()

    def test_rewrites_old_default_in_assignments(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Assignments storing the old default gate order are rewritten."""
        self._insert_assignment(isolated_conn, "a1", self._OLD)
        _migrate_gate_order(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a1'"
        ).fetchone()
        assert row["required_gates"] == self._NEW

    def test_rewrites_old_default_in_proposals(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Proposals storing the old default gate order are rewritten."""
        self._insert_proposal(isolated_conn, 1, self._OLD)
        _migrate_gate_order(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM proposals WHERE id=1"
        ).fetchone()
        assert row["required_gates"] == self._NEW

    def test_rewrites_board_meta_pipeline_default_gates(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """board_meta['pipeline_default_gates'] is updated when it holds the old value."""
        self._set_board_meta(isolated_conn, self._OLD)
        _migrate_gate_order(isolated_conn)
        row = isolated_conn.execute(
            "SELECT value FROM board_meta WHERE key='pipeline_default_gates'"
        ).fetchone()
        assert row["value"] == self._NEW

    def test_does_not_touch_custom_gate_lists(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Assignments with user-customised gate lists are left unchanged."""
        self._insert_assignment(isolated_conn, "a2", self._CUSTOM)
        _migrate_gate_order(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a2'"
        ).fetchone()
        assert row["required_gates"] == self._CUSTOM

    def test_idempotent(self, isolated_conn: sqlite3.Connection) -> None:
        """Running the migration twice produces the same result."""
        self._insert_assignment(isolated_conn, "a3", self._OLD)
        _migrate_gate_order(isolated_conn)
        _migrate_gate_order(isolated_conn)  # second call — no-op
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a3'"
        ).fetchone()
        assert row["required_gates"] == self._NEW

    def test_noop_when_already_new_order(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Rows already storing the new order are not affected."""
        self._insert_assignment(isolated_conn, "a4", self._NEW)
        _migrate_gate_order(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a4'"
        ).fetchone()
        assert row["required_gates"] == self._NEW

    def test_board_meta_absent_is_noop(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """If pipeline_default_gates is absent from board_meta, migration is a no-op."""
        _migrate_gate_order(isolated_conn)  # no board_meta row — must not raise
        row = isolated_conn.execute(
            "SELECT value FROM board_meta WHERE key='pipeline_default_gates'"
        ).fetchone()
        assert row is None


# ── #1663: stranded review verdicts ───────────────────────────────────────────


class TestBackfillOrphanedReviewVerdicts:
    """#1663: ``run_drain`` captured verdicts on the review row and never
    propagated them to the parent work row, stranding eight rows across the
    2026-08-01 overnight batch (#1527 #1624 #1658 #1633 #1353) plus #544, #1078
    and #1122.  The backfill copies each verdict from the review row that
    actually earned it — it never synthesises one."""

    @staticmethod
    def _work(
        conn: sqlite3.Connection,
        aid: str,
        *,
        atype: str = "work",
        review_state: str | None = "dispatched",
        review_verdict: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type, status, review_state, review_verdict) "
            "VALUES (?, 'laptop', 'api', 42, 't', ?, 'done', ?, ?)",
            (aid, atype, review_state, review_verdict),
        )

    @staticmethod
    def _review(
        conn: sqlite3.Connection,
        aid: str,
        work_aid: str,
        verdict: str | None,
        *,
        status: str = "done",
        dispatched_at: float = 1000.0,
    ) -> None:
        conn.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type, status, review_of_assignment_id, "
            "review_verdict, dispatched_at) "
            "VALUES (?, 'laptop', 'api', 42, '[review] t', 'review', ?, ?, ?, ?)",
            (aid, status, work_aid, verdict, dispatched_at),
        )

    @staticmethod
    def _row(conn: sqlite3.Connection, aid: str) -> dict:
        r = conn.execute(
            "SELECT review_state, review_verdict FROM assignments "
            "WHERE assignment_id=?",
            (aid,),
        ).fetchone()
        return {"review_state": r["review_state"], "review_verdict": r["review_verdict"]}

    def test_copies_approve_from_the_review_row(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """#1527's shape: work at dispatched/NULL, review at done/approve."""
        self._work(isolated_conn, "28d54c5b8873")
        self._review(isolated_conn, "6415c03e6ea2", "28d54c5b8873", "approve")

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 1
        assert self._row(isolated_conn, "28d54c5b8873") == {
            "review_state": "done", "review_verdict": "approve",
        }

    def test_copies_request_changes_from_the_review_row(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """#544 / #1078's shape.  A request-changes must be copied verbatim, not
        normalised to approve — the row is what tells a human a fix is owed."""
        self._work(isolated_conn, "ff4927937695")
        self._review(
            isolated_conn, "cb64561942fc", "ff4927937695", "request-changes",
        )

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 1
        assert self._row(isolated_conn, "ff4927937695") == {
            "review_state": "done", "review_verdict": "request-changes",
        }

    def test_never_synthesises_a_verdict_the_review_row_does_not_carry(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """#1122's shape: the review row ``188ae219aca3`` FAILED and its verdict
        was lost entirely (#1636/#1658).  Its findings were recovered from the
        worker transcript by hand and posted to PR #1656 — a fabricated verdict
        here would overwrite that with a guess.  Nothing to copy ⇒ no write."""
        self._work(isolated_conn, "a822bbd9eae3")
        self._review(
            isolated_conn, "188ae219aca3", "a822bbd9eae3", None, status="failed",
        )

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
        assert self._row(isolated_conn, "a822bbd9eae3") == {
            "review_state": "dispatched", "review_verdict": None,
        }

    def test_copies_a_verdict_recovered_onto_a_failed_review_row(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """The converse: when a verdict WAS recovered by hand onto a failed
        review row (#617's transcript recovery), it was earned by a real review
        and must be carried across.  Hence no ``status='done'`` filter."""
        self._work(isolated_conn, "wk-recovered")
        self._review(
            isolated_conn, "rev-recovered", "wk-recovered", "request-changes",
            status="failed",
        )

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 1
        assert self._row(isolated_conn, "wk-recovered")["review_verdict"] == (
            "request-changes"
        )

    def test_takes_the_latest_round_when_a_row_was_reviewed_twice(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        self._work(isolated_conn, "wk-2rounds")
        self._review(
            isolated_conn, "rev-r1", "wk-2rounds", "request-changes",
            dispatched_at=1000.0,
        )
        self._review(
            isolated_conn, "rev-r2", "wk-2rounds", "approve", dispatched_at=2000.0,
        )

        db_mod._backfill_orphaned_review_verdicts(isolated_conn)
        assert self._row(isolated_conn, "wk-2rounds")["review_verdict"] == "approve"

    def test_leaves_a_work_row_that_already_has_a_verdict_alone(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        self._work(
            isolated_conn, "wk-has", review_state="done", review_verdict="approve",
        )
        self._review(
            isolated_conn, "rev-has", "wk-has", "request-changes",
        )

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
        assert self._row(isolated_conn, "wk-has")["review_verdict"] == "approve"

    def test_leaves_rows_whose_review_stage_never_ran_alone(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """``pending`` / ``advisory`` / NULL means no review ran or it was
        waived — not stranded, and not ours to stamp."""
        for state in ("pending", "advisory", None):
            aid = f"wk-{state}"
            self._work(isolated_conn, aid, review_state=state)
            self._review(isolated_conn, f"rev-{state}", aid, "approve")

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
        for state in ("pending", "advisory", None):
            assert self._row(isolated_conn, f"wk-{state}")["review_verdict"] is None

    def test_ignores_non_work_like_rows(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Only WORK_LIKE_TYPES carry a parent review verdict.  A ``review`` or
        ``smoke`` row must never be rewritten by its own children."""
        self._work(isolated_conn, "sm-1", atype="smoke")
        self._review(isolated_conn, "rev-sm", "sm-1", "approve")

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
        assert self._row(isolated_conn, "sm-1")["review_verdict"] is None

    def test_covers_every_work_like_type(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        for atype in ("work", "mock-author", "test-author"):
            self._work(isolated_conn, f"wk-{atype}", atype=atype)
            self._review(isolated_conn, f"rev-{atype}", f"wk-{atype}", "approve")

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 3

    def test_is_idempotent(self, isolated_conn: sqlite3.Connection) -> None:
        self._work(isolated_conn, "wk-idem")
        self._review(isolated_conn, "rev-idem", "wk-idem", "approve")

        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 1
        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
        assert self._row(isolated_conn, "wk-idem")["review_verdict"] == "approve"

    def test_empty_db_is_a_noop(self, isolated_conn: sqlite3.Connection) -> None:
        assert db_mod._backfill_orphaned_review_verdicts(isolated_conn) == 0
