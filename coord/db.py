"""SQLite connection management and schema for coordinator state.

Single DB at ~/.coord/coord.db with WAL mode.  All coordinator state lives
here: assignments, proposals, merge queue, sessions, etc.

Usage
-----
- Production code: ``get_connection()`` returns the singleton connection.
- Tests: call ``override_connection(sqlite3.connect(":memory:"))`` then
  ``close()`` in teardown (the ``coord_db`` fixture in conftest.py does this).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

COORD_DIR = Path.home() / ".coord"
DB_PATH = COORD_DIR / "coord.db"

_conn: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """Return the module-level singleton connection, opening it on first call."""
    global _conn
    if _conn is None:
        _conn = _open(DB_PATH)
    return _conn


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    _maybe_migrate_json(conn)
    return conn


def override_connection(conn: sqlite3.Connection) -> None:
    """Replace the singleton connection.  Used in tests to inject :memory: DBs."""
    global _conn
    _conn = conn


def close() -> None:
    """Close the singleton connection and reset it to None."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ── Schema ────────────────────────────────────────────────────────────────────

def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes if they don't already exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS assignments (
            assignment_id TEXT PRIMARY KEY,
            machine_name TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_github TEXT,
            issue_number INTEGER NOT NULL,
            issue_title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            type TEXT NOT NULL DEFAULT 'work',
            branch TEXT,
            pr_url TEXT,
            briefing TEXT DEFAULT '',
            files_allowed TEXT DEFAULT '[]',
            files_forbidden TEXT DEFAULT '[]',
            model TEXT,
            dispatched_at REAL,
            finished_at REAL,
            smoke_test TEXT,
            smoke_test_reason TEXT,
            review_state TEXT,
            review_of_assignment_id TEXT,
            review_target TEXT,
            required_gates TEXT DEFAULT '[]',
            plan TEXT,
            unreachable_count INTEGER DEFAULT 0,
            exit_code INTEGER,
            review_iteration INTEGER DEFAULT 0,
            review_posted_at REAL,
            test_state TEXT,
            test_reason TEXT,
            cost_usd REAL,
            smoke_tests TEXT,
            review_findings TEXT
        );

        CREATE TABLE IF NOT EXISTS notifications (
            assignment_id TEXT PRIMARY KEY,
            event TEXT NOT NULL,
            branch TEXT,
            posted_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY,
            machine_name TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            issue_title TEXT NOT NULL,
            rationale TEXT DEFAULT '',
            files_likely TEXT DEFAULT '[]',
            briefing TEXT DEFAULT '',
            model TEXT,
            type TEXT DEFAULT 'work',
            required_gates TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS split_proposals (
            id INTEGER PRIMARY KEY,
            repo_name TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            issue_title TEXT NOT NULL,
            rationale TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS split_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            split_proposal_id INTEGER NOT NULL REFERENCES split_proposals(id),
            title TEXT NOT NULL,
            scope TEXT NOT NULL,
            files_likely TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS merge_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_github TEXT NOT NULL,
            branch TEXT NOT NULL,
            target_branch TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            issue_title TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            pr_number INTEGER,
            pr_url TEXT,
            size INTEGER,
            last_attempt REAL,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS plans (
            assignment_id TEXT PRIMARY KEY,
            plan_data TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            ended_at TEXT,
            clean_shutdown INTEGER DEFAULT 0,
            completed_this_session TEXT,
            issues_closed TEXT,
            total_cost_usd REAL
        );

        CREATE TABLE IF NOT EXISTS machines (
            name TEXT PRIMARY KEY,
            host TEXT NOT NULL,
            capabilities TEXT DEFAULT '[]',
            repos TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS board_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS issues (
            repo_name  TEXT    NOT NULL,
            number     INTEGER NOT NULL,
            title      TEXT    NOT NULL DEFAULT '',
            body       TEXT    NOT NULL DEFAULT '',
            state      TEXT    NOT NULL DEFAULT 'open',
            labels     TEXT    NOT NULL DEFAULT '[]',
            synced_at  REAL,
            PRIMARY KEY (repo_name, number)
        );

        CREATE INDEX IF NOT EXISTS idx_assignments_status ON assignments(status);
        CREATE INDEX IF NOT EXISTS idx_assignments_machine ON assignments(machine_name);
        CREATE INDEX IF NOT EXISTS idx_merge_queue_state ON merge_queue(state);

        INSERT OR IGNORE INTO schema_version VALUES (1);
    """)
    conn.commit()
    # Column-level migrations for existing databases.  SQLite does not support
    # "ADD COLUMN IF NOT EXISTS", so we catch OperationalError instead.
    _migrate_add_columns(conn)


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """Add new columns to existing databases via ALTER TABLE.

    Safe to call on databases that already have the columns — the
    OperationalError raised by SQLite is silently swallowed.
    """
    migrations = [
        "ALTER TABLE assignments ADD COLUMN review_iteration INTEGER DEFAULT 0",
        "ALTER TABLE assignments ADD COLUMN review_posted_at REAL",
        # #200: human-driven Test gate between Work and Review.
        "ALTER TABLE assignments ADD COLUMN test_state TEXT",
        "ALTER TABLE assignments ADD COLUMN test_reason TEXT",
        # #253: persisted adversarial-review verdict so the merge gate can
        # check approval without re-parsing logs after restart.
        "ALTER TABLE assignments ADD COLUMN review_verdict TEXT",
        # #208: worker cost captured from the final stream-json result event.
        "ALTER TABLE assignments ADD COLUMN cost_usd REAL",
        # #252: worker-emitted smoke-test list (JSON array of strings;
        # NULL = not emitted, '[]' = explicit "(none — internal)").
        "ALTER TABLE assignments ADD COLUMN smoke_tests TEXT",
        # #bounce: cached review-findings body (markdown text) so coord
        # bounce + the upcoming per-stage display don't have to re-fetch
        # the review log from the agent.  Populated by notify.py when
        # the review is first parsed.  NULL = not yet parsed; populated
        # = full findings.body text.
        "ALTER TABLE assignments ADD COLUMN review_findings TEXT",
    ]
    for sql in migrations:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists


# ── JSON migration ─────────────────────────────────────────────────────────────

def _maybe_migrate_json(conn: sqlite3.Connection) -> None:
    """Migrate old JSON files to SQLite if dispatched.json exists and DB is empty.

    The ``json_migrated`` marker in board_meta is checked first.  Once set it
    persists forever, so migration never re-runs even if JSON files reappear
    (e.g. from stale code, test fixtures, or an agent writing legacy state).
    """
    # Marker check must come first — bail out immediately if migration already ran.
    cursor = conn.execute(
        "SELECT value FROM board_meta WHERE key='json_migrated'"
    )
    if cursor.fetchone() is not None:
        return
    dispatched_json = COORD_DIR / "dispatched.json"
    if not dispatched_json.exists():
        return
    cursor = conn.execute("SELECT COUNT(*) FROM assignments")
    if cursor.fetchone()[0] > 0:
        return
    try:
        _migrate_json(conn)
    except Exception as exc:  # noqa: BLE001 — migration is best-effort
        import sys
        print(f"coord: warning: JSON→SQLite migration failed: {exc}", file=sys.stderr)


def _migrate_json(conn: sqlite3.Connection) -> None:  # noqa: C901 — acceptable complexity
    """One-shot migration from JSON files to SQLite.  Renames JSON files to .bak."""
    import time as _time

    dispatched_json = COORD_DIR / "dispatched.json"
    notified_json = COORD_DIR / "notified.json"
    board_json = COORD_DIR / "board.json"
    proposals_json = COORD_DIR / "pending_proposals.json"
    splits_json = COORD_DIR / "pending_splits.json"
    plans_json = COORD_DIR / "plans.json"
    session_json = COORD_DIR / "session.json"
    merge_queue_json = COORD_DIR / "merge_queue.json"

    with conn:  # single transaction
        # 1. dispatched.json → assignments (initial insert, status='running')
        dispatched_data: list[dict] = []
        if dispatched_json.exists():
            try:
                dispatched_data = json.loads(dispatched_json.read_text())
            except Exception:  # noqa: BLE001
                pass
        for rec in dispatched_data:
            conn.execute(
                """INSERT OR IGNORE INTO assignments (
                    assignment_id, machine_name, repo_name, repo_github,
                    issue_number, issue_title, status, type, briefing,
                    files_allowed, model, dispatched_at, review_of_assignment_id,
                    required_gates
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rec.get("assignment_id", ""),
                    rec.get("machine_name", ""),
                    rec.get("repo_name", ""),
                    rec.get("repo_github"),
                    rec.get("issue_number", 0),
                    rec.get("issue_title", ""),
                    rec.get("type", "work"),
                    rec.get("briefing", ""),
                    json.dumps(rec.get("files_likely", [])),
                    rec.get("model"),
                    rec.get("dispatched_at"),
                    rec.get("review_of_assignment_id"),
                    json.dumps(rec.get("required_gates", [])),
                ),
            )

        # 2. notified.json → notifications
        if notified_json.exists():
            try:
                notified: dict[str, dict] = json.loads(notified_json.read_text())
                for aid, info in notified.items():
                    conn.execute(
                        """INSERT OR REPLACE INTO notifications
                           (assignment_id, event, branch, posted_at)
                           VALUES (?, ?, ?, ?)""",
                        (aid, info.get("event", ""), info.get("branch"),
                         info.get("posted_at", _time.time())),
                    )
            except Exception:  # noqa: BLE001
                pass

        # 3. board.json → assignments (richer status fields via REPLACE)
        if board_json.exists():
            try:
                board_data = json.loads(board_json.read_text())
                all_entries = (
                    board_data.get("active", []) + board_data.get("completed", [])
                )
                for a in all_entries:
                    aid = a.get("assignment_id")
                    if not aid:
                        continue
                    conn.execute(
                        """INSERT OR REPLACE INTO assignments (
                            assignment_id, machine_name, repo_name, repo_github,
                            issue_number, issue_title, status, type, branch, pr_url,
                            briefing, files_allowed, files_forbidden, model,
                            dispatched_at, finished_at, smoke_test, smoke_test_reason,
                            review_state, review_of_assignment_id, review_target,
                            required_gates, plan, unreachable_count, exit_code
                        ) VALUES (
                            ?, ?, ?, ?,  ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?,  ?, ?, ?, ?,
                            ?, ?, ?,     ?, ?, ?, ?
                        )""",
                        (
                            aid,
                            a.get("machine_name", ""),
                            a.get("repo_name", ""),
                            None,  # repo_github not in board JSON
                            a.get("issue_number", 0),
                            a.get("issue_title", ""),
                            a.get("status", "running"),
                            a.get("type", "work"),
                            a.get("branch"),
                            a.get("pr_url"),
                            a.get("briefing", ""),
                            json.dumps(a.get("files_allowed", [])),
                            json.dumps(a.get("files_forbidden", [])),
                            a.get("model"),
                            a.get("dispatched_at"),
                            a.get("finished_at"),
                            a.get("smoke_test"),
                            a.get("smoke_test_reason"),
                            a.get("review_state"),
                            a.get("review_of_assignment_id"),
                            a.get("review_target"),
                            json.dumps(a.get("required_gates", [])),
                            json.dumps(a.get("plan")) if a.get("plan") else None,
                            a.get("unreachable_count", 0),
                            a.get("exit_code"),
                        ),
                    )
                round_number = board_data.get("round_number", 0)
                conn.execute(
                    "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', ?)",
                    (str(round_number),),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
                )
            except Exception:  # noqa: BLE001
                pass

        # 4. proposals
        if proposals_json.exists():
            try:
                for p in json.loads(proposals_json.read_text()):
                    conn.execute(
                        """INSERT OR IGNORE INTO proposals
                           (id, machine_name, repo_name, issue_number, issue_title,
                            rationale, files_likely, briefing, model, type, required_gates)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            p.get("id"), p.get("machine_name", ""), p.get("repo_name", ""),
                            p.get("issue_number", 0), p.get("issue_title", ""),
                            p.get("rationale", ""), json.dumps(p.get("files_likely", [])),
                            p.get("briefing", ""), p.get("model"),
                            p.get("type", "work"), json.dumps(p.get("required_gates", [])),
                        ),
                    )
            except Exception:  # noqa: BLE001
                pass

        # 5. split proposals + chunks
        if splits_json.exists():
            try:
                for s in json.loads(splits_json.read_text()):
                    conn.execute(
                        """INSERT OR IGNORE INTO split_proposals
                           (id, repo_name, issue_number, issue_title, rationale)
                           VALUES (?, ?, ?, ?, ?)""",
                        (s.get("id"), s.get("repo_name", ""), s.get("issue_number", 0),
                         s.get("issue_title", ""), s.get("rationale", "")),
                    )
                    for chunk in s.get("chunks", []):
                        conn.execute(
                            """INSERT INTO split_chunks
                               (split_proposal_id, title, scope, files_likely)
                               VALUES (?, ?, ?, ?)""",
                            (s.get("id"), chunk.get("title", ""), chunk.get("scope", ""),
                             json.dumps(chunk.get("files_likely", []))),
                        )
            except Exception:  # noqa: BLE001
                pass

        # 6. plans
        if plans_json.exists():
            try:
                for aid, plan_dict in json.loads(plans_json.read_text()).items():
                    conn.execute(
                        "INSERT OR IGNORE INTO plans (assignment_id, plan_data) VALUES (?, ?)",
                        (aid, json.dumps(plan_dict)),
                    )
            except Exception:  # noqa: BLE001
                pass

        # 7. session
        if session_json.exists():
            try:
                sess = json.loads(session_json.read_text())
                conn.execute(
                    """INSERT INTO sessions
                       (started_at, ended_at, clean_shutdown,
                        completed_this_session, issues_closed, total_cost_usd)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        sess.get("started_at"),
                        sess.get("ended_at"),
                        1 if sess.get("clean_shutdown") else 0,
                        json.dumps(sess.get("completed_this_session", [])),
                        json.dumps(sess.get("issues_closed", [])),
                        sess.get("total_cost_usd"),
                    ),
                )
            except Exception:  # noqa: BLE001
                pass

        # 8. merge queue
        if merge_queue_json.exists():
            try:
                for entry in json.loads(merge_queue_json.read_text()):
                    conn.execute(
                        """INSERT INTO merge_queue (
                            assignment_id, repo_name, repo_github, branch,
                            target_branch, issue_number, issue_title, state,
                            pr_number, pr_url, size, last_attempt, error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            entry.get("assignment_id"), entry.get("repo_name"),
                            entry.get("repo_github"), entry.get("branch"),
                            entry.get("target_branch"), entry.get("issue_number"),
                            entry.get("issue_title"), entry.get("state", "pending"),
                            entry.get("pr_number"), entry.get("pr_url"),
                            entry.get("size"), entry.get("last_attempt"),
                            entry.get("error"),
                        ),
                    )
            except Exception:  # noqa: BLE001
                pass

        # Persist migration marker — this is the canonical "already migrated" signal.
        # Checked at the top of _maybe_migrate_json(), so JSON files reappearing
        # later (stale code, test fixtures, agent writing legacy state) won't
        # re-trigger the migration.
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('json_migrated', ?)",
            (str(_time.time()),),
        )

        # Rename JSON files to .bak so migration doesn't re-run
        for f in [
            dispatched_json, notified_json, board_json, proposals_json,
            splits_json, plans_json, session_json, merge_queue_json,
        ]:
            if f.exists():
                try:
                    f.rename(f.with_suffix(f.suffix + ".bak"))
                except Exception:  # noqa: BLE001
                    pass
