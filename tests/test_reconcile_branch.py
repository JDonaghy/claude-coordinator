"""Tests that reconcile propagates the worker branch from agent /status to board."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import reconcile


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )


def _board() -> Board:
    a = Assignment(
        machine_name="laptop", repo_name="api",
        issue_number=42, issue_title="t",
        status="running", assignment_id="abc",
    )
    return Board(repos=[Repo(name="api", github="acme/api")], machines=[], active=[a])


def test_done_with_branch_sets_assignment_branch(config: Config) -> None:
    board = _board()
    fake_status = {
        "active": [],
        "completed": [{"id": "abc", "status": "done", "finished_at": 1.0, "branch": "worker/feat"}],
    }
    with patch("coord.reconcile._query_agent", return_value=fake_status):
        changed = reconcile(board, config)
    assert changed == ["abc"]
    done = board.completed[0]
    assert done.branch == "worker/feat"
    assert done.status == "done"


def test_done_without_branch_leaves_assignment_branch_none(config: Config) -> None:
    board = _board()
    fake_status = {
        "active": [],
        "completed": [{"id": "abc", "status": "done", "finished_at": 1.0}],
    }
    with patch("coord.reconcile._query_agent", return_value=fake_status):
        reconcile(board, config)
    assert board.completed[0].branch is None


def test_failed_status_propagates_without_branch(config: Config) -> None:
    board = _board()
    fake_status = {
        "active": [],
        "completed": [{"id": "abc", "status": "failed", "finished_at": 1.0}],
    }
    with patch("coord.reconcile._query_agent", return_value=fake_status):
        reconcile(board, config)
    failed = board.completed[0]
    assert failed.status == "failed"
