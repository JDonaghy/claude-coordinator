"""Tests for failure detection, stale assignment handling, retry, and auto-reassign."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import Config, ConcurrencyConfig
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import _reassign, reconcile
from coord.state import save_board


# ── Stale detection ─────────────────────────────────────────────────────────


class TestStaleDetection:
    @patch("coord.reconcile._query_agent")
    def test_unreachable_increments_count(self, mock_query: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"})],
            concurrency=ConcurrencyConfig(stale_threshold=3),
        )
        board = Board(active=[
            Assignment(machine_name="laptop", repo_name="api", issue_number=1,
                       issue_title="x", assignment_id="a1", status="running"),
        ])
        mock_query.return_value = None  # unreachable

        reconcile(board, config)
        assert board.active[0].unreachable_count == 1
        assert board.active[0].status == "running"

    @patch("coord.reconcile._query_agent")
    def test_stale_after_threshold(self, mock_query: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"})],
            concurrency=ConcurrencyConfig(stale_threshold=2),
        )
        a = Assignment(machine_name="laptop", repo_name="api", issue_number=1,
                       issue_title="x", assignment_id="a1", status="running",
                       unreachable_count=1)
        board = Board(active=[a])
        mock_query.return_value = None

        changed = reconcile(board, config)
        assert "a1" in changed
        assert board.active == []
        assert len(board.completed) == 1
        assert board.completed[0].status == "failed"

    @patch("coord.reconcile._query_agent")
    def test_reachable_resets_count(self, mock_query: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"})],
        )
        a = Assignment(machine_name="laptop", repo_name="api", issue_number=1,
                       issue_title="x", assignment_id="a1", status="running",
                       unreachable_count=2)
        board = Board(active=[a])
        mock_query.return_value = {"active": [{"id": "a1"}], "completed": []}

        reconcile(board, config)
        assert board.active[0].unreachable_count == 0


# ── Reassign ────────────────────────────────────────────────────────────────


class TestReassign:
    def test_picks_different_machine(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", assignment_id="a1", status="failed",
            briefing="do the thing",
        )
        board = Board()

        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None

        with patch("coord.reconcile.httpx.post", return_value=mock_resp):
            result = _reassign(failed, board, config)

        assert result is not None
        assert result.machine_name == "server"
        assert result.assignment_id == "retry1"
        assert "[retry]" in result.issue_title
        assert result in board.active

    def test_returns_none_when_no_machine(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[Machine(name="laptop", host="l", repos=["other"])],
        )
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="x", assignment_id="a1", status="failed",
        )
        assert _reassign(failed, Board(), config) is None

    def test_retry_targets_feature_branch_for_opted_in_milestone(self) -> None:
        """#934 review should-fix: _reassign's milestone-aware retry base
        (coord/reconcile.py:392-409) shipped with no test. A repo that
        opted into the git model, retrying an issue that belongs to a
        milestone, must post `branch: feature/ms-NN` to the agent — not
        default_branch — so the retry's own base matches where the
        original work branched from."""
        config = Config(
            repos=[Repo(name="api", github="a/a", default_branch="main",
                        develop_branch="develop")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", assignment_id="a1", status="failed",
            briefing="do the thing",
        )
        board = Board()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None

        with patch("coord.reconcile.httpx.post", return_value=mock_resp) as mock_post, \
             patch("coord.github_ops.get_issue",
                   return_value={"milestone": {"number": 9, "title": "M9"}}):
            result = _reassign(failed, board, config)

        assert result is not None
        posted_payload = mock_post.call_args.kwargs["json"]
        assert posted_payload["branch"] == "feature/ms-9"

    def test_retry_targets_default_branch_when_not_opted_in(self) -> None:
        """No develop_branch configured → default_branch, unchanged behavior."""
        config = Config(
            repos=[Repo(name="api", github="a/a", default_branch="main")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", assignment_id="a1", status="failed",
            briefing="do the thing",
        )
        board = Board()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None

        with patch("coord.reconcile.httpx.post", return_value=mock_resp) as mock_post, \
             patch("coord.github_ops.get_issue") as get_issue:
            result = _reassign(failed, board, config)

        get_issue.assert_not_called()
        assert result is not None
        posted_payload = mock_post.call_args.kwargs["json"]
        assert posted_payload["branch"] == "main"


# ── Auto-reassign from reconcile ────────────────────────────────────────────


class TestAutoReassign:
    @patch("coord.reconcile._query_agent")
    @patch("coord.reconcile.httpx.post")
    def test_auto_reassign_on_failure(self, mock_post: MagicMock, mock_query: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
            concurrency=ConcurrencyConfig(auto_reassign=True),
        )
        board = Board(active=[
            Assignment(machine_name="laptop", repo_name="api", issue_number=42,
                       issue_title="Fix", assignment_id="a1", status="running",
                       type="work", briefing="do it"),
        ])
        mock_query.return_value = {
            "active": [],
            "completed": [{"id": "a1", "status": "failed", "finished_at": 100.0}],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp

        changed = reconcile(board, config)
        assert "a1" in changed
        assert "retry1" in changed
        retry_assignments = [a for a in board.active if "[retry]" in a.issue_title]
        assert len(retry_assignments) == 1

    @patch("coord.reconcile._query_agent")
    def test_no_reassign_when_disabled(self, mock_query: MagicMock) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
            concurrency=ConcurrencyConfig(auto_reassign=False),
        )
        board = Board(active=[
            Assignment(machine_name="laptop", repo_name="api", issue_number=1,
                       issue_title="x", assignment_id="a1", status="running",
                       type="work"),
        ])
        mock_query.return_value = {
            "active": [],
            "completed": [{"id": "a1", "status": "failed", "finished_at": 100.0}],
        }
        changed = reconcile(board, config)
        assert "a1" in changed
        assert len(board.active) == 0


# ── CLI retry command ───────────────────────────────────────────────────────


class TestCoordRetry:
    @patch("coord.reconcile.httpx.post")
    def test_retry_dispatches_to_different_machine(
        self, mock_post: MagicMock, tmp_path: Path, coord_db,
    ) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n"
            "  - name: laptop\n    host: l\n    repos: [api]\n    repo_paths:\n      api: /tmp/a\n"
            "  - name: server\n    host: s\n    repos: [api]\n    repo_paths:\n      api: /tmp/a\n"
        )
        board = Board(completed=[
            Assignment(machine_name="laptop", repo_name="api", issue_number=42,
                       issue_title="Fix auth", assignment_id="a1", status="failed",
                       briefing="do it", finished_at=1.0),
        ])
        save_board(board)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "retry1"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, ["retry", "a1", "--config", str(config_file)])

        assert result.exit_code == 0
        assert "Retried" in result.output
        assert "server" in result.output

    def test_retry_rejects_non_failed(self, tmp_path: Path, coord_db) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        board = Board(active=[
            Assignment(machine_name="m", repo_name="api", issue_number=1,
                       issue_title="x", assignment_id="a1", status="running"),
        ])
        save_board(board)

        runner = CliRunner()
        result = runner.invoke(main, ["retry", "a1", "--config", str(config_file)])
        assert result.exit_code != 0
        assert "not failed" in result.output

    def test_retry_unknown_assignment(self, tmp_path: Path, coord_db) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        save_board(Board())

        runner = CliRunner()
        result = runner.invoke(main, ["retry", "nope", "--config", str(config_file)])
        assert result.exit_code != 0
        assert "not found" in result.output


class TestHelpText:
    def test_retry_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["retry", "--help"])
        assert result.exit_code == 0
        assert "Re-dispatch" in result.output
