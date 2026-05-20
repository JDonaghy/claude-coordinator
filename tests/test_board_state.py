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

    def test_gc_pruned_rows_deleted_from_db(self, coord_db) -> None:
        """Regression: after gc() prunes completed assignments, save+load must
        reflect the pruned count — not the original count."""
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

        save_board(board)
        loaded = load_board()
        assert loaded is not None
        assert len(loaded.completed) == 50, (
            f"Expected 50 completed after gc+save, got {len(loaded.completed)}"
        )


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
