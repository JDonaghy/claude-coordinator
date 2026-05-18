"""Tests for board state persistence, reconstruction, reconciliation, and GC."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from coord.models import Assignment, Board, Machine, Repo
from coord.state import save_board, load_board, build_board


# ── Board save/load roundtrip ──────────────────────────────────────────────


class TestBoardPersistence:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
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
        board_file = tmp_path / "board.json"
        save_board(board, path=board_file)
        loaded = load_board(path=board_file)

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

    def test_load_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_board(path=tmp_path / "nope.json") is None

    def test_load_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        bad = tmp_path / "board.json"
        bad.write_text("not json {{{")
        assert load_board(path=bad) is None

    def test_save_is_atomic(self, tmp_path: Path) -> None:
        board_file = tmp_path / "board.json"
        save_board(Board(round_number=1), path=board_file)
        assert board_file.exists()
        assert not board_file.with_suffix(".json.tmp").exists()

    def test_empty_board_roundtrip(self, tmp_path: Path) -> None:
        board_file = tmp_path / "board.json"
        save_board(Board(), path=board_file)
        loaded = load_board(path=board_file)
        assert loaded is not None
        assert loaded.active == []
        assert loaded.completed == []
        assert loaded.round_number == 0


# ── Build board from dispatched ledger ─────────────────────────────────────


class TestBuildBoard:
    def test_running_assignments_from_dispatched(self, tmp_path: Path) -> None:
        dispatched_file = tmp_path / "dispatched.json"
        notified_file = tmp_path / "notified.json"
        dispatched_file.write_text(json.dumps([
            {
                "assignment_id": "aaa",
                "machine_name": "laptop",
                "repo_name": "api",
                "repo_github": "acme/api",
                "issue_number": 10,
                "issue_title": "Fix auth",
                "files_likely": ["auth.py"],
                "briefing": "fix it",
                "dispatched_at": 1000.0,
            },
        ]))
        notified_file.write_text("{}")

        board = build_board(dispatched_path=dispatched_file, notified_path=notified_file)
        assert len(board.active) == 1
        assert board.active[0].assignment_id == "aaa"
        assert board.active[0].status == "running"
        assert board.active[0].files_allowed == ["auth.py"]
        assert board.completed == []

    def test_completed_assignments_from_notified(self, tmp_path: Path) -> None:
        dispatched_file = tmp_path / "dispatched.json"
        notified_file = tmp_path / "notified.json"
        dispatched_file.write_text(json.dumps([
            {
                "assignment_id": "bbb",
                "machine_name": "server",
                "repo_name": "shared",
                "repo_github": "acme/shared",
                "issue_number": 5,
                "issue_title": "Add logging",
                "files_likely": [],
                "briefing": "add logs",
                "dispatched_at": 900.0,
            },
        ]))
        notified_file.write_text(json.dumps({
            "bbb": {"event": "completion", "posted_at": 950.0},
        }))

        board = build_board(dispatched_path=dispatched_file, notified_path=notified_file)
        assert board.active == []
        assert len(board.completed) == 1
        assert board.completed[0].assignment_id == "bbb"
        assert board.completed[0].status == "done"

    def test_failed_assignment_from_notified(self, tmp_path: Path) -> None:
        dispatched_file = tmp_path / "dispatched.json"
        notified_file = tmp_path / "notified.json"
        dispatched_file.write_text(json.dumps([
            {
                "assignment_id": "ccc",
                "machine_name": "laptop",
                "repo_name": "api",
                "repo_github": "acme/api",
                "issue_number": 7,
                "issue_title": "Broken",
                "files_likely": [],
                "briefing": "try",
                "dispatched_at": 800.0,
            },
        ]))
        notified_file.write_text(json.dumps({
            "ccc": {"event": "failure", "posted_at": 850.0},
        }))

        board = build_board(dispatched_path=dispatched_file, notified_path=notified_file)
        assert board.active == []
        assert board.completed[0].status == "failed"

    def test_empty_ledger_gives_empty_board(self, tmp_path: Path) -> None:
        dispatched_file = tmp_path / "dispatched.json"
        notified_file = tmp_path / "notified.json"
        dispatched_file.write_text("[]")
        notified_file.write_text("{}")
        board = build_board(dispatched_path=dispatched_file, notified_path=notified_file)
        assert board.active == []
        assert board.completed == []

    def test_mixed_active_and_completed(self, tmp_path: Path) -> None:
        dispatched_file = tmp_path / "dispatched.json"
        notified_file = tmp_path / "notified.json"
        dispatched_file.write_text(json.dumps([
            {
                "assignment_id": "x1",
                "machine_name": "laptop",
                "repo_name": "api",
                "repo_github": "acme/api",
                "issue_number": 1,
                "issue_title": "A",
                "files_likely": [],
                "briefing": "",
                "dispatched_at": 100.0,
            },
            {
                "assignment_id": "x2",
                "machine_name": "server",
                "repo_name": "shared",
                "repo_github": "acme/shared",
                "issue_number": 2,
                "issue_title": "B",
                "files_likely": [],
                "briefing": "",
                "dispatched_at": 200.0,
            },
        ]))
        notified_file.write_text(json.dumps({
            "x1": {"event": "completion", "posted_at": 300.0},
        }))

        board = build_board(dispatched_path=dispatched_file, notified_path=notified_file)
        assert len(board.active) == 1
        assert board.active[0].assignment_id == "x2"
        assert len(board.completed) == 1
        assert board.completed[0].assignment_id == "x1"


# ── Reconciliation ─────────────────────────────────────────────────────────


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


# ── Board GC ───────────────────────────────────────────────────────────────


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


# ── Board model id-based methods ───────────────────────────────────────────


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


# ── CLI resume command ─────────────────────────────────────────────────────


class TestResumeCommand:
    def test_resume_no_board_rebuilds(self, tmp_path: Path) -> None:
        from coord.cli import main

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        dispatched_file = tmp_path / "dispatched.json"
        dispatched_file.write_text("[]")
        notified_file = tmp_path / "notified.json"
        notified_file.write_text("{}")
        board_file = tmp_path / "board.json"

        with (
            patch("coord.state.BOARD_FILE", board_file),
            patch("coord.state.DISPATCHED_FILE", dispatched_file),
            patch("coord.state.NOTIFIED_FILE", notified_file),
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["resume", "--config", str(config_file)])

        assert result.exit_code == 0
        assert "Rebuilding from dispatched ledger" in result.output
        assert "Board saved" in result.output

    def test_resume_loads_existing_board(self, tmp_path: Path) -> None:
        from coord.cli import main

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        board_file = tmp_path / "board.json"
        board = Board(round_number=5, completed=[
            Assignment(machine_name="m", repo_name="api", issue_number=1,
                       issue_title="t", assignment_id="old", status="done",
                       finished_at=1.0),
        ])
        save_board(board, path=board_file)

        with patch("coord.state.BOARD_FILE", board_file):
            runner = CliRunner()
            result = runner.invoke(main, ["resume", "--config", str(config_file)])

        assert result.exit_code == 0
        assert "Board round: 5" in result.output
        assert "completed: 1" in result.output
