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


# ── Write path (#590): daemon endpoints ──────────────────────────────────────


def _seed_running_assignment(conn, aid: str = "work9", atype: str = "work") -> None:
    """An in-flight row the seam can transition to a terminal state."""
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "owner/api", 7, "An issue", "running", atype),
    )
    conn.commit()


@pytest.fixture
def rw_db(tmp_path: Path):
    """A thread-safe (file-backed, ``check_same_thread=False``) coord.db override.

    The autouse ``coord_db`` fixture installs a thread-bound ``:memory:`` conn,
    which TestClient (running the async handler on a worker thread) can't touch.
    Production ``get_connection`` already uses ``check_same_thread=False`` (a
    file DB), so this fixture mirrors production for the daemon-write endpoints.
    """
    from coord import db
    from coord.db import _ensure_schema

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn


def test_serve_post_result_records_terminal_state(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # The daemon's write path posts a GitHub comment via github_ops; stub it so
    # the test never shells out to `gh`.
    posted: list = []
    monkeypatch.setattr(
        "coord.github_ops.post_issue_comment",
        lambda repo, num, body: posted.append((repo, num, body)),
    )
    _seed_running_assignment(rw_db)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/result",
            json={
                "assignment_id": "work9",
                "machine_name": "laptop",
                "repo_name": "api",
                "repo_github": "owner/api",
                "issue_number": 7,
                "status": "done",
                "verdict": "approve",
                "summary": "looks good",
            },
        )
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "done" and out["posted"] is True
    # The shared DB (the daemon's get_connection target) saw the transition.
    row = rw_db.execute(
        "SELECT status, review_state, review_verdict FROM assignments "
        "WHERE assignment_id='work9'"
    ).fetchone()
    assert row["status"] == "done"
    assert row["review_state"] == "pending"
    assert row["review_verdict"] == "approve"
    # Notifications ledger written so `coord notify` won't double-post.
    led = rw_db.execute(
        "SELECT event FROM notifications WHERE assignment_id='work9'"
    ).fetchone()
    assert led is not None
    assert len(posted) == 1  # exactly one GitHub comment


def test_serve_post_completion_records_done(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **k: None)
    _seed_running_assignment(rw_db, aid="work10")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/completion",
            json={
                "assignment_id": "work10",
                "machine_name": "laptop",
                "repo_name": "api",
                "repo_github": "owner/api",
                "issue_number": 7,
                "exit_code": 0,
                "commits_ahead": 2,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"
    row = rw_db.execute(
        "SELECT status FROM assignments WHERE assignment_id='work10'"
    ).fetchone()
    assert row["status"] == "done"


def test_serve_post_result_rejects_bad_status(file_db: Path, valid_config_path: Path):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/result",
            json={
                "assignment_id": "x", "machine_name": "m", "repo_name": "r",
                "repo_github": "o/r", "issue_number": 1,
                "status": "bogus", "verdict": None, "summary": "",
            },
        )
    assert resp.status_code == 400


def test_serve_post_result_drops_unknown_keys(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # Forward-compat: a newer client may send a field this daemon doesn't know;
    # it must be dropped, not crash reconstruction.
    monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **k: None)
    _seed_running_assignment(rw_db, aid="work11")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/result",
            json={
                "assignment_id": "work11", "machine_name": "laptop",
                "repo_name": "api", "repo_github": "owner/api", "issue_number": 7,
                "status": "done", "verdict": None, "summary": "ok",
                "future_field_from_a_newer_client": {"nested": 1},
            },
        )
    assert resp.status_code == 200


def test_serve_writes_require_bearer(file_db: Path, valid_config_path: Path):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path), token="s3cret")
    with TestClient(app) as cli:
        assert cli.post("/result", json={}).status_code == 401
        assert cli.post("/completion", json={}).status_code == 401


# ── Write path (#590): seam routing ──────────────────────────────────────────


def test_post_result_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    """When board_service is set, the seam POSTs the record instead of writing
    the local DB."""
    from coord import client as cc
    from coord import issue_store

    _seed_running_assignment(coord_db, aid="work12")
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}

    def fake_post_record(svc, path, payload, **kw):
        captured["path"] = path
        captured["payload"] = payload
        return {"status": "done", "event": "completion", "posted": True, "error": None}

    monkeypatch.setattr(cc, "post_record", fake_post_record)

    outcome = issue_store.post_result(
        issue_store.ResultRecord(
            assignment_id="work12", machine_name="laptop", repo_name="api",
            repo_github="owner/api", issue_number=7, status="done",
            verdict="approve", summary="ok",
        )
    )
    assert outcome.status == "done"
    assert captured["path"] == "/result"
    assert captured["payload"]["assignment_id"] == "work12"
    # Routed → the local DB row was NOT touched (still running).
    row = coord_db.execute(
        "SELECT status FROM assignments WHERE assignment_id='work12'"
    ).fetchone()
    assert row["status"] == "running"


def test_post_completion_remote_failure_is_graceful(monkeypatch):
    """A daemon round-trip failure must not crash the launcher exit path."""
    from coord import client as cc
    from coord import issue_store

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )

    def boom(*a, **k):
        raise RuntimeError("daemon down")

    monkeypatch.setattr(cc, "post_record", boom)
    outcome = issue_store.post_completion(
        issue_store.CompletionRecord(
            assignment_id="w", machine_name="m", repo_name="r", repo_github="o/r",
            issue_number=1, exit_code=0, commits_ahead=1,
        )
    )
    assert outcome.status == "error" and outcome.posted is False
    assert "daemon down" in (outcome.error or "")


def test_resolve_serve_token_precedence(tmp_path: Path, monkeypatch):
    from coord import serve_app

    tok_file = tmp_path / "serve_token"
    monkeypatch.setattr(serve_app, "SERVE_TOKEN_FILE", tok_file)
    monkeypatch.delenv("COORD_SERVE_TOKEN", raising=False)

    # nothing configured → open daemon
    assert serve_app.resolve_serve_token() is None
    # file source (what systemd uses), trailing whitespace stripped
    tok_file.write_text("filetok\n")
    assert serve_app.resolve_serve_token() == "filetok"
    # env beats file
    monkeypatch.setenv("COORD_SERVE_TOKEN", "envtok")
    assert serve_app.resolve_serve_token() == "envtok"
    # flag beats env; blank flag is treated as unset (falls through)
    assert serve_app.resolve_serve_token("flagtok") == "flagtok"
    assert serve_app.resolve_serve_token("   ") == "envtok"


def test_post_result_unset_writes_local(coord_db, monkeypatch):
    """board_service unset → unchanged local-DB write (no regression)."""
    from coord import client as cc
    from coord import issue_store

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **k: None)
    _seed_running_assignment(coord_db, aid="work13")
    issue_store.post_result(
        issue_store.ResultRecord(
            assignment_id="work13", machine_name="laptop", repo_name="api",
            repo_github="owner/api", issue_number=7, status="done",
            verdict=None, summary="ok",
        )
    )
    row = coord_db.execute(
        "SELECT status FROM assignments WHERE assignment_id='work13'"
    ).fetchone()
    assert row["status"] == "done"
