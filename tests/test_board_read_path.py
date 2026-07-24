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


# ── Invariant 1: read endpoints perform no third-party I/O ───────────────────


def _seed_pending_merge(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, "
        "branch, target_branch, issue_number, issue_title, state, pr_number) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("work1", "api", "acme/api", "issue-42-fix", "main", 42,
         "A work issue", "pending", 7),
    )
    conn.commit()


@pytest.fixture
def rw_db(tmp_path: Path):
    """Thread-safe file-backed coord.db override for TestClient tests
    (mirrors the established pattern — the autouse ``coord_db`` fixture's
    thread-bound ``:memory:`` conn is unusable from the ASGI worker thread)."""
    import coord.db as db_mod

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db_mod.override_connection(conn)
    yield conn
    db_mod.close()


def test_board_read_makes_zero_gh_calls(
    detail_db: Path, valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """THE guard for invariant 1 (the #762/#715/#1336 failure class): a cold
    /board build over a board with pending merge-queue entries (PR numbers
    present, ci_store=github by default) must spawn no subprocess at all —
    CI checks and the epic-closing gate are served from the tick-refreshed
    gate snapshot, never fetched inline."""
    import subprocess

    spawned: list = []

    def _spy(*args, **kwargs):  # noqa: ANN002, ANN003
        argv = args[0] if args else kwargs.get("args")
        spawned.append(argv)
        raise AssertionError(f"subprocess spawned on board read: {argv!r}")

    monkeypatch.setattr(subprocess, "run", _spy)
    monkeypatch.setattr(subprocess, "Popen", _spy)
    monkeypatch.setattr(subprocess, "check_output", _spy)

    _seed_pending_merge(rw_db)

    cfg = load_config(valid_config_path)
    assert cfg.ci_store.type == "github"  # the gate IS configured on
    app = build_app(SqliteStore(detail_db), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board")
    assert resp.status_code == 200
    board = resp.json()
    # The plan was genuinely computed over the pending entry (not blanked by
    # an error path) — it simply carries fail-open gate values until the
    # tick's next snapshot refresh.
    assert [pm["assignment_id"] for pm in board["merge_plan"]] == ["work1"]
    assert spawned == []


def test_board_serves_ci_from_gate_snapshot(
    detail_db: Path, valid_config_path: Path, rw_db, monkeypatch
) -> None:
    """The merge plan's CI annotations come from the refreshed snapshot."""
    from coord.ci_store import CheckRun
    from coord.gate_snapshot import GateSnapshot, GateSnapshotRefresher

    _seed_pending_merge(rw_db)

    # Pass the review + test gates (they precede CI) so the CI gate is the
    # one that decides: an approved review row + a passed test verdict.
    rw_db.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, branch, test_state) "
        "VALUES ('work1','laptop','api',42,'A work issue','done','work',"
        "'issue-42-fix','passed')"
    )
    rw_db.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, review_of_assignment_id, "
        "review_verdict) VALUES ('rev1','server','api',42,'Review of #42',"
        "'done','review','work1','approve')"
    )
    rw_db.commit()

    failed = CheckRun(
        name="pytest", status="completed", conclusion="failure",
        url="", run_id="1", started_at=None, completed_at=None,
    )
    snap = GateSnapshot(
        checks={("acme/api", 7): [failed]},
        ci_available=True,
        refreshed_at=1.0,
    )
    monkeypatch.setattr(GateSnapshotRefresher, "snapshot", lambda self: snap)

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(detail_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()
    (pm,) = board["merge_plan"]
    assert pm["status"] == "BLOCKED"
    assert "CI failed: pytest" in pm["reason"]


def test_gate_refresher_populates_snapshot_from_queue(rw_db, monkeypatch) -> None:
    """refresh() fetches per pending-PR entry and publishes atomically."""
    import coord.gate_snapshot as gs
    from coord.ci_store import CheckRun
    from coord.config import Config

    _seed_pending_merge(rw_db)

    calls: list = []

    class _FakeCi:
        is_available = True

        def list_checks_for_pr(self, repo: str, number: int):
            calls.append(("checks", repo, number))
            return [
                CheckRun(
                    name="ci", status="completed", conclusion="success",
                    url="", run_id="1", started_at=None, completed_at=None,
                )
            ]

    monkeypatch.setattr(gs, "build_ci_store", lambda t: _FakeCi())

    import coord.github_ops as github_ops

    monkeypatch.setattr(
        github_ops,
        "get_pr_commit_messages",
        lambda repo, n: [f"fix(#42): thing\n\nCloses #90 (repo={repo} pr={n})"],
    )
    monkeypatch.setattr(
        github_ops, "is_epic_issue", lambda repo, n: n == 90
    )

    refresher = gs.GateSnapshotRefresher()
    # Pre-refresh: fail-open empties.
    assert refresher.snapshot().list_checks_for_pr("acme/api", 7) == []
    assert refresher.snapshot().is_available is False

    snap = refresher.refresh(Config(repos=[], machines=[]))
    assert calls == [("checks", "acme/api", 7)]
    assert snap.is_available is True
    assert [c.name for c in snap.list_checks_for_pr("acme/api", 7)] == ["ci"]
    assert snap.get_pr_commit_messages("acme/api", 7)
    assert snap.is_epic_issue("acme/api", 90) is True
    assert snap.is_epic_issue("acme/api", 42) is False
    assert refresher.snapshot() is snap


# ── Invariant 2: no collection endpoint returns unbounded text ───────────────


def test_board_wire_bounds_assignment_text_fields(app_client: TestClient) -> None:
    """The collection wire serves bounded previews + explicit flags; the full
    text stays on the detail endpoint (verified above)."""
    from coord.board_wire import PREVIEW_CHARS, TRUNCATION_NOTICE

    board = app_client.get("/board").json()
    work = next(a for a in board["assignments"] if a["assignment_id"] == "work1")

    # review_findings: envelope-aware — verdict intact, body previewed, JSON
    # still parseable (the TUI parses this raw string).
    env = json.loads(work["review_findings"])
    assert env["verdict"] == "request-changes"
    assert len(env["body"]) <= PREVIEW_CHARS + len(TRUNCATION_NOTICE)
    assert env["truncated"] is True
    assert work["review_findings_truncated"] is True
    assert work["review_findings_len"] > PREVIEW_CHARS

    # test_reason: plain-text preview + flags.
    assert len(work["test_reason"]) <= PREVIEW_CHARS + len(TRUNCATION_NOTICE)
    assert work["test_reason"].startswith("t" * 100)
    assert work["test_reason_truncated"] is True
    assert work["test_reason_len"] == 6000


def test_board_wire_bounds_issue_bodies(app_client: TestClient) -> None:
    """Issue bodies get the high document cap (semantic parses — work orders,
    ## Files globs — must survive for every real body; today p99 ≈ 9 KB)."""
    from coord.board_wire import DOCUMENT_CHARS

    board = app_client.get("/board").json()
    issue = next(i for i in board["issues"] if i["number"] == 42)
    # 9 KB body is under the document cap: served whole, no flag.
    assert issue["body"] == "B" * 9000
    assert "body_truncated" not in issue
    assert DOCUMENT_CHARS >= 16384


def test_board_wire_short_fields_untouched(
    tmp_path: Path, valid_config_path: Path
) -> None:
    """Fields at/under the caps are byte-identical with no flags — the common
    case is unchanged on the wire."""
    p = tmp_path / "short.db"
    conn = sqlite3.connect(str(p))
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, review_findings, test_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("a1", "m", "api", 1, "t", "done", "review",
         json.dumps({"verdict": "approve", "body": "short"}), "brief reason"),
    )
    conn.commit()
    conn.close()
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(p), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()
    (row,) = board["assignments"]
    assert json.loads(row["review_findings"]) == {
        "verdict": "approve", "body": "short",
    }
    assert row["test_reason"] == "brief reason"
    assert "review_findings_truncated" not in row
    assert "test_reason_truncated" not in row


def test_board_payload_size_budget(
    tmp_path: Path, valid_config_path: Path
) -> None:
    """THE growth guard (instance #4 of #762/#715/#1336 must fail here first):
    a board seeded with pathological per-row text — the exact growth vector
    that produced the 5.5 MB payload — must stay within a hard wire budget.

    150 assignments x (10 KB findings + 8 KB reasons) + 60 issues x 64 KB
    bodies was ~5.3 MB of text pre-#1337.  Budget: 3 MB for the whole
    payload.  If a new unbounded field is ever added to the collection wire,
    this test is the tripwire.
    """
    p = tmp_path / "big.db"
    conn = sqlite3.connect(str(p))
    _ensure_schema(conn)
    findings = json.dumps({"verdict": "request-changes", "body": "F" * 10_000})
    now = __import__("time").time()
    for i in range(150):
        conn.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, status, type, dispatched_at, "
            "briefing, review_findings, test_reason, smoke_test_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"a{i}", "m", "api", i, f"issue {i}", "running", "work", now,
             "b" * 20_000, findings, "t" * 8_000, "s" * 8_000),
        )
    for i in range(60):
        conn.execute(
            "INSERT INTO issues (repo_name, number, title, body, state, "
            "labels, synced_at) VALUES (?,?,?,?,?,?,?)",
            ("api", i, f"issue {i}", "B" * 65_536, "open", "[]", now),
        )
    conn.commit()
    conn.close()

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(p), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board")
    assert resp.status_code == 200
    size = len(resp.content)
    budget = 3_000_000
    assert size < budget, (
        f"/board payload is {size} bytes (> {budget}). An unbounded field is "
        "back on the collection wire — bound it in coord.board_wire and serve "
        "the full text from a detail endpoint (#1337)."
    )
    # And no row-level field escaped its cap.
    board = resp.json()
    for a in board["assignments"]:
        for fld in ("review_findings", "test_reason", "smoke_test_reason"):
            val = a.get(fld)
            assert val is None or len(val) < 25_000, (fld, len(val))
        assert "briefing" not in a
    for i in board["issues"]:
        assert len(i.get("body") or "") <= 17_000


def test_post_board_roundtrip_cannot_clobber_full_text(rw_db) -> None:
    """A thin client that read the BOUNDED wire and posts the whole board back
    (POST /board → save_board upsert) must not overwrite the stored full
    text with previews — the free-text columns are excluded from the
    whole-board upsert's UPDATE clause."""
    from coord.models import Assignment, Board
    from coord.state import save_board

    rw_db.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, briefing, test_reason, "
        "smoke_test_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("w1", "m", "api", 1, "t", "running", "work",
         "FULL BRIEFING " * 100, "FULL REASON " * 100, "FULL SMOKE " * 100),
    )
    rw_db.commit()

    # The round-tripped assignment carries wire-bounded / defaulted values.
    a = Assignment(
        assignment_id="w1", machine_name="m", repo_name="api",
        issue_number=1, issue_title="t", status="done",
        briefing="",  # the wire never carries briefing
    )
    a.test_reason = "preview…"
    a.smoke_test_reason = "preview…"
    save_board(Board(active=[], completed=[a], round_number=0))

    row = rw_db.execute(
        "SELECT status, briefing, test_reason, smoke_test_reason "
        "FROM assignments WHERE assignment_id='w1'"
    ).fetchone()
    assert row["status"] == "done"  # bounded fields still update
    assert row["briefing"].startswith("FULL BRIEFING")
    assert row["test_reason"].startswith("FULL REASON")
    assert row["smoke_test_reason"].startswith("FULL SMOKE")


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
