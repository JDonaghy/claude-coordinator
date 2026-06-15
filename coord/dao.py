"""Storage-agnostic data-access layer for the coordinator board (#584/#589).

``CoordStore`` is the seam that lets the ``coord serve`` daemon front the board
without callers knowing the storage engine — and makes a future Postgres backend
(#282) a contained swap rather than a rewrite.  This module implements the
**read** side fully (the portable control center's read path) and declares the
**write** side for #590.

``SqliteStore`` is the concrete SQLite implementation.  It owns its *own*
read-only connection (NOT ``coord.db``'s read/write singleton): it opens the DB
with ``mode=ro`` + ``PRAGMA query_only`` and never runs schema/migration DDL, so
it is safe to point at a live ``coord.db`` that the coordinator process is
writing in WAL mode.

All SQLite idioms (``sqlite3.Row``, JSON-encoded TEXT columns, the ``mode=ro``
URI) are encapsulated here: read methods return plain Python dicts with JSON
columns decoded to native lists/objects, so neither the wire format nor a future
non-SQLite backend inherits any SQLite-only idiom.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from coord.db import DB_PATH

# Bump when the /board payload shape changes incompatibly.  Clients may branch
# on this; today everything is additive.
SCHEMA_VERSION = 1

# JSON-encoded columns per table — decoded to native objects on read so no
# SQLite idiom (JSON-in-TEXT) leaks past the DAO.  Columns added by later ALTER
# migrations are picked up automatically via ``SELECT *``; only JSON ones need
# listing here.
_JSON_COLUMNS: dict[str, set[str]] = {
    "assignments": {
        "files_allowed",
        "files_forbidden",
        "required_gates",
        "plan",
        "smoke_tests",
        "test_plan",
        # NOTE: review_findings is deliberately NOT decoded — the coord-tui
        # client consumes it as a raw JSON string (Option<String>), so it must
        # stay a string on the wire.
    },
    "proposals": {"files_likely", "required_gates"},
    "issues": {"labels"},
    "machines": {"capabilities", "repos"},
}

# Columns omitted from the board projection.  ``assignments.briefing`` is ~8 MB
# of an ~12 MB live payload and is NOT part of the board view (the TUI's board
# query never selects it; the Python mapper defaults it to ""), so dropping it
# keeps refreshes fast over Tailscale.  A per-assignment endpoint can serve full
# briefings later if a detail view needs them.
_DROP_COLUMNS: dict[str, set[str]] = {
    "assignments": {"briefing"},
}


@runtime_checkable
class CoordStore(Protocol):
    """Read/write interface over coordinator board state.

    The read methods are implemented today.  The write methods are declared so
    #590 (route writes through the daemon) lands as an implementation, not an
    interface change.
    """

    # ── reads (implemented now) ──────────────────────────────────────────────
    def list_assignments(self) -> list[dict]: ...
    def list_machines(self) -> list[dict]: ...
    def list_merge_queue(self) -> list[dict]: ...
    def list_proposals(self) -> list[dict]: ...
    def list_issues(self) -> list[dict]: ...
    def list_plans(self) -> dict[str, Any]: ...
    def list_notifications(self) -> list[dict]: ...
    def board_meta(self) -> dict[str, str]: ...
    def round_number(self) -> int: ...
    def board_projection(self) -> dict: ...

    # ── writes (declared; implemented in #590) ───────────────────────────────
    def record_result(self, record: Any) -> Any: ...
    def record_completion(self, record: Any) -> Any: ...
    def record_dispatched(self, assignment: Any) -> None: ...


def _decode_row(table: str, row: sqlite3.Row) -> dict:
    """sqlite3.Row → plain dict with that table's JSON columns decoded."""
    d = dict(row)
    for col in _DROP_COLUMNS.get(table, ()):
        d.pop(col, None)
    for col in _JSON_COLUMNS.get(table, ()):
        val = d.get(col)
        if isinstance(val, (str, bytes, bytearray)):
            try:
                d[col] = json.loads(val) if val else None
            except (json.JSONDecodeError, TypeError):
                d[col] = None
    return d


class SqliteStore:
    """Read-only SQLite-backed :class:`CoordStore`.

    Opens a fresh ``mode=ro`` connection per call (cheap for SQLite, thread-safe
    under the daemon's request handling, and never migrates the DB).
    ``board_projection`` opens a single connection so the whole payload is one
    consistent snapshot.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path is not None else DB_PATH

    # ── connection ────────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self._path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    # ── internal builders (take an open connection) ────────────────────────────
    def _table(self, conn: sqlite3.Connection, table: str, order: str | None = None) -> list[dict]:
        sql = f"SELECT * FROM {table}"  # noqa: S608 — table names are literals, not user input
        if order:
            sql += f" ORDER BY {order}"
        return [_decode_row(table, r) for r in conn.execute(sql).fetchall()]

    def _plans(self, conn: sqlite3.Connection) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in conn.execute("SELECT assignment_id, plan_data FROM plans").fetchall():
            try:
                out[r["assignment_id"]] = json.loads(r["plan_data"])
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def _board_meta(self, conn: sqlite3.Connection) -> dict[str, str]:
        # Served as raw strings; each client parses the keys it knows (the TUI
        # JSON-decodes pipeline_* keys, mirroring its local-SQLite behaviour).
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM board_meta").fetchall()}

    def _round_number(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM board_meta WHERE key = 'round_number'").fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    # ── public reads ────────────────────────────────────────────────────────────
    def list_assignments(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "assignments", order="dispatched_at DESC")

    def list_machines(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "machines", order="name")

    def list_merge_queue(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "merge_queue", order="id")

    def list_proposals(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "proposals", order="id")

    def list_issues(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "issues", order="repo_name, number")

    def list_plans(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            return self._plans(conn)

    def list_notifications(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return self._table(conn, "notifications")

    def board_meta(self) -> dict[str, str]:
        with closing(self._connect()) as conn:
            return self._board_meta(conn)

    def round_number(self) -> int:
        with closing(self._connect()) as conn:
            return self._round_number(conn)

    def board_projection(self) -> dict:
        """The full ``GET /board`` payload — one consistent snapshot.

        A superset of what the Rust TUI's ``load_data()`` reads from SQLite
        today, minus the live machine-reachability probes (the client keeps
        doing those itself over the tailnet).
        """
        with closing(self._connect()) as conn:
            return {
                "schema_version": SCHEMA_VERSION,
                "round_number": self._round_number(conn),
                "assignments": self._table(conn, "assignments", order="dispatched_at DESC"),
                "machines": self._table(conn, "machines", order="name"),
                "merge_queue": self._table(conn, "merge_queue", order="id"),
                "proposals": self._table(conn, "proposals", order="id"),
                "issues": self._table(conn, "issues", order="repo_name, number"),
                "plans": self._plans(conn),
                "notifications": self._table(conn, "notifications"),
                "board_meta": self._board_meta(conn),
            }

    # ── writes (deferred to #590) ────────────────────────────────────────────────
    def _not_yet(self, name: str) -> NotImplementedError:
        return NotImplementedError(
            f"{name}: writing through the daemon is part of #590 (route writes "
            "through the daemon). The read-path slice (#594) serves reads only; "
            "dispatch/record still run on the host."
        )

    def record_result(self, record: Any) -> Any:  # noqa: ARG002
        raise self._not_yet("record_result")

    def record_completion(self, record: Any) -> Any:  # noqa: ARG002
        raise self._not_yet("record_completion")

    def record_dispatched(self, assignment: Any) -> None:  # noqa: ARG002
        raise self._not_yet("record_dispatched")
