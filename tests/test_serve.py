"""Tests for the portable control-center read path (#584): DAO, daemon, client."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord import client as coord_client
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.serve_app import build_app


def _make_file_db(path: Path) -> None:
    """Create a real on-disk coord.db with a couple of representative rows.

    Writer commits and closes before the read-only SqliteStore opens it, so the
    main DB file holds the data (no WAL handshake needed for the test).
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, files_allowed, smoke_tests, "
        "review_findings, briefing) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "work1", "laptop", "api", 42, "A work issue", "done", "work",
            '["a.py", "b.py"]', '["run the tests", "click the button"]',
            None, "x" * 5000,  # large briefing — must be dropped from the projection
        ),
    )
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, review_of_assignment_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("rev1", "server", "api", 42, "Review of #42", "done", "review", "work1"),
    )
    conn.execute(
        "INSERT INTO machines (name, host, capabilities, repos) VALUES (?,?,?,?)",
        ("laptop", "laptop.tailnet", '["python"]', '["api"]'),
    )
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '7')")
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
        "('pipeline_default_gates', '[\"review\", \"test\", \"merge\"]')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def file_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_file_db(p)
    return p


# ── DAO ─────────────────────────────────────────────────────────────────────

def test_dao_decodes_json_drops_briefing_and_reads_meta(file_db: Path):
    proj = SqliteStore(file_db).board_projection()
    assert proj["schema_version"] == 1
    assert proj["round_number"] == 7
    work = next(a for a in proj["assignments"] if a["assignment_id"] == "work1")
    # JSON columns decoded to native objects, not strings.
    assert work["files_allowed"] == ["a.py", "b.py"]
    assert work["smoke_tests"] == ["run the tests", "click the button"]
    # briefing dropped to keep the payload small (TUI never reads it).
    assert "briefing" not in work
    # columns absent from the Assignment dataclass are still served raw.
    assert "exit_code" in work and "test_plan" in work
    assert {m["name"] for m in proj["machines"]} == {"laptop"}
    assert proj["machines"][0]["repos"] == ["api"]
    assert proj["board_meta"]["pipeline_default_gates"] == '["review", "test", "merge"]'


def test_dao_is_read_only(file_db: Path):
    conn = SqliteStore(file_db)._connect()
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO board_meta (key, value) VALUES ('x', 'y')")
    conn.close()


def test_dao_write_methods_declared_but_unimplemented(file_db: Path):
    store = SqliteStore(file_db)
    with pytest.raises(NotImplementedError):
        store.record_result(object())


# ── Daemon (serve_app) ────────────────────────────────────────────────────────

def test_serve_endpoints(file_db: Path, valid_config_path: Path):
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        assert cli.get("/healthz").json()["status"] == "ok"
        board = cli.get("/board").json()
        assert board["round_number"] == 7
        assert any(a["assignment_id"] == "work1" for a in board["assignments"])
        cfg_resp = cli.get("/config")
        assert cfg_resp.status_code == 200
        assert "repos:" in cfg_resp.text  # raw coordinator.yml


def test_serve_bearer_auth(file_db: Path, valid_config_path: Path):
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg, token="s3cret")
    with TestClient(app) as cli:
        assert cli.get("/board").status_code == 401
        assert cli.get("/healthz").status_code == 200  # health is exempt
        ok = cli.get("/board", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200


# ── Client ────────────────────────────────────────────────────────────────────

def test_board_from_payload_matches_local_build(file_db: Path):
    payload = SqliteStore(file_db).board_projection()
    board = coord_client.board_from_payload(payload)
    assert board.round_number == 7
    work = board.find_by_id("work1")
    assert work is not None
    assert work.status == "done" and work.type == "work"
    assert work.files_allowed == ["a.py", "b.py"]
    assert work.briefing == ""  # dropped on the wire → mapper default
    # review_state inferred from the linked review assignment.
    assert work.review_state == "done"


def test_resolve_board_service_precedence(tmp_path: Path, monkeypatch):
    toml = tmp_path / "client.toml"
    toml.write_text('board_service = "http://fromfile:7435"\ntoken = "ft"\n')
    monkeypatch.setattr(coord_client, "CLIENT_TOML", toml)

    # file only
    monkeypatch.delenv("COORD_SERVICE_URL", raising=False)
    monkeypatch.delenv("COORD_TOKEN", raising=False)
    svc = coord_client.resolve_board_service()
    assert svc is not None and svc.url == "http://fromfile:7435" and svc.token == "ft"

    # env beats file
    monkeypatch.setenv("COORD_SERVICE_URL", "http://fromenv:7435/")
    svc = coord_client.resolve_board_service()
    assert svc.url == "http://fromenv:7435"  # trailing slash stripped

    # flag beats env
    svc = coord_client.resolve_board_service(flag_url="http://fromflag:7435")
    assert svc.url == "http://fromflag:7435"


def test_resolve_board_service_unset_returns_none(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(coord_client, "CLIENT_TOML", tmp_path / "nope.toml")
    monkeypatch.delenv("COORD_SERVICE_URL", raising=False)
    assert coord_client.resolve_board_service() is None
