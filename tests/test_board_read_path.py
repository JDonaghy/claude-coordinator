"""#1336/#1337: the rearchitected `/board` read path and its invariants.

The failure class this file guards against (third occurrence: #762, #715,
#1336) is an unbounded payload growing until it crosses a fixed client
timeout — and a *write* being discarded because a *read* failed.  These are
the enforcement tests for the invariants:

1. Read endpoints perform no third-party I/O (no `gh` subprocess on /board).
2. Collection endpoints carry no unbounded free text (bounded previews +
   ``*_truncated`` flags; full text on detail endpoints only).
3. Point lookups get point endpoints (GET /assignment/{id}, /issue/{r}/{n}).
4. Writes never depend on reads (report-result survives a failed prefetch;
   the daemon enriches identity itself).
5. Polling is cache-validated (ETag / If-None-Match → 304).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.serve_app import build_app


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type, branch, "
        "files_allowed, briefing, review_findings, test_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "work1", "laptop", "api", "acme/api", 42, "A work issue",
            "done", "work", "issue-42-fix",
            '["a.py"]', "b" * 5000,
            json.dumps({"verdict": "request-changes", "body": "F" * 9000}),
            "t" * 6000,
        ),
    )
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "synced_at) VALUES (?,?,?,?,?,?,?)",
        ("api", 42, "A work issue", "B" * 9000, "open", '["bug"]', 0.0),
    )
    conn.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', '3')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def detail_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_db(p)
    return p


@pytest.fixture
def app_client(detail_db: Path, valid_config_path: Path) -> TestClient:
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(detail_db), cfg)
    with TestClient(app) as cli:
        yield cli


# ── Invariant 3: point endpoints ─────────────────────────────────────────────


def test_get_assignment_serves_full_row(app_client: TestClient) -> None:
    resp = app_client.get("/assignment/work1")
    assert resp.status_code == 200
    row = resp.json()
    # The detail endpoint serves the COMPLETE row: briefing (dropped from the
    # collection wire since forever) and the full unbounded text fields.
    assert row["briefing"] == "b" * 5000
    assert row["test_reason"] == "t" * 6000
    assert json.loads(row["review_findings"])["body"] == "F" * 9000
    # JSON columns decoded, same as the collection wire.
    assert row["files_allowed"] == ["a.py"]


def test_get_assignment_404_on_unknown_id(app_client: TestClient) -> None:
    resp = app_client.get("/assignment/nope")
    assert resp.status_code == 404
    assert resp.json()["error"] == "unknown assignment"


def test_get_issue_serves_full_body(app_client: TestClient) -> None:
    resp = app_client.get("/issue/api/42")
    assert resp.status_code == 200
    row = resp.json()
    assert row["body"] == "B" * 9000
    assert row["labels"] == ["bug"]


def test_get_issue_404_on_unknown(app_client: TestClient) -> None:
    assert app_client.get("/issue/api/999").status_code == 404


def test_detail_endpoints_require_auth_when_token_set(
    detail_db: Path, valid_config_path: Path
) -> None:
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(detail_db), cfg, token="s3cret")
    with TestClient(app) as cli:
        assert cli.get("/assignment/work1").status_code == 401
        ok = cli.get(
            "/assignment/work1", headers={"Authorization": "Bearer s3cret"}
        )
        assert ok.status_code == 200


def test_get_assignment_makes_no_gh_calls(
    app_client: TestClient, monkeypatch
) -> None:
    """The detail endpoint is a point SELECT — never a `gh` subprocess."""
    import subprocess

    def _no_gh(*args, **kwargs):  # noqa: ANN002, ANN003
        argv = args[0] if args else kwargs.get("args")
        raise AssertionError(f"subprocess spawned on detail read: {argv!r}")

    monkeypatch.setattr(subprocess, "run", _no_gh)
    monkeypatch.setattr(subprocess, "Popen", _no_gh)
    assert app_client.get("/assignment/work1").status_code == 200


# ── Invariant 4: writes never depend on reads ────────────────────────────────


def test_post_result_enriches_blank_identity_from_daemon_db(
    detail_db: Path, valid_config_path: Path, tmp_path: Path, monkeypatch
) -> None:
    """A thin client whose identity prefetch failed POSTs the record with
    blank identity fields — the daemon must resolve them from its own
    assignments row and still land the write (the #1336 lost-verdict fix)."""
    import coord.db as db_mod
    import coord.issue_store as issue_store

    # Thread-safe file-backed rw DB for the handler's state writes.
    rw = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    rw.row_factory = sqlite3.Row
    _ensure_schema(rw)
    rw.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("rev9", "server", "api", "acme/api", 42, "review of #42",
         "running", "review"),
    )
    rw.commit()
    db_mod.override_connection(rw)

    posted: dict = {}

    def _fake_comment(*, repo_github: str, issue_number: int, body: str):
        posted["repo_github"] = repo_github
        posted["issue_number"] = issue_number
        return True, None

    monkeypatch.setattr(issue_store, "_post_github_comment", _fake_comment)

    # The daemon's read store needs the same row (identity resolution reads
    # the read-only DAO); mirror it into the file DB backing the app.
    seed = sqlite3.connect(str(detail_db))
    seed.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "repo_github, issue_number, issue_title, status, type) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("rev9", "server", "api", "acme/api", 42, "review of #42",
         "running", "review"),
    )
    seed.commit()
    seed.close()

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(detail_db), cfg)
    with TestClient(app) as cli:
        resp = cli.post(
            "/result",
            json={
                "assignment_id": "rev9",
                # Blank identity: the failed-prefetch client shape.
                "machine_name": "",
                "repo_name": "",
                "repo_github": "",
                "issue_number": 0,
                "status": "done",
                "verdict": "approve",
                "summary": "looks good",
            },
        )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "done"
    # The GitHub comment went to the identity the DAEMON resolved.
    assert posted == {"repo_github": "acme/api", "issue_number": 42}
    # And the terminal write landed on the row.
    row = rw.execute(
        "SELECT status, review_verdict FROM assignments WHERE assignment_id='rev9'"
    ).fetchone()
    assert row["status"] == "done"
    assert row["review_verdict"] == "approve"
    db_mod.close()


def test_report_result_survives_failed_prefetch(monkeypatch, coord_db) -> None:
    """CLI half of invariant 4: a failed/slow board read must WARN and proceed
    with the POST — never sys.exit(1) and discard the verdict."""
    from click.testing import CliRunner

    import coord.client as cc
    from coord import issue_store
    from coord.commands.review import report_result

    class _Svc:
        url = "http://daemon:7435"
        token = None

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _Svc())

    def _timeout(*a, **k):
        raise TimeoutError("timed out")

    # Both the point endpoint and the collection fallback fail.
    monkeypatch.setattr(cc, "fetch_assignment", _timeout)
    monkeypatch.setattr(cc, "fetch_board_payload", _timeout)

    recorded: dict = {}

    def _fake_post_result(record):
        recorded["record"] = record
        return issue_store.StoreOutcome(status="done", event="done", posted=True)

    monkeypatch.setattr(issue_store, "post_result", _fake_post_result)

    runner = CliRunner()
    result = runner.invoke(
        report_result,
        [
            "--assignment", "rev-1336",
            "--status", "done",
            "--verdict", "approve",
            "--summary", "ok",
        ],
    )
    assert result.exit_code == 0, result.output
    # The verdict reached the seam despite the failed read.
    assert recorded["record"].assignment_id == "rev-1336"
    assert recorded["record"].verdict == "approve"
    # The warning names the real cause — a board READ failure — not a
    # misleading "could not reach board service" (the #1336 wild-goose chase).
    from tests.conftest import output_and_stderr

    text = output_and_stderr(result)
    assert "identity prefetch" in text
    assert "BOARD READ" in text
    assert "could not reach board service" not in text


def test_report_result_prefers_point_endpoint(monkeypatch, coord_db) -> None:
    """The identity prefetch uses GET /assignment/{id} — not a full /board
    collection fetch (invariant 3)."""
    from click.testing import CliRunner

    import coord.client as cc
    from coord import issue_store
    from coord.commands.review import report_result

    class _Svc:
        url = "http://daemon:7435"
        token = None

    monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _Svc())
    monkeypatch.setattr(
        cc,
        "fetch_assignment",
        lambda svc, aid, **kw: {
            "assignment_id": aid,
            "machine_name": "server",
            "repo_name": "api",
            "repo_github": "acme/api",
            "issue_number": 42,
            "branch": None,
        },
    )

    def _collection_forbidden(*a, **k):
        raise AssertionError(
            "fetch_board_payload called — the point endpoint should have "
            "resolved the identity"
        )

    monkeypatch.setattr(cc, "fetch_board_payload", _collection_forbidden)

    recorded: dict = {}

    def _fake_post_result(record):
        recorded["record"] = record
        return issue_store.StoreOutcome(status="done", event="done", posted=True)

    monkeypatch.setattr(issue_store, "post_result", _fake_post_result)

    runner = CliRunner()
    result = runner.invoke(
        report_result,
        ["--assignment", "rev-1", "--status", "done", "--verdict", "approve"],
    )
    assert result.exit_code == 0, result.output
    assert recorded["record"].repo_github == "acme/api"
    assert recorded["record"].issue_number == 42
