"""Tests for coord.db — schema creation, migration, connection override."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from coord import db as db_mod
from coord.db import _ensure_schema, override_connection, close


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


# ── Gate-order migration (#520) ───────────────────────────────────────────────

class TestGateOrderMigrationV520:
    """_migrate_gate_order_v520 rewrites the pre-#465 default gate list."""

    _OLD = '["test", "review", "merge"]'
    _NEW = '["review", "test", "merge"]'

    def _insert_assignment(
        self,
        conn: sqlite3.Connection,
        aid: str,
        gates: str,
    ) -> None:
        conn.execute(
            """INSERT INTO assignments
               (assignment_id, machine_name, repo_name, issue_number,
                issue_title, status, required_gates)
               VALUES (?, 'laptop', 'api', 1, 't', 'done', ?)""",
            (aid, gates),
        )
        conn.commit()

    def _insert_proposal(
        self,
        conn: sqlite3.Connection,
        machine: str,
        gates: str,
    ) -> None:
        conn.execute(
            """INSERT INTO proposals
               (machine_name, repo_name, issue_number, issue_title, required_gates)
               VALUES (?, 'api', 1, 't', ?)""",
            (machine, gates),
        )
        conn.commit()

    def test_rewrites_old_assignment_gates(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """assignment.required_gates with the old default is rewritten to new order."""
        self._insert_assignment(isolated_conn, "a1", self._OLD)
        db_mod._migrate_gate_order_v520(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a1'"
        ).fetchone()
        assert row["required_gates"] == self._NEW

    def test_rewrites_old_proposal_gates(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """proposals.required_gates with the old default is rewritten."""
        self._insert_proposal(isolated_conn, "laptop", self._OLD)
        db_mod._migrate_gate_order_v520(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM proposals WHERE machine_name='laptop'"
        ).fetchone()
        assert row["required_gates"] == self._NEW

    def test_rewrites_board_meta_pipeline_default_gates(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """board_meta['pipeline_default_gates'] with the old value is updated."""
        isolated_conn.execute(
            "INSERT INTO board_meta (key, value) VALUES ('pipeline_default_gates', ?)",
            (self._OLD,),
        )
        isolated_conn.commit()
        db_mod._migrate_gate_order_v520(isolated_conn)
        row = isolated_conn.execute(
            "SELECT value FROM board_meta WHERE key='pipeline_default_gates'"
        ).fetchone()
        assert row["value"] == self._NEW

    def test_does_not_touch_non_default_gates(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Assignments with custom gate lists are left untouched."""
        custom = '["merge"]'
        self._insert_assignment(isolated_conn, "a2", custom)
        db_mod._migrate_gate_order_v520(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a2'"
        ).fetchone()
        assert row["required_gates"] == custom

    def test_writes_marker(self, isolated_conn: sqlite3.Connection) -> None:
        """After migration the gate_order_v520 marker is written."""
        db_mod._migrate_gate_order_v520(isolated_conn)
        row = isolated_conn.execute(
            "SELECT value FROM board_meta WHERE key='gate_order_v520'"
        ).fetchone()
        assert row is not None

    def test_idempotent_on_second_call(self, isolated_conn: sqlite3.Connection) -> None:
        """Second call is a no-op: already-migrated rows are not double-migrated."""
        self._insert_assignment(isolated_conn, "a3", self._OLD)
        db_mod._migrate_gate_order_v520(isolated_conn)
        # Manually revert to simulate a row appearing after migration
        isolated_conn.execute(
            "UPDATE assignments SET required_gates=? WHERE assignment_id='a3'",
            (self._OLD,),
        )
        isolated_conn.commit()
        # Second call must not change the row again (marker present → skip)
        db_mod._migrate_gate_order_v520(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a3'"
        ).fetchone()
        assert row["required_gates"] == self._OLD, (
            "Migration ran again after marker was set"
        )

    def test_new_order_already_present_is_unchanged(
        self, isolated_conn: sqlite3.Connection
    ) -> None:
        """Rows already carrying the new gate order pass through unchanged."""
        self._insert_assignment(isolated_conn, "a4", self._NEW)
        db_mod._migrate_gate_order_v520(isolated_conn)
        row = isolated_conn.execute(
            "SELECT required_gates FROM assignments WHERE assignment_id='a4'"
        ).fetchone()
        assert row["required_gates"] == self._NEW
