"""#1791: `coord status` (the thin-client CLI path) still renders correctly
against a TRIMMED `/board` payload — the collection-cardinality bound
`coord.board_wire.bound_board_payload` adds on top of #1337's per-row width
bound.

This is the "acceptance" test named in #1791's issue body: "`coord status`,
`coord-tui`, and the webapp still render correctly against a trimmed
payload; anything needing full history uses the detail endpoints." This
file covers the CLI path — `coord status` fetches `/board` through
`coord.client.fetch_board_payload` exactly like the daemon's real thin
clients do, so a real (not hand-built) trimmed payload — produced by the
real `/board` endpoint over a DB with thousands of terminal rows — must
round-trip through `board_from_payload` and render without the CLI raising
or losing the still-active work.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from click.testing import CliRunner
from starlette.testclient import TestClient

import coord.network as network_mod
from coord import client as cc
from coord.board_wire import MAX_TERMINAL_ASSIGNMENTS
from coord.commands.status import status as status_cmd
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.network import MachineStatus
from coord.serve_app import build_app


def _seed_big_board(path: Path, *, terminal_rows: int) -> None:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    now = time.time()
    findings = json.dumps({"verdict": "request-changes", "body": "F" * 5_000})
    rows = [
        (
            f"term{i}", "laptop", "api", i, f"issue {i}", "done", "work",
            now - i, now - i, "b" * 10_000, findings, "t" * 4_000, "s" * 4_000,
        )
        for i in range(terminal_rows)
    ]
    conn.executemany(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, dispatched_at, finished_at, "
        "briefing, review_findings, test_reason, smoke_test_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "synced_at) VALUES (?,?,?,?,?,?,?)",
        [
            ("api", i, f"issue {i}", "B" * 8000, "closed", "[]", now)
            for i in range(terminal_rows)
        ],
    )
    # One still-live piece of active work — must survive the trim.
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, dispatched_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("live1", "laptop", "api", 424242, "Still in flight", "running", "work", now),
    )
    conn.commit()
    conn.close()


def _fetch_real_trimmed_payload(db_path: Path, valid_config_path: Path) -> dict:
    """Build the payload the way the real daemon does: seed a DB well over
    MAX_TERMINAL_ASSIGNMENTS, then hit the real /board endpoint (not a
    hand-authored dict) so the CLI test exercises the actual wire the daemon
    produces."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(db_path), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board")
    assert resp.status_code == 200
    return resp.json()


def test_coord_status_renders_against_a_trimmed_board(
    tmp_path: Path, valid_config_path: Path, monkeypatch, coord_db,
) -> None:
    terminal_rows = MAX_TERMINAL_ASSIGNMENTS + 300
    db_path = tmp_path / "big.db"
    _seed_big_board(db_path, terminal_rows=terminal_rows)
    payload = _fetch_real_trimmed_payload(db_path, valid_config_path)

    # Sanity: this really is a trimmed payload, not the whole history —
    # otherwise this test would pass vacuously.
    assert payload["board_truncated"] is True
    assert len(payload["assignments"]) < terminal_rows
    assert any(a["assignment_id"] == "live1" for a in payload["assignments"])

    # Route `coord status` through the thin-client path, serving the SAME
    # trimmed payload the real daemon just produced above.
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://daemon:7435")
    )
    monkeypatch.setattr(cc, "fetch_board_payload", lambda *a, **k: payload)
    # `_load_config` fetches the daemon's config on a thin client (#1080) —
    # stub it to the same local coordinator.yml so this test doesn't need a
    # live daemon just to resolve `cfg`.
    monkeypatch.setattr(cc, "fetch_remote_config", lambda *a, **k: valid_config_path)

    # No live machines to poll — this test is about the /board render path,
    # not per-machine /status polling (covered elsewhere).
    def _fake_check_all(machines, timeout=3.0, **kw):
        return [MachineStatus(machine=m, state="offline", reason="test") for m in machines]

    monkeypatch.setattr(network_mod, "check_all", _fake_check_all)

    runner = CliRunner()
    result = runner.invoke(
        status_cmd,
        ["--config", str(valid_config_path), "--no-reconcile"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    # The board rendered — machine section present — and didn't choke on the
    # trimmed shape (no traceback, no crash on a missing/dropped field).
    assert "Machines:" in result.output
    assert "laptop" in result.output
    assert "server" in result.output
