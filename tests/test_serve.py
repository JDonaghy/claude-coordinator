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


# ── Write path (#590 Phase 2): dispatch + test-verdict ────────────────────────


def test_serve_dispatched_records_assignment_row(
    file_db: Path, valid_config_path: Path, rw_db
):
    from coord.models import Assignment

    a = Assignment(
        machine_name="precision", repo_name="api", issue_number=11,
        issue_title="thin-client dispatch", assignment_id="rev99", type="review",
        review_of_assignment_id="work1", branch="issue-11-x",
    )
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/dispatched",
            json={"assignment": __import__("dataclasses").asdict(a), "repo_github": "owner/api"},
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT status, type, review_of_assignment_id FROM assignments "
        "WHERE assignment_id='rev99'"
    ).fetchone()
    assert row["status"] == "running" and row["type"] == "review"
    assert row["review_of_assignment_id"] == "work1"


def test_serve_dispatched_work_records_row(
    file_db: Path, valid_config_path: Path, rw_db
):
    from coord.models import Proposal

    p = Proposal(
        id=1, machine_name="precision", repo_name="api", issue_number=12,
        issue_title="thin-client work", rationale="because",
    )
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/dispatched-work",
            json={
                "assignment_id": "work88",
                "proposal": __import__("dataclasses").asdict(p),
                "repo_github": "owner/api",
                "provider_name": "claude",
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT status, provider_name FROM assignments WHERE assignment_id='work88'"
    ).fetchone()
    assert row["status"] == "running" and row["provider_name"] == "claude"


def test_serve_test_verdict_records(file_db: Path, valid_config_path: Path, rw_db):
    _seed_running_assignment(rw_db, aid="work77")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/test-verdict",
            json={
                "assignment_id": "work77", "test_state": "failed",
                "test_reason": "scroll broke", "smoke_test": "fail",
                "smoke_test_reason": "scroll broke",
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT test_state, test_reason, smoke_test FROM assignments "
        "WHERE assignment_id='work77'"
    ).fetchone()
    assert row["test_state"] == "failed" and row["smoke_test"] == "fail"
    assert row["test_reason"] == "scroll broke"


def test_record_dispatched_assignment_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state
    from coord.models import Assignment

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload) or {"ok": True},
    )
    state.record_dispatched_assignment(
        assignment=Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="zzz", type="review",
        ),
        repo_github="o/api",
    )
    assert captured["path"] == "/dispatched"
    assert captured["payload"]["assignment"]["assignment_id"] == "zzz"
    # Routed → no local row created.
    assert coord_db.execute(
        "SELECT COUNT(*) c FROM assignments WHERE assignment_id='zzz'"
    ).fetchone()["c"] == 0


def test_record_test_verdict_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload) or {"ok": True},
    )
    state.record_test_verdict(
        assignment_id="aaa", test_state="passed", smoke_test="pass",
    )
    assert captured["path"] == "/test-verdict"
    assert captured["payload"]["test_state"] == "passed"


def test_record_dispatched_assignment_unset_writes_local(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state
    from coord.models import Assignment

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    state.record_dispatched_assignment(
        assignment=Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="loc1", type="review",
        ),
        repo_github="o/api",
    )
    assert coord_db.execute(
        "SELECT status FROM assignments WHERE assignment_id='loc1'"
    ).fetchone()["status"] == "running"


# ── Write path (#601): issue-cache (labels + sync) ────────────────────────────


def test_serve_issue_labels_updates_cache(file_db: Path, valid_config_path: Path, rw_db):
    import json as _j
    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("api", 586, "x", "", "open", '["coord", "status:ready"]', 1.0),
    )
    rw_db.commit()
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-labels",
            json={"repo_name": "api", "issue_number": 586, "labels": ["coord"]},
        )
    assert resp.status_code == 200 and resp.json()["updated"] is True
    row = rw_db.execute(
        "SELECT labels FROM issues WHERE repo_name='api' AND number=586"
    ).fetchone()
    assert _j.loads(row["labels"]) == ["coord"]  # status:ready stripped on the daemon


def test_serve_issues_sync_upserts(file_db: Path, valid_config_path: Path, rw_db):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issues-sync",
            json={
                "repo_name": "api",
                "issues": [
                    {"number": 7, "title": "issue seven", "body": "b",
                     "labels": [{"name": "coord"}]},
                ],
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT title, state FROM issues WHERE repo_name='api' AND number=7"
    ).fetchone()
    assert row["title"] == "issue seven" and row["state"] == "open"


def test_update_issue_labels_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, state, labels, synced_at) "
        "VALUES ('api', 9, 'x', 'open', '[\"coord\", \"status:ready\"]', 1.0)"
    )
    coord_db.commit()
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"updated": True},
    )
    assert state.update_issue_labels("api", 9, ["coord"]) is True
    assert captured["path"] == "/issue-labels"
    assert captured["payload"]["issue_number"] == 9
    # Routed → the local issues row is NOT touched (still has status:ready).
    import json as _j
    row = coord_db.execute("SELECT labels FROM issues WHERE number=9").fetchone()
    assert "status:ready" in _j.loads(row["labels"])


def test_upsert_open_issues_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"ok": True},
    )
    state.upsert_open_issues("api", [{"number": 1, "title": "t", "labels": []}])
    assert captured["path"] == "/issues-sync"
    assert captured["payload"]["repo_name"] == "api"
    # Routed → no local issues row created.
    assert coord_db.execute("SELECT COUNT(*) c FROM issues").fetchone()["c"] == 0


# ── #603: per-issue context store ───────────────────────────────────────────

def test_serve_issue_context_add_get_pin_clear(file_db: Path, valid_config_path: Path, rw_db):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        a = cli.post("/issue-context", json={
            "action": "add", "repo_name": "api", "issue_number": 7,
            "body": "depends on lib #99", "pinned": True, "source": "operator",
        })
        assert a.status_code == 200
        eid = a.json()["entry_id"]
        assert isinstance(eid, int)
        cli.post("/issue-context", json={
            "action": "add", "repo_name": "api", "issue_number": 7,
            "body": "a later note", "source": "test",
        })
        # GET returns both entries, oldest-first.
        g = cli.get("/issue-context", params={"repo_name": "api", "issue_number": 7})
        assert g.status_code == 200
        entries = g.json()["entries"]
        assert [e["body"] for e in entries] == ["depends on lib #99", "a later note"]
        assert entries[0]["pinned"] is True
        # unpin, then clear.
        p = cli.post("/issue-context", json={
            "action": "pin", "repo_name": "api", "issue_number": 7,
            "entry_id": eid, "pinned": False,
        })
        assert p.json()["updated"] is True
        c = cli.post("/issue-context", json={
            "action": "clear", "repo_name": "api", "issue_number": 7,
        })
        assert c.json()["deleted"] == 2
    assert rw_db.execute(
        "SELECT COUNT(*) c FROM issue_context WHERE repo_name='api' AND issue_number=7"
    ).fetchone()["c"] == 0


def test_serve_issue_context_unknown_action_400(file_db: Path, valid_config_path: Path, rw_db):
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/issue-context", json={
            "action": "bogus", "repo_name": "api", "issue_number": 7,
        })
    assert resp.status_code == 400


def test_add_issue_context_entry_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"entry_id": 42},
    )
    assert state.add_issue_context_entry("api", 7, "x", pinned=True) == 42
    assert captured["path"] == "/issue-context"
    assert captured["payload"]["action"] == "add" and captured["payload"]["pinned"] is True
    # Routed → no local row created.
    assert coord_db.execute("SELECT COUNT(*) c FROM issue_context").fetchone()["c"] == 0


def test_list_issue_context_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    monkeypatch.setattr(
        cc, "fetch_issue_context",
        lambda svc, repo, num: [{"id": 1, "pinned": True, "source": None,
                                 "body": "remote note", "created_at": 1.0}],
    )
    assert state.list_issue_context("api", 7)[0]["body"] == "remote note"


def test_add_issue_context_entry_blank_is_noop(coord_db):
    from coord import state
    assert state.add_issue_context_entry("api", 7, "   ") is None
    assert coord_db.execute("SELECT COUNT(*) c FROM issue_context").fetchone()["c"] == 0


def test_render_issue_context_entries_pins_first_then_newest_then_budget():
    from coord import state
    entries = [
        {"id": 1, "pinned": True, "source": "operator", "body": "PIN dep #99", "created_at": 1.0},
        {"id": 2, "pinned": False, "source": "test", "body": "old note", "created_at": 2.0},
        {"id": 3, "pinned": False, "source": "work", "body": "new note", "created_at": 3.0},
    ]
    out = state.render_issue_context_entries(entries)
    lines = out.splitlines()
    assert lines[0].startswith("- 📌 PIN dep #99")  # pinned first
    assert "new note" in lines[1] and "old note" in lines[2]  # newest-first notes
    # Budget: 1 pin + 1 note slot → oldest non-pinned trimmed with a marker.
    capped = state.render_issue_context_entries(entries, max_entries=2)
    assert "PIN dep #99" in capped and "new note" in capped
    assert "old note" not in capped and "trimmed" in capped
    assert state.render_issue_context_entries([]) == ""


def test_issue_context_dropped_on_close(coord_db):
    from coord import state
    state._add_issue_context_entry_local("api", 7, "ctx for closing issue", pinned=True)
    state._add_issue_context_entry_local("api", 8, "ctx for issue staying open")
    coord_db.execute(
        "INSERT INTO issues(repo_name,number,state,synced_at) VALUES('api',8,'open',0)"
    )
    coord_db.commit()
    # #7 absent from the open set → closed → its context dropped; #8 kept.
    state._upsert_open_issues_local("api", [{"number": 8, "title": "t", "body": "", "labels": []}])
    assert state._list_issue_context_local("api", 7) == []
    assert len(state._list_issue_context_local("api", 8)) == 1


def test_record_test_verdict_local_appends_context(coord_db):
    # #603: a test FAILURE auto-appends a durable context entry (source=test).
    from coord import state
    coord_db.execute(
        "INSERT INTO assignments(assignment_id,machine_name,repo_name,issue_number,"
        "issue_title,status,type) VALUES('w1','m','api',7,'t','done','work')"
    )
    coord_db.commit()
    state._record_test_verdict_local(assignment_id="w1", test_state="failed", test_reason="boom")
    ents = state._list_issue_context_local("api", 7)
    assert len(ents) == 1 and ents[0]["source"] == "test"
    assert "Test FAILED: boom" in ents[0]["body"]
    # A pass adds nothing.
    state._record_test_verdict_local(assignment_id="w1", test_state="passed")
    assert len(state._list_issue_context_local("api", 7)) == 1


def test_post_result_request_changes_appends_context(coord_db, monkeypatch):
    # #603: a request-changes verdict auto-appends a context entry (source=review).
    from coord import issue_store, state
    monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **k: None)
    coord_db.execute(
        "INSERT INTO assignments(assignment_id,machine_name,repo_name,issue_number,"
        "issue_title,status,type) VALUES('rev1','m','api',7,'t','running','review')"
    )
    coord_db.commit()
    issue_store._post_result_local(issue_store.ResultRecord(
        assignment_id="rev1", machine_name="m", repo_name="api", repo_github="o/api",
        issue_number=7, status="done", verdict="request-changes",
        findings_body="must set is_keyboard_focused", summary="",
    ))
    ents = state._list_issue_context_local("api", 7)
    assert any(e["source"] == "review" and "is_keyboard_focused" in e["body"] for e in ents)


def test_cli_context_add_show_clear(coord_db):
    # #603: the operator-facing `coord context` round-trip.
    from click.testing import CliRunner
    from coord.cli import main
    r = CliRunner()
    out = r.invoke(main, ["context", "add", "api", "7", "depends on lib #9", "--pin"])
    assert out.exit_code == 0 and "added" in out.output
    out = r.invoke(main, ["context", "show", "api", "7"])
    assert out.exit_code == 0 and "depends on lib #9" in out.output and "📌" in out.output
    out = r.invoke(main, ["context", "clear", "api", "7"])
    assert out.exit_code == 0 and "cleared 1" in out.output


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
