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


def test_serve_merge_passes_show_plan_to_callback(file_db: Path, valid_config_path: Path):
    """#684 regression: ``post_merge`` must pass ``show_plan`` to the merge
    callback.  #684 added ``--plan``/``show_plan`` to ``coord merge`` (routing
    ``--plan`` via /board, never /merge) but left the daemon handler invoking
    ``merge_cmd.callback(...)`` without it — so every daemon-routed merge (thin
    client, TUI 'Go', headless drain) crashed with ``merge() missing 1 required
    positional argument: 'show_plan'`` before doing anything.

    A nonexistent ``repo_filter`` keeps the dry-run a hermetic no-op (empty
    queue → no gh/network), so the test asserts only that the signature bug
    does not recur.
    """
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        resp = cli.post("/merge", json={"dry_run": True, "repo_filter": "no-such-repo"})
        assert resp.status_code == 200
        err = resp.json().get("error") or ""
        assert "show_plan" not in err, f"merge handler regressed on show_plan: {err}"
        assert "missing 1 required positional argument" not in err


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
    # #646 invariant: a verdict may only be recorded on a review row.
    _seed_running_assignment(rw_db, atype="review")
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


def test_serve_post_board_upserts_full_board(
    file_db: Path, valid_config_path: Path, rw_db
):
    """#749: POST /board — the generic whole-board upsert endpoint backing
    coord.board_service.write_board() for the client paths that still
    read-modify-write the full board (assign/approve/stop/retry/…)."""
    from coord.client import serialize_board
    from coord.models import Assignment, Board

    board = Board(
        round_number=4,
        completed=[
            Assignment(
                machine_name="precision", repo_name="api", issue_number=21,
                issue_title="thin-client board write", assignment_id="wb1",
                status="done", branch="issue-21-x",
            ),
        ],
    )
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/board", json=serialize_board(board))
    assert resp.status_code == 200 and resp.json()["ok"] is True

    row = rw_db.execute(
        "SELECT status, branch FROM assignments WHERE assignment_id='wb1'"
    ).fetchone()
    assert row["status"] == "done" and row["branch"] == "issue-21-x"
    meta = rw_db.execute(
        "SELECT value FROM board_meta WHERE key='round_number'"
    ).fetchone()
    assert meta["value"] == "4"


def test_post_board_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    """coord.client.post_board POSTs the serialized board to /board."""
    from coord import client as cc
    from coord.models import Assignment, Board

    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"ok": True},
    )
    board = Board(
        round_number=6,
        completed=[
            Assignment(
                machine_name="m", repo_name="api", issue_number=1,
                issue_title="t", assignment_id="wb2", status="done",
            ),
        ],
    )
    cc.post_board(cc.ServiceConfig("http://d:7435"), board)
    assert captured["path"] == "/board"
    assert captured["payload"]["round_number"] == 6
    assert captured["payload"]["assignments"][0]["assignment_id"] == "wb2"
    # Routed → nothing written to the local DB.
    assert coord_db.execute(
        "SELECT COUNT(*) c FROM assignments WHERE assignment_id='wb2'"
    ).fetchone()["c"] == 0


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


# ── Write path (#665): assignment-usage (cost / tokens / is_interactive) ──────


def test_update_assignment_cost_routes_when_service_set(coord_db, monkeypatch):
    """update_assignment_cost() POSTs to /assignment-usage when board_service is set."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="cu01")
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload) or {"ok": True},
    )
    state.update_assignment_cost("cu01", 0.42)
    assert captured["path"] == "/assignment-usage"
    assert captured["payload"]["assignment_id"] == "cu01"
    assert captured["payload"]["cost_usd"] == 0.42
    # Routed → the local DB row was NOT touched.
    row = coord_db.execute(
        "SELECT cost_usd FROM assignments WHERE assignment_id='cu01'"
    ).fetchone()
    assert row["cost_usd"] is None


def test_update_assignment_cost_unset_writes_local(coord_db, monkeypatch):
    """update_assignment_cost() writes the local DB when board_service is unset."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="cu02")
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    state.update_assignment_cost("cu02", 1.23)
    row = coord_db.execute(
        "SELECT cost_usd FROM assignments WHERE assignment_id='cu02'"
    ).fetchone()
    assert row["cost_usd"] == 1.23


def test_update_assignment_tokens_routes_when_service_set(coord_db, monkeypatch):
    """update_assignment_tokens() POSTs to /assignment-usage when board_service is set."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="tu01")
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload) or {"ok": True},
    )
    state.update_assignment_tokens("tu01", input_tokens=100, output_tokens=50)
    assert captured["path"] == "/assignment-usage"
    assert captured["payload"]["assignment_id"] == "tu01"
    assert captured["payload"]["input_tokens"] == 100
    assert captured["payload"]["output_tokens"] == 50
    # Routed → local row untouched.
    row = coord_db.execute(
        "SELECT input_tokens FROM assignments WHERE assignment_id='tu01'"
    ).fetchone()
    assert row["input_tokens"] is None or row["input_tokens"] == 0


def test_update_assignment_tokens_zero_total_skips_route(coord_db, monkeypatch):
    """update_assignment_tokens() with all-zero counts is a no-op (no POST, no local write)."""
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    called = []
    monkeypatch.setattr(cc, "post_record", lambda *a, **k: called.append(a) or {"ok": True})
    state.update_assignment_tokens("tu-noop")  # all defaults are 0
    assert called == []


def test_update_assignment_tokens_unset_writes_local(coord_db, monkeypatch):
    """update_assignment_tokens() writes the local DB when board_service is unset."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="tu02")
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    state.update_assignment_tokens("tu02", input_tokens=200, output_tokens=75,
                                   cache_creation_tokens=10, cache_read_tokens=5)
    row = coord_db.execute(
        "SELECT input_tokens, output_tokens FROM assignments WHERE assignment_id='tu02'"
    ).fetchone()
    assert row["input_tokens"] == 200 and row["output_tokens"] == 75


def test_mark_assignment_interactive_routes_when_service_set(coord_db, monkeypatch):
    """mark_assignment_interactive() POSTs to /assignment-usage when board_service is set."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="ia01")
    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload) or {"ok": True},
    )
    state.mark_assignment_interactive("ia01")
    assert captured["path"] == "/assignment-usage"
    assert captured["payload"]["assignment_id"] == "ia01"
    assert captured["payload"]["is_interactive"] is True
    # Routed → local row untouched.
    row = coord_db.execute(
        "SELECT is_interactive FROM assignments WHERE assignment_id='ia01'"
    ).fetchone()
    assert not row["is_interactive"]


def test_mark_assignment_interactive_unset_writes_local(coord_db, monkeypatch):
    """mark_assignment_interactive() writes the local DB when board_service is unset."""
    from coord import client as cc
    from coord import state

    _seed_running_assignment(coord_db, aid="ia02")
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    state.mark_assignment_interactive("ia02")
    row = coord_db.execute(
        "SELECT is_interactive FROM assignments WHERE assignment_id='ia02'"
    ).fetchone()
    assert row["is_interactive"] == 1


def test_serve_assignment_usage_records_cost(file_db, valid_config_path, rw_db):
    """POST /assignment-usage with cost_usd writes cost to the daemon DB."""
    _seed_running_assignment(rw_db, aid="du01")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={"assignment_id": "du01", "cost_usd": 0.55},
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT cost_usd FROM assignments WHERE assignment_id='du01'"
    ).fetchone()
    assert row["cost_usd"] == 0.55


def test_serve_assignment_usage_records_tokens(file_db, valid_config_path, rw_db):
    """POST /assignment-usage with token fields writes tokens to the daemon DB."""
    _seed_running_assignment(rw_db, aid="du02")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={
                "assignment_id": "du02",
                "input_tokens": 300,
                "output_tokens": 120,
                "cache_creation_tokens": 20,
                "cache_read_tokens": 10,
            },
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT input_tokens, output_tokens FROM assignments WHERE assignment_id='du02'"
    ).fetchone()
    assert row["input_tokens"] == 300 and row["output_tokens"] == 120


def test_serve_assignment_usage_records_interactive(file_db, valid_config_path, rw_db):
    """POST /assignment-usage with is_interactive sets the flag on the daemon DB."""
    _seed_running_assignment(rw_db, aid="du03")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={"assignment_id": "du03", "is_interactive": True},
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    row = rw_db.execute(
        "SELECT is_interactive FROM assignments WHERE assignment_id='du03'"
    ).fetchone()
    assert row["is_interactive"] == 1


def test_serve_assignment_usage_combined(file_db, valid_config_path, rw_db):
    """POST /assignment-usage can set cost + tokens + interactive in one request."""
    _seed_running_assignment(rw_db, aid="du04")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={
                "assignment_id": "du04",
                "cost_usd": 0.10,
                "input_tokens": 50,
                "output_tokens": 25,
                "cache_creation_tokens": 5,
                "cache_read_tokens": 2,
                "is_interactive": True,
            },
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT cost_usd, input_tokens, is_interactive FROM assignments "
        "WHERE assignment_id='du04'"
    ).fetchone()
    assert row["cost_usd"] == 0.10
    assert row["input_tokens"] == 50
    assert row["is_interactive"] == 1


def test_serve_assignment_usage_records_smoke_tests(file_db, valid_config_path, rw_db):
    """#749: POST /assignment-usage also routes the SMOKE_TESTS block —
    coord.state.update_assignment_smoke_tests was previously unrouted, so a
    thin client's `coord notify`/`coord approve-plan` never recorded it."""
    _seed_running_assignment(rw_db, aid="du05")
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/assignment-usage",
            json={"assignment_id": "du05", "smoke_tests": ["click the button"]},
        )
    assert resp.status_code == 200
    row = rw_db.execute(
        "SELECT smoke_tests FROM assignments WHERE assignment_id='du05'"
    ).fetchone()
    assert row["smoke_tests"] == '["click the button"]'


def test_update_assignment_smoke_tests_routes_when_service_set(coord_db, monkeypatch):
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
    state.update_assignment_smoke_tests("aid1", ["run the tests"])
    assert captured["path"] == "/assignment-usage"
    assert captured["payload"]["smoke_tests"] == ["run the tests"]


def test_serve_assignment_usage_missing_id(file_db, valid_config_path):
    """POST /assignment-usage without assignment_id returns 400."""
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/assignment-usage", json={"cost_usd": 1.0})
    assert resp.status_code == 400


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


def test_serve_issue_edit_writes_backend_and_cache(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # The tracker (gh) write runs on the DAEMON behind the seam — stub it so the
    # test never shells out, and assert it got the github slug + new content.
    calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.edit_issue",
        lambda repo, num, *, title=None, body=None: calls.append((repo, num, title, body)),
    )
    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("api", 7, "old title", "old body", "open", "[]", 1.0),
    )
    rw_db.commit()
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/issue-edit",
            json={
                "repo_name": "api",
                "issue_number": 7,
                "title": "new title",
                "body": "new body",
                "repo_github": "owner/api",
            },
        )
    assert resp.status_code == 200 and resp.json()["updated"] is True
    assert calls == [("owner/api", 7, "new title", "new body")]
    # Cache mirrors the edit so the TUI reflects it on the next refresh.
    row = rw_db.execute(
        "SELECT title, body FROM issues WHERE repo_name='api' AND number=7"
    ).fetchone()
    assert row["title"] == "new title" and row["body"] == "new body"


def test_edit_issue_content_routes_when_service_set(coord_db, monkeypatch):
    from coord import client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"updated": True},
    )
    # When routing to the daemon, the backend write must NOT run client-side.
    def _boom(*a, **k):
        raise AssertionError("backend write must run on the daemon, not the client")

    monkeypatch.setattr("coord.github_ops.edit_issue", _boom)
    assert (
        state.edit_issue_content("api", 9, title="t", repo_github="owner/api") is True
    )
    assert captured["path"] == "/issue-edit"
    assert captured["payload"]["issue_number"] == 9
    assert captured["payload"]["repo_github"] == "owner/api"


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


def test_cli_context_curate_replaces_entries(coord_db, monkeypatch):
    # #603 Phase 4: curate compresses via claude -p and replaces the entries.
    from coord import state
    for i in range(5):
        state._add_issue_context_entry_local("api", 7, f"note {i}", pinned=(i == 0))
    fake = '```json\n[{"body":"merged critical dep","pinned":true},' \
           '{"body":"one lesson kept","pinned":false}]\n```'
    monkeypatch.setattr("coord.test_orchestrator._call_claude", lambda *a, **k: fake)
    from click.testing import CliRunner
    from coord.cli import main
    out = CliRunner().invoke(main, ["context", "curate", "api", "7"])
    assert out.exit_code == 0 and "5 → 2" in out.output
    ents = state._list_issue_context_local("api", 7)
    assert len(ents) == 2
    assert ents[0]["body"] == "merged critical dep" and ents[0]["pinned"] is True
    assert all(e["source"] == "curated" for e in ents)


def test_cli_context_curate_noop_when_few(coord_db, monkeypatch):
    from coord import state
    state._add_issue_context_entry_local("api", 7, "only note")
    called = []
    monkeypatch.setattr("coord.test_orchestrator._call_claude",
                        lambda *a, **k: called.append(1) or "[]")
    from click.testing import CliRunner
    from coord.cli import main
    out = CliRunner().invoke(main, ["context", "curate", "api", "7"])
    assert out.exit_code == 0 and "nothing to curate" in out.output
    assert called == []  # no metered call for a tiny digest


def test_cli_fix_briefing_includes_context_and_test_story(coord_db, valid_config_path):
    # #603 Phase 5: `coord fix-briefing` prints the context block + the resolved
    # test-failure story (the exact-briefing preview the TUI dialog shows).
    # Pass --config explicitly: coordinator.yml is NOT checked in (gitignored
    # dev config), so the default relative path only resolves when cwd happens
    # to hold a local one — it does on a dev box, but not in CI's fresh
    # checkout, which left this test red on every push since v0.4.40.
    from coord import state
    coord_db.execute(
        "INSERT INTO assignments(assignment_id,machine_name,repo_name,issue_number,"
        "issue_title,status,type,branch,test_state,test_reason) VALUES"
        "('w1','laptop','claude-coordinator',7,'Fix X','done','work','issue-7-x',"
        "'failed','Button does nothing on click')"
    )
    coord_db.commit()
    state._add_issue_context_entry_local(
        "claude-coordinator", 7, "depends on quadraui #368", pinned=True
    )
    from click.testing import CliRunner
    from coord.cli import main
    out = CliRunner().invoke(
        main, ["fix-briefing", "w1", "--config", str(valid_config_path)]
    )
    assert out.exit_code == 0, out.output
    assert "⚠️ Issue context" in out.output  # context block at the top
    assert "depends on quadraui #368" in out.output
    assert "Button does nothing on click" in out.output  # the resolved test story


def test_serve_merge_runs_callback_and_captures_output(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # #584: POST /merge runs `coord merge` on the daemon with the recursion
    # guard set, and relays the captured CLI output + exit code.
    import os
    import click
    from coord.cli import merge as merge_cmd

    def fake_callback(**kwargs):
        assert os.environ.get("COORD_MERGE_ON_DAEMON") == "1"  # guard set
        click.echo(f"merged dry_run={kwargs['dry_run']} method={kwargs['method']}")

    monkeypatch.setattr(merge_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/merge", json={"dry_run": True, "method": "squash"})
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0 and out["error"] is None
    assert "merged dry_run=True method=squash" in out["output"]
    assert os.environ.get("COORD_MERGE_ON_DAEMON") is None  # restored after


def test_serve_merge_relays_nonzero_exit(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    import sys
    from coord.cli import merge as merge_cmd
    monkeypatch.setattr(merge_cmd, "callback", lambda **k: sys.exit(2))
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/merge", json={})
    assert resp.json()["exit_code"] == 2


def test_serve_merge_ignores_client_skip_review(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    """#821: POST /merge with skip_review=True must NOT propagate to the merge callback.

    The daemon always enforces the review gate regardless of any flag the thin
    client sends.  Verify the callback is invoked with skip_review=False even
    when the POST body contains skip_review=True.
    """
    from coord.cli import merge as merge_cmd

    captured: dict = {}

    def fake_callback(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(merge_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/merge", json={"skip_review": True, "dry_run": True})
    assert resp.status_code == 200
    # Daemon must have stripped the client's skip_review flag.
    assert captured.get("skip_review") is False, (
        f"daemon must pass skip_review=False to callback, got {captured.get('skip_review')!r}"
    )


def test_merge_command_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    # #584: `coord merge` on a thin client POSTs to /merge and relays the output,
    # instead of no-opping against the empty local board.
    from coord import client as cc
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"output": "DAEMON MERGE OUTPUT\n", "exit_code": 0},
    )
    out = CliRunner().invoke(main, ["merge", "--dry-run", "--repo", "api"])
    assert out.exit_code == 0, out.output
    assert captured["path"] == "/merge"
    assert captured["payload"]["dry_run"] is True
    assert captured["payload"]["repo_filter"] == "api"
    assert "DAEMON MERGE OUTPUT" in out.output


def test_serve_reconcile_merges_runs_callback_and_captures_output(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # #584: POST /reconcile-merges runs `coord reconcile-merges` on the daemon
    # with the recursion guard set, and relays the captured CLI output + code.
    import os
    import click
    from coord.cli import reconcile_merges as reconcile_cmd

    def fake_callback(**kwargs):
        assert os.environ.get("COORD_RECONCILE_ON_DAEMON") == "1"  # guard set
        click.echo(
            f"reconciled dry_run={kwargs['dry_run']} repo={kwargs['repo_name']}"
        )

    monkeypatch.setattr(reconcile_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/reconcile-merges", json={"dry_run": True, "repo": "api"})
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0 and out["error"] is None
    assert "reconciled dry_run=True repo=api" in out["output"]
    assert os.environ.get("COORD_RECONCILE_ON_DAEMON") is None  # restored after


def test_serve_reconcile_merges_relays_nonzero_exit(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    import sys
    from coord.cli import reconcile_merges as reconcile_cmd
    monkeypatch.setattr(reconcile_cmd, "callback", lambda **k: sys.exit(2))
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/reconcile-merges", json={})
    assert resp.json()["exit_code"] == 2


def test_reconcile_merges_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    # #584: `coord reconcile-merges` on a thin client POSTs to /reconcile-merges
    # and relays the output, instead of no-opping against the empty local board.
    from coord import client as cc
    from coord import cli as coord_cli
    from coord import state as coord_state
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    # routing happens before any local-board work — assert build_board never runs
    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("build_board must not be called on a thin client")

    monkeypatch.setattr(coord_state, "build_board", _boom, raising=False)
    monkeypatch.setattr(coord_state, "save_board", _boom, raising=False)
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"output": "DAEMON RECONCILE OUTPUT\n", "exit_code": 0},
    )
    out = CliRunner().invoke(main, ["reconcile-merges", "--dry-run", "--repo", "api"])
    assert out.exit_code == 0, out.output
    assert captured["path"] == "/reconcile-merges"
    assert captured["payload"]["dry_run"] is True
    assert captured["payload"]["repo"] == "api"
    assert "DAEMON RECONCILE OUTPUT" in out.output


def test_serve_diagnose_runs_callback_and_captures_output(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # POST /diagnose runs `coord diagnose` on the daemon with the recursion
    # guard set, and relays the captured CLI output + exit code.
    import os
    import click
    from coord.cli import diagnose as diagnose_cmd

    def fake_callback(**kwargs):
        assert os.environ.get("COORD_DIAGNOSE_ON_DAEMON") == "1"  # guard set
        click.echo(
            f"diagnosed repo={kwargs['repo']} issue={kwargs['issue']} "
            f"stage={kwargs['stage']} reset={kwargs['reset']}"
        )

    monkeypatch.setattr(diagnose_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/diagnose",
            json={"repo": "api", "issue": 42, "stage": "review", "reset": False},
        )
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0 and out["error"] is None
    assert "diagnosed repo=api issue=42 stage=review reset=False" in out["output"]
    assert os.environ.get("COORD_DIAGNOSE_ON_DAEMON") is None  # restored after


def test_serve_diagnose_real_callback_no_orphan_worktrees_crash(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # Regression: POST /diagnose used to raise
    #   TypeError: diagnose() missing 1 required positional argument: 'orphan_worktrees'
    # because serve_app.post_diagnose called diagnose_cmd.callback(...) without
    # passing the orphan_worktrees kwarg.  This test drives the REAL callback
    # (no monkeypatching of .callback) and should FAIL without the serve_app fix.
    from coord import client as cc
    from coord import state as coord_state
    from coord.diagnose import DiagnoseResult
    from coord.models import Board

    # Route to local path (COORD_DIAGNOSE_ON_DAEMON guard takes over inside the
    # endpoint, but resolve_board_service must return None so the callback doesn't
    # try to route again before the guard is set).
    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
    monkeypatch.setattr(coord_state, "build_board", lambda: Board())
    monkeypatch.setattr(
        "coord.diagnose.diagnose_stage",
        lambda *a, **k: DiagnoseResult(
            repo_name="api", issue_number=42, stage="work", recovered=False
        ),
    )

    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/diagnose", json={"repo": "api", "issue": 42})

    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0, f"expected exit_code=0, got: {out}"
    assert out["error"] is None, f"expected no error, got: {out['error']}"
    assert "missing" not in (out["error"] or "")
    assert "positional argument" not in (out["error"] or "")


def test_serve_diagnose_relays_nonzero_exit(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    import sys
    from coord.cli import diagnose as diagnose_cmd
    monkeypatch.setattr(diagnose_cmd, "callback", lambda **k: sys.exit(2))
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post("/diagnose", json={"repo": "api", "issue": 1})
    assert resp.json()["exit_code"] == 2


def test_diagnose_routes_to_daemon_when_service_set(coord_db, monkeypatch):
    # `coord diagnose` on a thin client POSTs to /diagnose and relays the
    # output, instead of no-opping against the empty local board.
    from coord import client as cc
    from coord import state as coord_state
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("build_board must not be called on a thin client")

    monkeypatch.setattr(coord_state, "build_board", _boom, raising=False)
    monkeypatch.setattr(coord_state, "save_board", _boom, raising=False)
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"output": "DAEMON DIAGNOSE OUTPUT\n", "exit_code": 0},
    )
    out = CliRunner().invoke(
        main, ["diagnose", "api", "42", "--stage", "review", "--reset"]
    )
    assert out.exit_code == 0, out.output
    assert captured["path"] == "/diagnose"
    assert captured["payload"]["repo"] == "api"
    assert captured["payload"]["issue"] == 42
    assert captured["payload"]["stage"] == "review"
    assert captured["payload"]["reset"] is True
    assert "DAEMON DIAGNOSE OUTPUT" in out.output


def test_serve_test_plan_runs_callback_and_captures_output(
    file_db: Path, valid_config_path: Path, rw_db, monkeypatch
):
    # #851: POST /test-plan runs `coord test-plan` on the daemon with the
    # recursion guard set, and relays the captured CLI output + exit code.
    # Mirrors test_serve_diagnose_runs_callback_and_captures_output.
    import os
    import click
    from coord.cli import test_plan_cmd

    def fake_callback(**kwargs):
        assert os.environ.get("COORD_TEST_PLAN_ON_DAEMON") == "1"  # guard set
        click.echo(
            f"test-planned assignment_id={kwargs['assignment_id']} "
            f"refresh={kwargs['refresh']} model={kwargs['model']}"
        )

    monkeypatch.setattr(test_plan_cmd, "callback", fake_callback)
    app = build_app(SqliteStore(file_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/test-plan",
            json={"assignment_id": "abc123", "refresh": True, "model": "haiku"},
        )
    assert resp.status_code == 200
    out = resp.json()
    assert out["exit_code"] == 0 and out["error"] is None
    assert (
        "test-planned assignment_id=abc123 refresh=True model=haiku" in out["output"]
    )
    assert os.environ.get("COORD_TEST_PLAN_ON_DAEMON") is None  # restored after


def test_test_plan_routes_to_daemon_when_service_set(coord_db, tmp_path, monkeypatch):
    # #851: `coord test-plan` on a thin client POSTs to /test-plan and relays
    # the output, instead of reporting "not found" against its empty local
    # DB (generate_plan queries the local DB directly and has no daemon-
    # routing of its own). Mirrors test_diagnose_routes_to_daemon_when_service_set.
    from coord import client as cc
    from coord import test_orchestrator
    from click.testing import CliRunner
    from coord.cli import main

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("generate_plan must not run locally on a thin client")

    monkeypatch.setattr(test_orchestrator, "generate_plan", _boom, raising=False)
    captured: dict = {}
    monkeypatch.setattr(
        cc, "post_record",
        lambda svc, path, payload, **kw: captured.update(path=path, payload=payload)
        or {"output": "DAEMON TEST-PLAN OUTPUT\n", "exit_code": 0},
    )
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos:\n  - name: api\n    github: acme/api\nmachines: []\n")
    out = CliRunner().invoke(
        main,
        ["test-plan", "abc123", "--refresh", "--model", "sonnet", "--config", str(cfg)],
    )
    assert out.exit_code == 0, out.output
    assert captured["path"] == "/test-plan"
    assert captured["payload"] == {
        "assignment_id": "abc123", "refresh": True, "model": "sonnet",
    }
    assert "DAEMON TEST-PLAN OUTPUT" in out.output


def test_log_falls_back_to_daemon_board_machine_name(coord_db, tmp_path, monkeypatch):
    # #851: `coord log` on a thin client (or any machine that isn't the
    # dispatcher) has no local dispatched-ledger record for a valid remote
    # assignment id and no local log file. Before this fix that fell through
    # to "no log found" and made a healthy id look broken; now it asks the
    # daemon board for the assignment's own machine_name so the operator
    # doesn't have to guess --machine.
    from unittest.mock import patch

    from coord import agent as agent_mod
    from coord import client as cc
    from click.testing import CliRunner
    from coord.cli import main

    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tailnet\n"
        "    repos: [api]\n"
        "  - name: server\n"
        "    host: server.tailnet\n"
        "    repos: [api]\n"
    )

    # No local log for this assignment on this machine.
    monkeypatch.setattr(agent_mod, "DEFAULT_STATE_DIR", tmp_path / "state")

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    monkeypatch.setattr(
        cc,
        "fetch_board_payload",
        lambda svc, **kw: {
            "assignments": [
                {
                    "assignment_id": "remote-only",
                    "machine_name": "server",
                    "repo_name": "api",
                    "status": "done",
                },
            ]
        },
    )

    with patch(
        "coord.network.fetch_log",
        return_value=(200, b"remote log content via daemon board\n"),
    ):
        result = CliRunner().invoke(
            main, ["log", "remote-only", "--config", str(cfg)]
        )

    assert result.exit_code == 0, result.output
    assert "remote log content via daemon board" in result.output


def test_diagnose_cli_never_calls_save_board(valid_config_path: Path, coord_db, monkeypatch):
    # Regression (quadraui #366): the diagnose command must persist ONLY through
    # the issue_store seam (finalize→post_completion, recover→post_result,
    # reconcile→state.update_*).  A save_board would write the STALE in-memory
    # snapshot and clobber those seam writes — flipping a just-finalized phantom
    # back to 'running'.  So save_board must NEVER be called by diagnose.
    from coord import client as cc
    from coord import state as state_mod
    from coord.cli import diagnose as diagnose_cmd
    from coord.diagnose import DiagnoseResult
    from coord.models import Board

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)  # local path
    monkeypatch.setattr(state_mod, "build_board", lambda: Board())
    monkeypatch.setattr(
        "coord.diagnose.diagnose_stage",
        lambda *a, **k: DiagnoseResult(
            repo_name="api", issue_number=42, stage="work", recovered=True
        ),
    )

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("diagnose must not save_board (it clobbers seam writes)")

    monkeypatch.setattr(state_mod, "save_board", _boom, raising=False)
    # Should complete without ever touching save_board.
    diagnose_cmd.callback(
        repo="api", issue=42, stage="work", reset=False, dry_run=False,
        config_path=valid_config_path,
        orphan_worktrees=False,  # #618: new flag; default False for this test
    )


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


# ── Passive tick (#736 / #217): daemon enqueues approved work on every interval ──


def _seed_approved_done_work(conn, *, aid: str = "work99", branch: str = "issue-7-impl") -> None:
    """Seed an approved + test-passed done work assignment into the shared DB.

    Inserts:
    - A done work assignment on *branch* with ``test_state='passed'``.
    - A done review assignment pointing at it with ``review_verdict='approve'``.

    After these rows are present, ``build_board()`` will include them in
    ``board.completed`` and ``enqueue_approved_work`` should enqueue the work.
    The DB must already have ``board_initialized`` set (coord_db autouse fixture
    sets this via ``_ensure_schema``; for ``rw_db`` we set it explicitly).
    """
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch, test_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "acme/api", 7, "The issue", "done", "work", branch, "passed"),
    )
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, review_of_assignment_id, review_verdict) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            f"rev-{aid}", "server", "api", "acme/api", 7, "Review of issue",
            "done", "review", aid, "approve",
        ),
    )
    conn.commit()


def test_passive_tick_enqueues_approved_work(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#736: _passive_tick() enqueues an approved+tested work assignment into the
    merge queue without a manual ``coord merge`` call.

    This is the key regression guard for the #217 invisible limbo: the daemon
    tick now reliably enqueues approved work on every interval, independent of
    ``pipeline.auto_loop`` or ``coord notify``.
    """
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _passive_tick

    # reconcile_completed_assignments polls the agent HTTP API; stub it to avoid
    # network calls and focus the test on the enqueue path.
    monkeypatch.setattr(
        "coord.reconcile._query_agent",
        lambda host: None,  # agent unreachable → reconcile is a no-op
    )

    _seed_approved_done_work(rw_db)
    cfg = load_config(valid_config_path)

    reconciled, enqueued = _passive_tick(cfg)

    # Reconcile found nothing (we stubbed the agent).
    assert reconciled == []
    # The approved+tested assignment was enqueued by the tick.
    assert enqueued == ["work99"]
    items = mq.load_queue()
    assert len(items) == 1
    assert items[0].assignment_id == "work99"
    assert items[0].branch == "issue-7-impl"
    assert items[0].repo_github == "acme/api"


def test_passive_tick_is_idempotent(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """A second tick with the same approved work produces no further queue changes."""
    from coord.config import load as load_config
    from coord.serve_app import _passive_tick

    monkeypatch.setattr("coord.reconcile._query_agent", lambda host: None)
    _seed_approved_done_work(rw_db)
    cfg = load_config(valid_config_path)

    _passive_tick(cfg)  # first tick — creates the entry
    _, enqueued2 = _passive_tick(cfg)  # second tick — already keyed correctly

    assert enqueued2 == []


# ── #775: _reconcile_merges_tick + _sync_issues_tick ─────────────────────────


def _seed_done_work_with_branch(
    conn,
    *,
    aid: str = "work-m1",
    branch: str = "issue-42-impl",
    issue_number: int = 42,
) -> None:
    """Seed a done work assignment that has a branch (eligible for merge reconcile)."""
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')"
    )
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            aid, "laptop", "api", "acme/api", issue_number,
            "The issue", "done", "work", branch,
        ),
    )
    conn.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            aid, "api", "acme/api", branch, "main",
            issue_number, "The issue", "pending",
        ),
    )
    conn.commit()


def test_reconcile_merges_tick_flips_merged_and_prunes_queue(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#775: _reconcile_merges_tick flips a done assignment to 'merged' and
    prunes its stale merge_queue row when the branch is terminal on GitHub.

    This is the black-box acceptance test for the tick path described in the
    issue acceptance criteria.
    """
    from coord import github_ops, merge_queue as mq
    from coord.config import load as load_config
    from coord.serve_app import _reconcile_merges_tick

    # Stub all GitHub probes so we never shell out.
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: True)
    monkeypatch.setattr(
        github_ops, "list_remote_branch_names", lambda repo: set()
    )
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])
    # prune_stale_queue_entries calls issue_is_closed / pr_is_merged.
    monkeypatch.setattr(github_ops, "issue_is_closed", lambda *a: True)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda *a: False)

    _seed_done_work_with_branch(rw_db)
    cfg = load_config(valid_config_path)

    actions = _reconcile_merges_tick(cfg)

    # The reconcile must have reported the flip.
    assert any("mark merged" in a for a in actions), (
        f"Expected 'mark merged' action; got: {actions}"
    )
    # DB must reflect the flip.
    row = rw_db.execute(
        "SELECT status FROM assignments WHERE assignment_id = 'work-m1'"
    ).fetchone()
    assert row is not None and row["status"] == "merged", (
        f"Assignment status should be 'merged', got: {row['status'] if row else None}"
    )
    # The merge_queue row must have been pruned.
    queue = mq.load_queue()
    assert not any(e.assignment_id == "work-m1" for e in queue), (
        f"merge_queue row should have been pruned; queue: {[e.assignment_id for e in queue]}"
    )


def test_sync_issues_tick_marks_issues_closed(
    valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """#775: _sync_issues_tick propagates issue closures into the DB so the
    board's is_closed flag becomes accurate without a manual 'coord sync'.
    """
    from coord import github_ops
    from coord.config import load as load_config
    from coord.serve_app import _sync_issues_tick

    # Seed an open issue in the DB.
    rw_db.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
    )
    rw_db.execute(
        "INSERT INTO issues (repo_name, number, title, state, body, labels) "
        "VALUES (?,?,?,?,?,?)",
        ("api", 42, "An issue", "open", "", "[]"),
    )
    rw_db.commit()

    # GitHub now returns an empty open-issue list (issue 42 was closed).
    monkeypatch.setattr(
        github_ops, "get_open_issues", lambda repo: []
    )

    cfg = load_config(valid_config_path)
    total = _sync_issues_tick(cfg)

    # The sync reported 0 open issues (all repos returned empty lists).
    assert total == 0

    # The issue row must now be marked 'closed' in the DB.
    row = rw_db.execute(
        "SELECT state FROM issues WHERE repo_name = 'api' AND number = 42"
    ).fetchone()
    assert row is not None and row["state"] == "closed", (
        f"Issue should be 'closed' after sync; got: {row['state'] if row else None}"
    )


# ── #776: merge_plan in /board payload ───────────────────────────────────────


def test_board_payload_has_merge_plan_key(
    file_db: Path, valid_config_path: Path
) -> None:
    """/board always includes a 'merge_plan' key (may be an empty list)."""
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()
    assert "merge_plan" in board
    assert isinstance(board["merge_plan"], list)


def test_board_merge_plan_contains_correct_fields(
    rw_db, valid_config_path: Path, monkeypatch, tmp_path: Path
) -> None:
    """/board merge_plan entries carry the required #776 fields.

    Seeds a PENDING merge-queue entry and verifies the plan contains
    rank, status, reason, target_branch, enqueued_at, size, milestone.
    """
    from coord import github_ops, merge_queue as mq
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    # Stub GitHub so build_board and plan() never shell out.
    monkeypatch.setattr(github_ops, "get_branch_diff_size", lambda *a: 0)

    # Seed a pending merge-queue entry with a known enqueued_at.
    import time as _time
    ts = _time.time() - 30.0
    rw_db.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    rw_db.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    rw_db.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state, size, enqueued_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("w1", "api", "acme/api", "issue-1-impl", "main", 1, "t", "pending", 42, ts),
    )
    rw_db.commit()

    cfg = load_config(valid_config_path)
    # #684/#776 regression: read from the SAME db rw_db seeded (its temp
    # rw.db), not the canonical DB_PATH.  SqliteStore opens mode=ro, which
    # errors ("unable to open database file") when the path is absent — so
    # SqliteStore(DB_PATH) failed in CI (no ~/.coord/coord.db) and only
    # "passed" locally where a real coord.db happened to exist.
    app = build_app(SqliteStore(tmp_path / "rw.db"), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    assert "merge_plan" in board
    assert len(board["merge_plan"]) == 1
    pm = board["merge_plan"][0]

    # Required fields from the #776 spec
    assert pm["assignment_id"] == "w1"
    assert pm["rank"] == 1
    assert pm["status"] in (mq.PLAN_READY, mq.PLAN_BLOCKED)
    assert "reason" in pm
    assert pm["target_branch"] == "main"
    assert pm["size"] == 42
    assert pm["enqueued_at"] is not None
    assert pm["milestone"] is None  # not in issues table


def test_board_merge_plan_does_not_503_on_plan_error(
    file_db: Path, valid_config_path: Path, monkeypatch
) -> None:
    """/board returns 200 even when plan() raises — merge_plan falls back to []."""
    from coord import merge_queue as mq
    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    monkeypatch.setattr(mq, "plan", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(file_db), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board")
    assert resp.status_code == 200
    body = resp.json()
    assert body["merge_plan"] == []


# ── #781: _auto_drain_tick ────────────────────────────────────────────────────


def _seed_queued_ready_entry(
    conn,
    *,
    aid: str = "work-drain1",
    branch: str = "issue-55-impl",
    issue_number: int = 55,
) -> None:
    """Seed a fully-gated (approved + tested) done work assignment AND a
    corresponding pending merge_queue row.

    After this seed:
    - ``plan()`` sees the work has an approved review + passed test verdict →
      marks the entry ``PLAN_READY`` (all gates pass).
    - ``_auto_drain_tick`` should pick it up and call ``process()``.
    """
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch, test_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "acme/api", issue_number, "The issue", "done", "work", branch, "passed"),
    )
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, review_of_assignment_id, review_verdict) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            f"rev-{aid}", "server", "api", "acme/api", issue_number, "Review of issue",
            "done", "review", aid, "approve",
        ),
    )
    conn.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (aid, "api", "acme/api", branch, "main", issue_number, "The issue", "pending"),
    )
    conn.commit()


def _seed_queued_blocked_entry(
    conn,
    *,
    aid: str = "work-blocked1",
    branch: str = "issue-56-impl",
    issue_number: int = 56,
) -> None:
    """Seed a done work assignment with NO approved review + a pending queue row.

    ``plan()`` marks this entry ``PLAN_BLOCKED`` (review not approved), so
    ``_auto_drain_tick`` must skip it.
    """
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')")
    conn.execute("INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '1')")
    conn.execute(
        "INSERT INTO assignments "
        "(assignment_id, machine_name, repo_name, repo_github, issue_number, "
        " issue_title, status, type, branch, test_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (aid, "laptop", "api", "acme/api", issue_number, "The issue", "done", "work", branch, "passed"),
    )
    # No review row — plan() will evaluate review gate → BLOCKED.
    conn.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (aid, "api", "acme/api", branch, "main", issue_number, "The issue", "pending"),
    )
    conn.commit()


def _make_drain_config(tmp_path: "Path", *, auto_drain: bool = True) -> "Path":
    """Write a coordinator.yml with merge.auto_drain set and return its path."""
    content = (
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tailnet\n"
        "    capabilities: [python]\n"
        "    repos: [api]\n"
        "\n"
        f"merge:\n"
        f"  auto_drain: {'true' if auto_drain else 'false'}\n"
    )
    p = tmp_path / "coord-drain.yml"
    p.write_text(content)
    return p


def test_auto_drain_config_default_off(valid_config_path: "Path") -> None:
    """#781: merge.auto_drain defaults to False when the merge: block is absent."""
    from coord.config import load as load_config

    cfg = load_config(valid_config_path)
    assert cfg.merge.auto_drain is False
    assert cfg.merge.max_per_tick == 0


def test_auto_drain_ready_entry_merges(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#781: _auto_drain_tick() merges a READY entry when auto_drain is enabled.

    Gate conditions met (approved review + passed test), so plan() marks the
    entry READY and _auto_drain_tick calls process() which merges the PR.
    """
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.merge_queue import MERGED
    from coord.serve_app import _auto_drain_tick

    # Stub out github_ops so process() never shells out.
    monkeypatch.setattr(
        "coord.github_ops.create_pr",
        lambda repo, *, base, head, title, body: {
            "number": 201, "url": "https://gh/201", "existed": False
        },
    )
    monkeypatch.setattr("coord.github_ops.get_pr_size", lambda repo, number: 42)
    monkeypatch.setattr("coord.github_ops.merge_pr", lambda repo, number, method="rebase": (True, "merged"))
    # NoOpCi so CI gate is always a pass (is_available=False).  Patch at the
    # source module — _auto_drain_tick imports build_ci_store as a local import.
    from coord.ci_store import NoOpCi as _NoOpCi
    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t: _NoOpCi())

    _seed_queued_ready_entry(rw_db)
    cfg = load_config(_make_drain_config(tmp_path, auto_drain=True))
    assert cfg.merge.auto_drain is True

    events = _auto_drain_tick(cfg)

    # At least one "merged" event emitted.
    merge_events = [ev for ev in events if ev.kind == "merged"]
    assert merge_events, f"expected a merged event, got: {[ev.kind for ev in events]}"
    assert merge_events[0].entry.assignment_id == "work-drain1"

    # Queue entry transitioned to MERGED.
    items = mq.load_queue()
    assert any(item.state == MERGED for item in items), (
        f"expected MERGED in queue, got: {[item.state for item in items]}"
    )


def test_auto_drain_blocked_entry_not_touched(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#781: _auto_drain_tick() skips a BLOCKED entry — no merge call, state unchanged.

    The blocked entry has no approved review, so plan() marks it PLAN_BLOCKED.
    _auto_drain_tick should return an empty events list and leave the queue row
    in its original 'pending' state.
    """
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _auto_drain_tick

    # Track any merge calls — there should be none.
    merge_calls: list = []
    monkeypatch.setattr(
        "coord.github_ops.merge_pr",
        lambda repo, number, method="rebase": merge_calls.append((repo, number)) or (True, "merged"),
    )
    from coord.ci_store import NoOpCi as _NoOpCi
    monkeypatch.setattr("coord.ci_store.build_ci_store", lambda t: _NoOpCi())

    _seed_queued_blocked_entry(rw_db)
    cfg = load_config(_make_drain_config(tmp_path, auto_drain=True))

    events = _auto_drain_tick(cfg)

    # No events — BLOCKED entry was skipped entirely.
    assert events == [], f"expected no events for blocked entry, got: {[ev.kind for ev in events]}"
    assert merge_calls == [], "merge_pr must not be called for a BLOCKED entry"

    # Queue row is still pending.
    items = mq.load_queue()
    assert len(items) == 1
    assert items[0].state == "pending"


def test_auto_drain_error_isolation(
    tmp_path: "Path", rw_db, monkeypatch
) -> None:
    """#781: an error inside _auto_drain_tick propagates cleanly so the tick
    loop's try/except can absorb it without crashing the daemon.

    Verifies two isolation properties:
    1. The error raised by plan() bubbles out of _auto_drain_tick (the caller
       is responsible for catching it — matching the pattern of every other tick
       step in _tick_loop).
    2. The queue is left untouched (no partial writes on error).
    """
    import pytest
    from coord.config import load as load_config
    from coord import merge_queue as mq
    from coord.serve_app import _auto_drain_tick

    _seed_queued_ready_entry(rw_db)
    cfg = load_config(_make_drain_config(tmp_path, auto_drain=True))

    original_items = mq.load_queue()
    assert len(original_items) == 1

    # Simulate a transient CI-lookup failure inside plan().
    monkeypatch.setattr(
        mq, "plan",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ci lookup exploded")),
    )

    with pytest.raises(RuntimeError, match="ci lookup exploded"):
        _auto_drain_tick(cfg)

    # Queue is unchanged — no partial writes occurred.
    after = mq.load_queue()
    assert len(after) == 1
    assert after[0].state == "pending"
