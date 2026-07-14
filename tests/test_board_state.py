"""Tests for board state persistence, reconstruction, reconciliation, and GC."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from coord.models import Assignment, Board, Machine, Repo
from coord.state import save_board, load_board, build_board


# ── Board save/load roundtrip ──────────────────────────────────────────────────


class TestBoardPersistence:
    def test_save_and_load_roundtrip(self, coord_db) -> None:
        board = Board(
            active=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=10,
                    issue_title="Fix auth",
                    assignment_id="abc123",
                    status="running",
                    dispatched_at=1000.0,
                ),
            ],
            completed=[
                Assignment(
                    machine_name="server",
                    repo_name="shared",
                    issue_number=5,
                    issue_title="Add logging",
                    assignment_id="def456",
                    status="done",
                    dispatched_at=900.0,
                    finished_at=950.0,
                ),
            ],
            round_number=3,
        )
        save_board(board)
        loaded = load_board()

        assert loaded is not None
        assert loaded.round_number == 3
        assert len(loaded.active) == 1
        assert loaded.active[0].assignment_id == "abc123"
        assert loaded.active[0].machine_name == "laptop"
        assert loaded.active[0].dispatched_at == 1000.0
        assert len(loaded.completed) == 1
        assert loaded.completed[0].assignment_id == "def456"
        assert loaded.completed[0].status == "done"
        assert loaded.completed[0].finished_at == 950.0

    def test_load_empty_db_returns_none(self, coord_db) -> None:
        assert load_board() is None

    def test_save_empty_board_and_reload(self, coord_db) -> None:
        save_board(Board())
        loaded = load_board()
        assert loaded is not None
        assert loaded.active == []
        assert loaded.completed == []
        assert loaded.round_number == 0

    def test_save_updates_status(self, coord_db) -> None:
        """After saving a board where an assignment moved to done, load reflects that."""
        a = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix auth",
            assignment_id="abc123",
            status="running",
        )
        board = Board(active=[a])
        save_board(board)

        a.status = "done"
        a.branch = "issue-10-fix-auth"
        board.completed.append(a)
        board.active.remove(a)
        save_board(board)

        loaded = load_board()
        assert loaded is not None
        assert len(loaded.active) == 0
        assert len(loaded.completed) == 1
        assert loaded.completed[0].branch == "issue-10-fix-auth"
        assert loaded.completed[0].status == "done"

    def test_empty_board_roundtrip(self, coord_db) -> None:
        save_board(Board())
        loaded = load_board()
        assert loaded is not None
        assert loaded.active == []
        assert loaded.completed == []
        assert loaded.round_number == 0


# ── Build board from DB ─────────────────────────────────────────────────────────


class TestBuildBoard:
    def test_running_assignments_from_db(self, coord_db) -> None:
        from coord.state import record_dispatched
        from coord.models import Proposal
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth",
            rationale="", files_likely=["auth.py"], briefing="fix it",
        )
        record_dispatched(assignment_id="aaa", proposal=p, repo_github="acme/api")

        board = build_board()
        assert len(board.active) == 1
        assert board.active[0].assignment_id == "aaa"
        assert board.active[0].status == "running"
        assert board.active[0].files_allowed == ["auth.py"]
        assert board.completed == []

    def test_completed_assignments_from_db(self, coord_db) -> None:
        from coord.state import record_dispatched, mark_notified
        from coord.models import Proposal
        p = Proposal(
            id=1, machine_name="server", repo_name="shared",
            issue_number=5, issue_title="Add logging",
            rationale="", files_likely=[], briefing="add logs",
        )
        record_dispatched(assignment_id="bbb", proposal=p, repo_github="acme/shared")

        # Simulate save_board marking it done
        from coord.models import Board
        board = build_board()
        a = board.find_by_id("bbb")
        assert a is not None
        a.status = "done"
        board.completed.append(a)
        board.active.remove(a)
        save_board(board)

        board2 = build_board()
        assert board2.active == []
        assert len(board2.completed) == 1
        assert board2.completed[0].assignment_id == "bbb"
        assert board2.completed[0].status == "done"

    def test_failed_assignment(self, coord_db) -> None:
        from coord.state import record_dispatched
        from coord.models import Proposal, Board
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=7, issue_title="Broken",
            rationale="", files_likely=[], briefing="try",
        )
        record_dispatched(assignment_id="ccc", proposal=p, repo_github="acme/api")

        board = build_board()
        a = board.find_by_id("ccc")
        assert a is not None
        a.status = "failed"
        board.completed.append(a)
        board.active.remove(a)
        save_board(board)

        board2 = build_board()
        assert board2.active == []
        assert board2.completed[0].status == "failed"

    def test_plan_event_marks_assignment_done(self, coord_db) -> None:
        """Plan type assignment should end up done."""
        from coord.state import record_dispatched
        from coord.models import Proposal, Board
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=11, issue_title="Plan feature",
            rationale="", files_likely=[], briefing="",
            type="plan",
        )
        record_dispatched(assignment_id="ppp", proposal=p, repo_github="acme/api")

        board = build_board()
        a = board.find_by_id("ppp")
        assert a is not None
        a.status = "done"
        board.completed.append(a)
        board.active.remove(a)
        save_board(board)

        board2 = build_board()
        assert board2.active == []
        assert len(board2.completed) == 1
        assert board2.completed[0].assignment_id == "ppp"
        assert board2.completed[0].status == "done"

    def test_empty_db_gives_empty_board(self, coord_db) -> None:
        board = build_board()
        assert board.active == []
        assert board.completed == []

    def test_mixed_active_and_completed(self, coord_db) -> None:
        from coord.state import record_dispatched
        from coord.models import Proposal, Board
        for i, (aid, machine, repo) in enumerate([
            ("x1", "laptop", "api"),
            ("x2", "server", "shared"),
        ]):
            p = Proposal(
                id=i + 1, machine_name=machine, repo_name=repo,
                issue_number=i + 1, issue_title=chr(65 + i),
                rationale="", files_likely=[], briefing="",
            )
            record_dispatched(
                assignment_id=aid, proposal=p,
                repo_github=f"acme/{repo}",
            )

        # Mark x1 as done
        board = build_board()
        a = board.find_by_id("x1")
        assert a is not None
        a.status = "done"
        board.completed.append(a)
        board.active.remove(a)
        save_board(board)

        board2 = build_board()
        assert len(board2.active) == 1
        assert board2.active[0].assignment_id == "x2"
        assert len(board2.completed) == 1
        assert board2.completed[0].assignment_id == "x1"


# ── Reconciliation ─────────────────────────────────────────────────────────────


class TestReconcile:
    @pytest.fixture
    def board_with_active(self) -> Board:
        return Board(
            active=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=10,
                    issue_title="Fix auth",
                    assignment_id="aaa",
                    status="running",
                ),
                Assignment(
                    machine_name="server",
                    repo_name="shared",
                    issue_number=5,
                    issue_title="Add logging",
                    assignment_id="bbb",
                    status="running",
                ),
            ],
            machines=[
                Machine(name="laptop", host="laptop.tailnet"),
                Machine(name="server", host="server.tailnet"),
            ],
        )

    @pytest.fixture
    def config(self) -> "Config":
        from coord.config import Config
        return Config(
            repos=[
                Repo(name="api", github="acme/api"),
                Repo(name="shared", github="acme/shared"),
            ],
            machines=[
                Machine(name="laptop", host="laptop.tailnet"),
                Machine(name="server", host="server.tailnet"),
            ],
        )

    @patch("coord.reconcile._query_agent")
    def test_completed_assignments_move_to_completed(
        self, mock_query: MagicMock, board_with_active: Board, config,
    ) -> None:
        from coord.reconcile import reconcile

        def agent_status(host, **kw):
            if "laptop" in host:
                return {
                    "active": [],
                    "completed": [{"id": "aaa", "status": "done", "finished_at": 999.0}],
                }
            return {"active": [{"id": "bbb"}], "completed": []}

        mock_query.side_effect = agent_status

        changed = reconcile(board_with_active, config)
        assert changed == ["aaa"]
        assert len(board_with_active.active) == 1
        assert board_with_active.active[0].assignment_id == "bbb"
        assert len(board_with_active.completed) == 1
        assert board_with_active.completed[0].assignment_id == "aaa"
        assert board_with_active.completed[0].status == "done"
        assert board_with_active.completed[0].finished_at == 999.0

    @patch("coord.reconcile._query_agent")
    def test_failed_assignment_reconciled(
        self, mock_query: MagicMock, board_with_active: Board, config,
    ) -> None:
        from coord.reconcile import reconcile

        mock_query.return_value = {
            "active": [],
            "completed": [
                {"id": "aaa", "status": "failed", "finished_at": 888.0},
                {"id": "bbb", "status": "done", "finished_at": 999.0},
            ],
        }
        changed = reconcile(board_with_active, config)
        assert set(changed) == {"aaa", "bbb"}
        assert len(board_with_active.active) == 0
        assert len(board_with_active.completed) == 2
        failed = board_with_active.find_by_id("aaa")
        assert failed.status == "failed"
        done = board_with_active.find_by_id("bbb")
        assert done.status == "done"

    @patch("coord.reconcile._query_agent")
    def test_offline_agent_skipped(
        self, mock_query: MagicMock, board_with_active: Board, config,
    ) -> None:
        from coord.reconcile import reconcile

        mock_query.return_value = None
        changed = reconcile(board_with_active, config)
        assert changed == []
        assert len(board_with_active.active) == 2

    @patch("coord.reconcile._query_agent")
    def test_no_changes_returns_empty(
        self, mock_query: MagicMock, board_with_active: Board, config,
    ) -> None:
        from coord.reconcile import reconcile

        mock_query.return_value = {"active": [{"id": "aaa"}, {"id": "bbb"}], "completed": []}
        changed = reconcile(board_with_active, config)
        assert changed == []
        assert len(board_with_active.active) == 2

    @patch("coord.reconcile._query_agent")
    def test_backfills_branch_on_completed_assignments(
        self, mock_query: MagicMock, config,
    ) -> None:
        """Assignments already in completed (from build_board) get branch backfilled."""
        from coord.reconcile import reconcile

        board = Board(
            completed=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=10,
                    issue_title="Fix auth",
                    assignment_id="aaa",
                    status="done",
                    branch=None,
                ),
            ],
            machines=[Machine(name="laptop", host="laptop.tailnet")],
        )

        mock_query.return_value = {
            "active": [],
            "completed": [
                {"id": "aaa", "status": "done", "branch": "issue-10-fix-auth", "finished_at": 999.0},
            ],
        }

        changed = reconcile(board, config)
        assert "aaa" in changed
        assert board.completed[0].branch == "issue-10-fix-auth"

    @patch("coord.reconcile._query_agent")
    def test_skips_backfill_when_branch_already_set(
        self, mock_query: MagicMock, config,
    ) -> None:
        from coord.reconcile import reconcile

        board = Board(
            completed=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=10,
                    issue_title="Fix auth",
                    assignment_id="aaa",
                    status="done",
                    branch="already-set",
                ),
            ],
            machines=[Machine(name="laptop", host="laptop.tailnet")],
        )

        mock_query.return_value = {
            "active": [],
            "completed": [
                {"id": "aaa", "status": "done", "branch": "different-branch"},
            ],
        }

        changed = reconcile(board, config)
        assert changed == []
        assert board.completed[0].branch == "already-set"


# ── Board GC ───────────────────────────────────────────────────────────────────


class TestBoardGC:
    def test_gc_keeps_recent_assignments(self) -> None:
        board = Board(completed=[
            Assignment(
                machine_name="m", repo_name="r", issue_number=i,
                issue_title=f"t{i}", status="done", finished_at=float(i),
            )
            for i in range(10)
        ])
        removed = board.gc(keep=10)
        assert removed == 0
        assert len(board.completed) == 10

    def test_gc_prunes_oldest(self) -> None:
        board = Board(completed=[
            Assignment(
                machine_name="m", repo_name="r", issue_number=i,
                issue_title=f"t{i}", status="done", finished_at=float(i),
            )
            for i in range(60)
        ])
        removed = board.gc(keep=50)
        assert removed == 10
        assert len(board.completed) == 50
        assert board.completed[0].finished_at == 10.0

    def test_gc_noop_when_under_limit(self) -> None:
        board = Board(completed=[
            Assignment(
                machine_name="m", repo_name="r", issue_number=1,
                issue_title="t", status="done", finished_at=1.0,
            ),
        ])
        assert board.gc(keep=50) == 0

    def test_gc_prunes_in_memory_but_db_retains_all(self, coord_db) -> None:
        """gc() removes old assignments from the in-memory board, but
        save_board() must NOT delete them from the DB.  The assignments table
        is append-only; DB rows are never deleted as a side-effect of saving
        a partial snapshot."""
        board = Board(completed=[
            Assignment(
                machine_name="m", repo_name="r", issue_number=i,
                issue_title=f"t{i}", status="done", finished_at=float(i),
                assignment_id=f"a{i:03d}",
            )
            for i in range(60)
        ])
        save_board(board)

        removed = board.gc(keep=50)
        assert removed == 10
        assert len(board.completed) == 50

        # After saving the pruned board, DB still has all 60 rows.
        save_board(board)
        loaded = load_board()
        assert loaded is not None
        assert len(loaded.completed) == 60, (
            f"Expected 60 completed in DB after gc+save (append-only), "
            f"got {len(loaded.completed)}"
        )

    def test_partial_board_save_does_not_delete_other_assignments(
        self, coord_db
    ) -> None:
        """save_board() with a partial board snapshot must NOT delete assignments
        that are present in the DB but absent from the snapshot.

        Regression for: freshly dispatched reviews vanishing because coord status
        loaded only recent assignments and then called save_board()."""
        a1 = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="First", assignment_id="aaa", status="running",
        )
        a2 = Assignment(
            machine_name="server", repo_name="shared", issue_number=2,
            issue_title="Second", assignment_id="bbb", status="running",
        )
        # Save both assignments to the DB.
        save_board(Board(active=[a1, a2]))

        # Now save a board containing only a1 (simulating a partial snapshot,
        # e.g. coord status loaded only recent items).
        save_board(Board(active=[a1]))

        loaded = load_board()
        assert loaded is not None
        ids = {a.assignment_id for a in loaded.active + loaded.completed}
        assert "aaa" in ids, "a1 should still be in DB"
        assert "bbb" in ids, "a2 should still be in DB — partial save must not delete it"


# ── Board model id-based methods ───────────────────────────────────────────────


class TestBoardIdMethods:
    def test_find_by_id_in_active(self) -> None:
        a = Assignment(machine_name="m", repo_name="r", issue_number=1,
                       issue_title="t", assignment_id="abc", status="running")
        board = Board(active=[a])
        assert board.find_by_id("abc") is a
        assert board.find_by_id("nope") is None

    def test_find_by_id_in_completed(self) -> None:
        a = Assignment(machine_name="m", repo_name="r", issue_number=1,
                       issue_title="t", assignment_id="xyz", status="done")
        board = Board(completed=[a])
        assert board.find_by_id("xyz") is a

    def test_mark_done_by_id(self) -> None:
        a = Assignment(machine_name="m", repo_name="r", issue_number=1,
                       issue_title="t", assignment_id="abc", status="running")
        board = Board(active=[a])
        result = board.mark_done_by_id("abc", branch="feat/x", finished_at=100.0)
        assert result is a
        assert a.status == "done"
        assert a.branch == "feat/x"
        assert a.finished_at == 100.0
        assert board.active == []
        assert board.completed == [a]

    def test_mark_done_by_id_unknown(self) -> None:
        board = Board()
        assert board.mark_done_by_id("nope") is None

    def test_mark_failed_by_id(self) -> None:
        a = Assignment(machine_name="m", repo_name="r", issue_number=1,
                       issue_title="t", assignment_id="abc", status="running")
        board = Board(active=[a])
        result = board.mark_failed_by_id("abc", finished_at=200.0)
        assert result is a
        assert a.status == "failed"
        assert a.finished_at == 200.0
        assert board.active == []
        assert board.completed == [a]


# ── CLI resume command ─────────────────────────────────────────────────────────


class TestResumeCommand:
    def test_resume_no_board_rebuilds(self, coord_db) -> None:
        from coord.cli import main

        config_file_content = (
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        runner = CliRunner()
        import tempfile, os
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False
        ) as f:
            f.write(config_file_content)
            config_file = f.name
        try:
            result = runner.invoke(main, ["resume", "--config", config_file])
        finally:
            os.unlink(config_file)

        assert result.exit_code == 0
        assert "Rebuilding from dispatched ledger" in result.output
        assert "Board saved" in result.output

    def test_resume_loads_existing_board(self, coord_db) -> None:
        from coord.cli import main

        config_file_content = (
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        board = Board(round_number=5, completed=[
            Assignment(machine_name="m", repo_name="api", issue_number=1,
                       issue_title="t", assignment_id="old", status="done",
                       finished_at=1.0),
        ])
        save_board(board)

        runner = CliRunner()
        import tempfile, os
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yml", delete=False
        ) as f:
            f.write(config_file_content)
            config_file = f.name
        try:
            result = runner.invoke(main, ["resume", "--config", config_file])
        finally:
            os.unlink(config_file)

        assert result.exit_code == 0
        assert "Board round: 5" in result.output
        assert "completed: 1" in result.output


# ── _save_config_snapshot ──────────────────────────────────────────────────────


class TestSaveConfigSnapshot:
    """_save_config_snapshot() populates the machines table in the DB."""

    def test_populates_machines_table(self, coord_db) -> None:
        from coord.cli import _save_config_snapshot
        from coord.config import Config
        from coord.models import Machine, Repo

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(name="laptop", host="laptop.tailnet",
                        capabilities=["python"], repos=["api"]),
                Machine(name="server", host="server.tailnet",
                        capabilities=["python", "docker"], repos=["api"]),
            ],
        )
        _save_config_snapshot(cfg)

        import json as _json
        rows = coord_db.execute("SELECT * FROM machines ORDER BY name").fetchall()
        assert len(rows) == 2
        names = [r["name"] for r in rows]
        assert "laptop" in names
        assert "server" in names

        laptop = next(r for r in rows if r["name"] == "laptop")
        assert laptop["host"] == "laptop.tailnet"
        assert _json.loads(laptop["capabilities"]) == ["python"]
        assert _json.loads(laptop["repos"]) == ["api"]

        server = next(r for r in rows if r["name"] == "server")
        assert _json.loads(server["capabilities"]) == ["python", "docker"]

    def test_replaces_existing_machines(self, coord_db) -> None:
        """Calling _save_config_snapshot twice overwrites the first set."""
        from coord.cli import _save_config_snapshot
        from coord.config import Config
        from coord.models import Machine, Repo

        cfg1 = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="old", host="old.tailnet", repos=["api"])],
        )
        _save_config_snapshot(cfg1)

        cfg2 = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="new", host="new.tailnet", repos=["api"])],
        )
        _save_config_snapshot(cfg2)

        rows = coord_db.execute("SELECT name FROM machines ORDER BY name").fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "new"

    def test_writes_pipeline_require_plan_from_dispatch_flag(self, coord_db) -> None:
        """pipeline_require_plan in board_meta reflects dispatch.require_plan."""
        from coord.cli import _save_config_snapshot
        from coord.config import Config, DispatchConfig
        from coord.models import Machine, Repo

        cfg_on = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="m1", host="m1.tailnet", repos=["api"])],
            dispatch=DispatchConfig(require_plan=True),
        )
        _save_config_snapshot(cfg_on)
        row = coord_db.execute(
            "SELECT value FROM board_meta WHERE key = 'pipeline_require_plan'"
        ).fetchone()
        assert row is not None
        assert row["value"] == "1"

        cfg_off = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="m1", host="m1.tailnet", repos=["api"])],
            dispatch=DispatchConfig(require_plan=False),
        )
        _save_config_snapshot(cfg_off)
        row = coord_db.execute(
            "SELECT value FROM board_meta WHERE key = 'pipeline_require_plan'"
        ).fetchone()
        assert row is not None
        assert row["value"] == "0"

    def test_writes_pipeline_acceptance_routes_for_routed_repos_only(self, coord_db) -> None:
        """#1151: board_meta['pipeline_acceptance_routes'] carries repo ->
        route `match` globs, but ONLY for repos whose acceptance driver is
        routed (a non-empty `routes` list, #1125). A repo with a flat
        (unrouted) driver, or no driver at all, must be omitted entirely —
        the TUI's `acceptance_for_path_arg` (tui/src/app/pipeline.rs) treats
        an absent key as "no --for-path needed", so a routed repo that's
        wrongly included as e.g. an empty list would silently regress to
        the pre-#1151 unconditional-dispatch bug.
        """
        from coord.cli import _save_config_snapshot
        from coord.config import (
            AcceptanceConfig,
            AcceptanceDriverConfig,
            Config,
        )
        from coord.models import Machine, Repo

        cfg = Config(
            repos=[
                Repo(name="routed-repo", github="acme/routed-repo"),
                Repo(name="flat-repo", github="acme/flat-repo"),
                Repo(name="no-driver-repo", github="acme/no-driver-repo"),
            ],
            machines=[Machine(name="m1", host="m1.tailnet",
                               repos=["routed-repo", "flat-repo", "no-driver-repo"])],
            acceptance=AcceptanceConfig(drivers={
                "routed-repo": AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(match="coord/**", kind="cli-pytest"),
                    AcceptanceDriverConfig(match="tui/**", kind="tui-tuidriver"),
                ]),
                "flat-repo": AcceptanceDriverConfig(kind="cli-pytest", run="pytest"),
            }),
        )
        _save_config_snapshot(cfg)

        import json as _json
        row = coord_db.execute(
            "SELECT value FROM board_meta WHERE key = 'pipeline_acceptance_routes'"
        ).fetchone()
        assert row is not None
        routes = _json.loads(row["value"])
        assert routes == {"routed-repo": ["coord/**", "tui/**"]}
        assert "flat-repo" not in routes, "an unrouted (flat) driver must be omitted"
        assert "no-driver-repo" not in routes, "a repo with no driver at all must be omitted"


# ── upsert_open_issues ──────────────────────────────────────────────────────

def test_upsert_open_issues_inserts_rows(coord_db) -> None:
    from coord.state import upsert_open_issues
    from coord.db import get_connection

    issues = [
        {"number": 1, "title": "Fix login", "body": "Broken", "labels": [{"name": "bug"}]},
        {"number": 2, "title": "Add tests", "body": "", "labels": []},
    ]
    upsert_open_issues("myrepo", issues)

    rows = get_connection().execute(
        "SELECT number, title, state, labels FROM issues WHERE repo_name='myrepo' ORDER BY number"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["number"] == 1
    assert rows[0]["title"] == "Fix login"
    assert rows[0]["state"] == "open"
    assert rows[1]["number"] == 2


def test_upsert_open_issues_marks_removed_issues_closed(coord_db) -> None:
    from coord.state import upsert_open_issues
    from coord.db import get_connection

    upsert_open_issues("repo", [{"number": 1, "title": "A", "body": "", "labels": []}])
    upsert_open_issues("repo", [{"number": 2, "title": "B", "body": "", "labels": []}])

    rows = get_connection().execute(
        "SELECT number, state FROM issues WHERE repo_name='repo' ORDER BY number"
    ).fetchall()
    assert rows[0]["number"] == 1
    assert rows[0]["state"] == "closed"   # was open, now absent from latest sync
    assert rows[1]["number"] == 2
    assert rows[1]["state"] == "open"


def test_upsert_open_issues_stamps_synced_at_when_issue_closes(coord_db) -> None:
    """#771 review: the retention clock for a closed issue must start at
    close-detection time, not freeze at whenever it was last confirmed open.

    Previously the close-marking UPDATE didn't touch ``synced_at``, so an
    issue that had gone e.g. 6 days without a resync-worthy change (no title/
    label/milestone edit) inherited that stale timestamp the moment it
    closed — leaving only ~1 of the intended 7 days of grace before the
    local-cache prune below deletes it, instead of a full 7 days from
    closure.
    """
    import time

    from coord.db import get_connection
    from coord.state import upsert_open_issues

    conn = get_connection()
    old_ts = time.time() - 6 * 86400  # last confirmed open 6 days ago
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES ('repo', 42, 'Old', '', 'open', '[]', ?)",
        (old_ts,),
    )
    conn.commit()

    # Next sync: #42 is no longer in the fetched open list → transitions closed.
    upsert_open_issues("repo", [])

    row = get_connection().execute(
        "SELECT state, synced_at FROM issues WHERE repo_name='repo' AND number=42"
    ).fetchone()
    assert row["state"] == "closed"
    assert row["synced_at"] > old_ts + 86400, (
        "synced_at must be refreshed to ~now on the open->closed transition, "
        "not left at the stale last-confirmed-open timestamp"
    )


def test_upsert_open_issues_does_not_reset_synced_at_for_already_closed(coord_db) -> None:
    """The close-time stamp only applies to the open->closed transition —
    an already-closed row's clock must keep counting from when *it* closed,
    so the 7-day prune still reclaims it on schedule instead of the clock
    resetting on every subsequent sync that finds it still absent."""
    import time

    from coord.db import get_connection
    from coord.state import upsert_open_issues

    conn = get_connection()
    old_ts = time.time() - 8 * 86400  # closed 8 days ago — already past the window
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at) "
        "VALUES ('repo', 43, 'Old', '', 'closed', '[]', ?)",
        (old_ts,),
    )
    conn.commit()

    # #43 stays absent from the open list on this sync too.
    upsert_open_issues("repo", [])

    row = get_connection().execute(
        "SELECT synced_at FROM issues WHERE repo_name='repo' AND number=43"
    ).fetchone()
    assert row is None, "an already-closed row past the 7-day window must still be pruned"


def test_upsert_open_issues_updates_title_on_resync(coord_db) -> None:
    from coord.state import upsert_open_issues
    from coord.db import get_connection

    upsert_open_issues("repo", [{"number": 5, "title": "Old title", "body": "", "labels": []}])
    upsert_open_issues("repo", [{"number": 5, "title": "New title", "body": "", "labels": []}])

    row = get_connection().execute(
        "SELECT title FROM issues WHERE repo_name='repo' AND number=5"
    ).fetchone()
    assert row["title"] == "New title"


def test_upsert_open_issues_updates_labels_on_resync(coord_db) -> None:
    """#658 regression: coord sync must overwrite stale labels for existing rows.

    coord backlog removes a status:* label from GitHub.  The subsequent
    coord sync must write the updated label set into the local issues cache
    so the TUI pipeline view stops showing the old status label.
    """
    import json
    from coord.state import upsert_open_issues
    from coord.db import get_connection

    # First sync: issue arrives with status:ready label.
    upsert_open_issues(
        "repo",
        [{"number": 6, "title": "T", "body": "", "labels": [{"name": "coord"}, {"name": "status:ready"}]}],
    )
    row = get_connection().execute(
        "SELECT labels FROM issues WHERE repo_name='repo' AND number=6"
    ).fetchone()
    assert "status:ready" in json.loads(row["labels"])

    # coord backlog removes status:ready on GitHub.  Second sync reflects that.
    upsert_open_issues(
        "repo",
        [{"number": 6, "title": "T", "body": "", "labels": [{"name": "coord"}]}],
    )
    row = get_connection().execute(
        "SELECT labels FROM issues WHERE repo_name='repo' AND number=6"
    ).fetchone()
    labels = json.loads(row["labels"])
    assert "status:ready" not in labels, (
        "coord sync did not remove the stale status:ready label from the issues cache"
    )
    assert "coord" in labels


def test_upsert_open_issues_persists_milestone(coord_db) -> None:
    """#406 Phase A: milestone_number + milestone_title survive a coord sync."""
    from coord.state import upsert_open_issues
    from coord.db import get_connection

    issues = [
        {
            "number": 10,
            "title": "Milestone issue",
            "body": "",
            "labels": [],
            "milestone": {"number": 5, "title": "v0.5"},
        },
        {
            "number": 11,
            "title": "No-milestone issue",
            "body": "",
            "labels": [],
            "milestone": None,
        },
    ]
    upsert_open_issues("repo", issues)

    rows = get_connection().execute(
        "SELECT number, milestone_number, milestone_title FROM issues "
        "WHERE repo_name='repo' ORDER BY number"
    ).fetchall()
    assert rows[0]["number"] == 10
    assert rows[0]["milestone_number"] == 5
    assert rows[0]["milestone_title"] == "v0.5"
    assert rows[1]["number"] == 11
    assert rows[1]["milestone_number"] is None
    assert rows[1]["milestone_title"] is None


def test_upsert_open_issues_clears_milestone_on_resync(coord_db) -> None:
    """#406 Phase A: milestone is cleared when re-synced without one."""
    from coord.state import upsert_open_issues
    from coord.db import get_connection

    # First sync: issue has milestone.
    upsert_open_issues(
        "repo",
        [{"number": 20, "title": "T", "body": "", "labels": [],
          "milestone": {"number": 3, "title": "v0.3"}}],
    )
    # Second sync: milestone removed.
    upsert_open_issues(
        "repo",
        [{"number": 20, "title": "T", "body": "", "labels": [], "milestone": None}],
    )

    row = get_connection().execute(
        "SELECT milestone_number, milestone_title FROM issues "
        "WHERE repo_name='repo' AND number=20"
    ).fetchone()
    assert row["milestone_number"] is None
    assert row["milestone_title"] is None


# ── update_issue_labels (#266 follow-up) ────────────────────────────────────

def test_update_issue_labels_writes_to_existing_row(coord_db) -> None:
    """The TUI's right-click label actions write straight to the local
    issues table after gh edit succeeds — without this, the TUI's 5s
    auto-refresh shows stale labels until the throttled `coord sync`
    runs (every 5 min)."""
    import json
    from coord.state import upsert_open_issues, update_issue_labels
    from coord.db import get_connection

    upsert_open_issues(
        "repo",
        [{"number": 7, "title": "T", "body": "", "labels": [{"name": "coord"}]}],
    )

    updated = update_issue_labels("repo", 7, ["coord", "status:refining"])
    assert updated is True

    row = get_connection().execute(
        "SELECT labels FROM issues WHERE repo_name='repo' AND number=7"
    ).fetchone()
    labels = json.loads(row["labels"])
    assert labels == ["coord", "status:refining"]


def test_update_issue_labels_no_row_returns_false(coord_db) -> None:
    """When the issue isn't in the cache yet (e.g. brain hasn't synced
    this repo), update returns False — the row will be inserted by the
    next `coord sync` so this is not an error."""
    from coord.state import update_issue_labels

    updated = update_issue_labels("repo", 999, ["coord"])
    assert updated is False


def test_update_issue_labels_dedups_and_sorts(coord_db) -> None:
    """Labels are normalised on write (sorted, deduplicated) so the
    classifier sees a canonical set — protects against accidental
    duplicates from upstream callers."""
    import json
    from coord.state import upsert_open_issues, update_issue_labels
    from coord.db import get_connection

    upsert_open_issues("repo", [{"number": 8, "title": "T", "body": "", "labels": []}])
    update_issue_labels("repo", 8, ["zeta", "alpha", "alpha", "beta"])

    row = get_connection().execute(
        "SELECT labels FROM issues WHERE repo_name='repo' AND number=8"
    ).fetchone()
    assert json.loads(row["labels"]) == ["alpha", "beta", "zeta"]


# ── #208: cost_usd column + update_assignment_cost ──────────────────────────


def test_update_assignment_cost_sets_value_when_null(coord_db) -> None:
    """First-time capture: cost_usd is null, the helper sets it."""
    from coord.db import get_connection
    from coord.state import update_assignment_cost
    a = Assignment(
        machine_name="m", repo_name="r", issue_number=1, issue_title="t",
        briefing="b", assignment_id="cost1", status="done",
        dispatched_at=10.0, finished_at=20.0,
    )
    save_board(Board(completed=[a]))
    update_assignment_cost("cost1", 0.42)

    row = get_connection().execute(
        "SELECT cost_usd FROM assignments WHERE assignment_id='cost1'"
    ).fetchone()
    assert row["cost_usd"] == 0.42


def test_update_assignment_cost_keeps_higher_value(coord_db) -> None:
    """Subsequent updates only overwrite when the new value is larger.

    Guards against an agent that lost its session state and reports a
    lower live `cost_so_far` than the finalised log-parsed total.
    """
    from coord.db import get_connection
    from coord.state import update_assignment_cost
    a = Assignment(
        machine_name="m", repo_name="r", issue_number=1, issue_title="t",
        briefing="b", assignment_id="cost2", status="done",
        cost_usd=0.50,
    )
    save_board(Board(completed=[a]))
    update_assignment_cost("cost2", 0.30)  # lower → ignored

    row = get_connection().execute(
        "SELECT cost_usd FROM assignments WHERE assignment_id='cost2'"
    ).fetchone()
    assert row["cost_usd"] == 0.50

    update_assignment_cost("cost2", 0.75)  # higher → applied

    row = get_connection().execute(
        "SELECT cost_usd FROM assignments WHERE assignment_id='cost2'"
    ).fetchone()
    assert row["cost_usd"] == 0.75


def test_update_assignment_cost_unknown_id_is_silent_noop(coord_db) -> None:
    """The helper doesn't raise when the assignment doesn't exist —
    callers shouldn't have to coordinate row existence with cost capture."""
    from coord.db import get_connection
    from coord.state import update_assignment_cost
    # No save_board, no row exists.
    update_assignment_cost("ghost", 1.23)  # must not raise

    row = get_connection().execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE assignment_id='ghost'"
    ).fetchone()
    assert row["n"] == 0


# ── #546: token columns + update_assignment_tokens ──────────────────────────


def test_update_assignment_tokens_sets_values_when_zero(coord_db) -> None:
    """First-time capture: all token columns are 0, the helper writes them."""
    from coord.db import get_connection
    from coord.state import update_assignment_tokens
    a = Assignment(
        machine_name="m", repo_name="r", issue_number=1, issue_title="t",
        briefing="b", assignment_id="tok1", status="done",
        dispatched_at=10.0, finished_at=20.0,
    )
    save_board(Board(completed=[a]))
    update_assignment_tokens("tok1", input_tokens=1000, output_tokens=200,
                             cache_creation_tokens=50, cache_read_tokens=300)

    row = get_connection().execute(
        "SELECT input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens "
        "FROM assignments WHERE assignment_id='tok1'"
    ).fetchone()
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 200
    assert row["cache_creation_tokens"] == 50
    assert row["cache_read_tokens"] == 300


def test_update_assignment_tokens_does_not_overwrite_existing(coord_db) -> None:
    """Idempotent: the UPDATE only fires when input_tokens is still 0 —
    a second call with different values is silently ignored (first writer wins)."""
    from coord.db import get_connection
    from coord.state import update_assignment_tokens
    a = Assignment(
        machine_name="m", repo_name="r", issue_number=1, issue_title="t",
        briefing="b", assignment_id="tok2", status="done",
    )
    save_board(Board(completed=[a]))
    # Write the initial token values directly via the helper (simulating first capture).
    update_assignment_tokens("tok2", input_tokens=500, output_tokens=100)
    # A second call with different values must be a no-op (first writer wins).
    update_assignment_tokens("tok2", input_tokens=9999, output_tokens=8888)

    row = get_connection().execute(
        "SELECT input_tokens, output_tokens FROM assignments WHERE assignment_id='tok2'"
    ).fetchone()
    assert row["input_tokens"] == 500
    assert row["output_tokens"] == 100


def test_update_assignment_tokens_unknown_id_is_silent_noop(coord_db) -> None:
    """The helper doesn't raise when the assignment row doesn't exist —
    callers shouldn't have to coordinate row existence with token capture."""
    from coord.db import get_connection
    from coord.state import update_assignment_tokens
    # No save_board, no row exists.
    update_assignment_tokens("ghost-tok", input_tokens=1000, output_tokens=200)  # must not raise

    row = get_connection().execute(
        "SELECT COUNT(*) AS n FROM assignments WHERE assignment_id='ghost-tok'"
    ).fetchone()
    assert row["n"] == 0


def test_assignment_save_load_roundtrips_cost_usd(coord_db) -> None:
    """Assignment.cost_usd survives a save/load cycle through the upsert
    + ORM mapping.  This is the basic "the column is plumbed correctly"
    smoke test."""
    a = Assignment(
        machine_name="m", repo_name="r", issue_number=1, issue_title="t",
        briefing="b", assignment_id="rt1", status="done",
        cost_usd=1.23,
    )
    save_board(Board(completed=[a]))
    board = load_board()
    assert board.completed[0].cost_usd == 1.23


# ── #252: smoke_tests column + update_assignment_smoke_tests ────────────────


def test_assignment_save_load_roundtrips_smoke_tests(coord_db) -> None:
    """Assignment.smoke_tests survives the upsert + ORM mapping —
    distinguish None / empty list / populated list cleanly."""
    a = Assignment(
        machine_name="m", repo_name="r", issue_number=1, issue_title="t",
        briefing="b", assignment_id="sm1", status="done",
        smoke_tests=["one — trigger — outcome", "two — trigger — outcome"],
    )
    save_board(Board(completed=[a]))
    board = load_board()
    assert board.completed[0].smoke_tests == [
        "one — trigger — outcome",
        "two — trigger — outcome",
    ]


def test_smoke_tests_empty_list_distinguished_from_none(coord_db) -> None:
    """[] (worker said "change is internal") must NOT collapse to None
    on roundtrip — the TUI uses the distinction to render different
    messages."""
    a = Assignment(
        machine_name="m", repo_name="r", issue_number=1, issue_title="t",
        briefing="b", assignment_id="sm2", status="done",
        smoke_tests=[],
    )
    save_board(Board(completed=[a]))
    board = load_board()
    # Explicit empty list, not None.
    assert board.completed[0].smoke_tests == []
    assert board.completed[0].smoke_tests is not None


def test_smoke_tests_none_stays_null(coord_db) -> None:
    """Default Assignment with smoke_tests=None (no block was emitted)
    persists as SQL NULL and loads back as None."""
    from coord.db import get_connection
    a = Assignment(
        machine_name="m", repo_name="r", issue_number=1, issue_title="t",
        briefing="b", assignment_id="sm3", status="done",
    )
    save_board(Board(completed=[a]))
    row = get_connection().execute(
        "SELECT smoke_tests FROM assignments WHERE assignment_id='sm3'"
    ).fetchone()
    assert row["smoke_tests"] is None

    board = load_board()
    assert board.completed[0].smoke_tests is None


def test_update_assignment_smoke_tests_persists_list(coord_db) -> None:
    """The notify-side helper writes the JSON-encoded list to the row."""
    from coord.db import get_connection
    from coord.state import update_assignment_smoke_tests
    a = Assignment(
        machine_name="m", repo_name="r", issue_number=1, issue_title="t",
        briefing="b", assignment_id="sm4", status="done",
    )
    save_board(Board(completed=[a]))
    update_assignment_smoke_tests("sm4", ["item one", "item two"])

    row = get_connection().execute(
        "SELECT smoke_tests FROM assignments WHERE assignment_id='sm4'"
    ).fetchone()
    import json as _json
    assert _json.loads(row["smoke_tests"]) == ["item one", "item two"]


def test_update_assignment_smoke_tests_unknown_id_silent_noop(coord_db) -> None:
    from coord.state import update_assignment_smoke_tests
    # Just must not raise.
    update_assignment_smoke_tests("ghost", ["x"])
