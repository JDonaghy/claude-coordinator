"""#2087: an assignment naming an unconfigured repo/machine must not persist.

2026-08-10: a scratch reproduction script called `record_dispatched` /
`record_dispatched_assignment` directly against the DEFAULT state path
(`~/.coord/coord.db`, the daemon host's canonical DB) with test-fixture
values — `machine=laptop`, `repo=api`/`acme/api` — that name no real machine
or repo. Nothing at the write layer objected. `coord assign`'s own CLI-level
check (coord/commands/dispatch.py) never ran because this call bypassed the
CLI entirely; it is only ONE of many callers of these functions.

These tests exercise the fix at the WRITE WAIST itself
(`coord.state._validate_dispatch_target`, wired into `_record_dispatched_local`
and `_record_dispatched_assignment_local`) — not the pre-existing `coord
assign` CLI check (covered by tests/test_cli_assign.py) — so a caller that
skips the CLI (a scratch script, `coord drive`, the daemon's own HTTP
handlers, a future dispatch path) still gets refused.
"""

from __future__ import annotations

import dataclasses

import pytest
from starlette.testclient import TestClient

from coord.config import Config
from coord.dao import SqliteStore
from coord.models import Assignment, Machine, Proposal, Repo
from coord.serve_app import build_app
from coord.state import (
    UnknownDispatchTargetError,
    _dispatch_target_config as _REAL_DISPATCH_TARGET_CONFIG,
    _record_dispatched_assignment_local,
    _record_dispatched_local,
    _validate_dispatch_target,
    record_dispatched,
    record_dispatched_assignment,
)

# The real fleet shape from the incident report: dellserver/elitebook/
# precision are configured; `laptop` never was. `api`/`acme/api` is not a
# configured repo either — the config only knows a differently-named one.
FLEET_CONFIG = Config(
    repos=[Repo(name="widgets", github="acme/widgets")],
    machines=[
        Machine(name="dellserver", host="dellserver.tailnet", repos=["widgets"]),
        Machine(name="elitebook", host="elitebook.tailnet", repos=["widgets"]),
        Machine(name="precision", host="precision.tailnet", repos=["widgets"]),
    ],
)


def _enable_validation(monkeypatch, cfg: Config | None) -> None:
    """Override conftest.py's autouse no-op default so this test exercises
    the real gate against *cfg* — mirrors production, where
    `_dispatch_target_config()` always resolves a real Config."""
    monkeypatch.setattr("coord.state._dispatch_target_config", lambda: cfg)


# ── _validate_dispatch_target: the gate itself ──────────────────────────────


class TestValidateDispatchTarget:
    def test_unknown_repo_named_in_error(self, monkeypatch) -> None:
        _enable_validation(monkeypatch, FLEET_CONFIG)
        with pytest.raises(UnknownDispatchTargetError, match="api"):
            _validate_dispatch_target(repo_name="api", machine_name="dellserver")

    def test_unknown_machine_named_in_error(self, monkeypatch) -> None:
        _enable_validation(monkeypatch, FLEET_CONFIG)
        with pytest.raises(UnknownDispatchTargetError, match="laptop"):
            _validate_dispatch_target(repo_name="widgets", machine_name="laptop")

    def test_known_repo_and_machine_pass(self, monkeypatch) -> None:
        _enable_validation(monkeypatch, FLEET_CONFIG)
        _validate_dispatch_target(repo_name="widgets", machine_name="precision")  # no raise

    def test_config_none_skips_validation(self, monkeypatch) -> None:
        """The conftest.py autouse default (and this test's own explicit
        opt-out) — validation is a no-op when the seam returns None."""
        _enable_validation(monkeypatch, None)
        _validate_dispatch_target(repo_name="anything", machine_name="anything")  # no raise

    def test_production_default_loads_the_real_coordinator_yml(self, monkeypatch) -> None:
        """Without conftest.py's override, `_dispatch_target_config()` calls
        the real `coord.config.load()` — the actual gate a stray
        reproduction script hits when it doesn't pass a config at all. Calls
        the pristine function captured at module-import time (before any
        fixture patched `coord.state._dispatch_target_config`), mirroring
        conftest.py's own `_REAL_SUBPROCESS_RUN` pattern."""
        sentinel = FLEET_CONFIG
        monkeypatch.setattr("coord.config.load", lambda *a, **k: sentinel)
        assert _REAL_DISPATCH_TARGET_CONFIG() is sentinel


# ── the write waist: must not persist ───────────────────────────────────────


class TestRecordDispatchedRefusesUnknownTarget:
    """`record_dispatched` / `_record_dispatched_local` — the Proposal-based
    path a plain `coord assign`/`coord drive` work dispatch uses (the
    incident's `work-repro` row: `type="work"`)."""

    def test_dispatch_to_unknown_machine_and_repo_raises_and_does_not_persist(
        self, monkeypatch, coord_db,
    ) -> None:
        """#2087 acceptance #4: the literal incident repro — dispatching
        api#9999 to laptop against a config declaring neither machine nor
        repo must fail, and nothing must land in the assignments table."""
        _enable_validation(monkeypatch, FLEET_CONFIG)
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=9999, issue_title="Some work", rationale="repro",
        )
        with pytest.raises(UnknownDispatchTargetError):
            record_dispatched(
                assignment_id="work-repro", proposal=proposal, repo_github="acme/api",
            )
        row = coord_db.execute(
            "SELECT * FROM assignments WHERE assignment_id='work-repro'"
        ).fetchone()
        assert row is None

    def test_known_machine_and_repo_still_dispatch_normally(self, monkeypatch, coord_db) -> None:
        """No regression: a real target still writes through."""
        _enable_validation(monkeypatch, FLEET_CONFIG)
        proposal = Proposal(
            id=2, machine_name="precision", repo_name="widgets",
            issue_number=1, issue_title="real work", rationale="ok",
        )
        record_dispatched(
            assignment_id="real1", proposal=proposal, repo_github="acme/widgets",
        )
        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id='real1'"
        ).fetchone()
        assert row["status"] == "running"

    def test_local_writer_called_directly_is_also_refused(self, monkeypatch, coord_db) -> None:
        """The public `record_dispatched` wrapper only routes to the daemon
        when a board_service is configured; a caller that reaches
        `_record_dispatched_local` directly (as the daemon's own
        `/dispatched-work` handler does, server-side) must be refused too —
        the gate lives in the `_local` writer itself, not the wrapper."""
        _enable_validation(monkeypatch, FLEET_CONFIG)
        proposal = Proposal(
            id=3, machine_name="laptop", repo_name="api",
            issue_number=9999, issue_title="Some work", rationale="repro",
        )
        with pytest.raises(UnknownDispatchTargetError):
            _record_dispatched_local(
                assignment_id="work-repro-2", proposal=proposal, repo_github="acme/api",
            )
        row = coord_db.execute(
            "SELECT * FROM assignments WHERE assignment_id='work-repro-2'"
        ).fetchone()
        assert row is None


class TestRecordDispatchedAssignmentRefusesUnknownTarget:
    """`record_dispatched_assignment` / `_record_dispatched_assignment_local`
    — the Assignment-based path review/smoke/retry dispatches use (the
    incident's `smoke-repro` row: `type="smoke"`)."""

    def test_smoke_dispatch_to_unknown_machine_and_repo_raises_and_does_not_persist(
        self, monkeypatch, coord_db,
    ) -> None:
        """The incident's second phantom row, reproduced exactly:
        `type="smoke"`, `machine=laptop`, `repo=api`, `issue=9999`."""
        _enable_validation(monkeypatch, FLEET_CONFIG)
        smoke = Assignment(
            machine_name="laptop", repo_name="api", issue_number=9999,
            issue_title="Some work", assignment_id="smoke-repro", type="smoke",
        )
        with pytest.raises(UnknownDispatchTargetError):
            record_dispatched_assignment(assignment=smoke, repo_github="acme/api")
        row = coord_db.execute(
            "SELECT * FROM assignments WHERE assignment_id='smoke-repro'"
        ).fetchone()
        assert row is None

    def test_local_writer_called_directly_is_also_refused(self, monkeypatch, coord_db) -> None:
        _enable_validation(monkeypatch, FLEET_CONFIG)
        smoke = Assignment(
            machine_name="laptop", repo_name="api", issue_number=9999,
            issue_title="Some work", assignment_id="smoke-repro-2", type="smoke",
        )
        with pytest.raises(UnknownDispatchTargetError):
            _record_dispatched_assignment_local(assignment=smoke, repo_github="acme/api")
        row = coord_db.execute(
            "SELECT * FROM assignments WHERE assignment_id='smoke-repro-2'"
        ).fetchone()
        assert row is None

    def test_known_machine_and_repo_still_dispatch_normally(self, monkeypatch, coord_db) -> None:
        _enable_validation(monkeypatch, FLEET_CONFIG)
        a = Assignment(
            machine_name="precision", repo_name="widgets", issue_number=2,
            issue_title="real review", assignment_id="real-review", type="review",
        )
        record_dispatched_assignment(assignment=a, repo_github="acme/widgets")
        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id='real-review'"
        ).fetchone()
        assert row["status"] == "running"


# ── the daemon's own HTTP write path (the incident's actual mechanism:
# a caller landing straight on the daemon host's local, canonical DB) ───────


class TestDaemonDispatchEndpointsRefuseUnknownTarget:
    @pytest.fixture
    def rw_db(self, tmp_path):
        import sqlite3
        from coord import db
        from coord.db import _ensure_schema

        conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db.override_connection(conn)
        yield conn

    @pytest.fixture
    def file_db(self, tmp_path):
        import sqlite3
        from coord.db import _ensure_schema

        p = tmp_path / "coord.db"
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        conn.commit()
        conn.close()
        return p

    def test_post_dispatched_work_unknown_target_is_400_not_503(
        self, monkeypatch, file_db, rw_db,
    ) -> None:
        _enable_validation(monkeypatch, FLEET_CONFIG)
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=9999, issue_title="Some work", rationale="repro",
        )
        app = build_app(SqliteStore(file_db), FLEET_CONFIG)
        with TestClient(app) as cli:
            resp = cli.post(
                "/dispatched-work",
                json={
                    "assignment_id": "work-repro",
                    "proposal": dataclasses.asdict(proposal),
                    "repo_github": "acme/api",
                },
            )
        assert resp.status_code == 400, resp.text
        assert "laptop" in resp.text or "api" in resp.text
        row = rw_db.execute(
            "SELECT * FROM assignments WHERE assignment_id='work-repro'"
        ).fetchone()
        assert row is None

    def test_post_dispatched_unknown_target_is_400_not_503(
        self, monkeypatch, file_db, rw_db,
    ) -> None:
        _enable_validation(monkeypatch, FLEET_CONFIG)
        smoke = Assignment(
            machine_name="laptop", repo_name="api", issue_number=9999,
            issue_title="Some work", assignment_id="smoke-repro", type="smoke",
        )
        app = build_app(SqliteStore(file_db), FLEET_CONFIG)
        with TestClient(app) as cli:
            resp = cli.post(
                "/dispatched",
                json={
                    "assignment": dataclasses.asdict(smoke),
                    "repo_github": "acme/api",
                },
            )
        assert resp.status_code == 400, resp.text
        row = rw_db.execute(
            "SELECT * FROM assignments WHERE assignment_id='smoke-repro'"
        ).fetchone()
        assert row is None
