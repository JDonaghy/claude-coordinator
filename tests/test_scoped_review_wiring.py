"""#1476: wiring tests — reconcile() and `coord notify` must both invoke
coord.review.dispatch_scoped_reviews_for_queue, the same way they already
invoke dispatch_pending_reviews. Without this wiring, a merge entry whose
approval was voided by a content-changing conflict-fix rebase would never
get its scoped re-review dispatched automatically — the pure logic in
coord.merge_queue / coord.review would be correct but unreachable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coord.config import Config, ReviewsConfig
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import reconcile


@pytest.fixture
def repo() -> Repo:
    return Repo(name="api", github="acme/api", default_branch="main")


@pytest.fixture
def config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail", capabilities=["python"],
                repos=["api"], repo_paths={"api": "/work/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )


def test_reconcile_dispatches_scoped_reviews(config: Config) -> None:
    board = Board()
    with patch("coord.review.dispatch_pending_reviews", return_value=[]), \
         patch(
             "coord.review.dispatch_scoped_reviews_for_queue", return_value=[]
         ) as mock_scoped:
        reconcile(board, config)
    mock_scoped.assert_called_once_with(board, config)


def test_reconcile_collects_scoped_review_assignment_ids(config: Config) -> None:
    board = Board()
    scoped_review = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1,
        issue_title="[scoped-review] t", assignment_id="scoped-1", type="review",
    )
    with patch("coord.review.dispatch_pending_reviews", return_value=[]), \
         patch(
             "coord.review.dispatch_scoped_reviews_for_queue",
             return_value=[scoped_review],
         ):
        changed = reconcile(board, config)
    assert "scoped-1" in changed


def test_notify_dispatches_scoped_reviews(config: Config) -> None:
    from coord.notify import _dispatch_board_pending_reviews

    with patch("coord.board_service.read_board", return_value=Board()), \
         patch("coord.board_service.write_board") as mock_write, \
         patch("coord.review.dispatch_pending_reviews", return_value=[]), \
         patch(
             "coord.review.dispatch_scoped_reviews_for_queue"
         ) as mock_scoped:
        mock_scoped.return_value = [
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=1,
                issue_title="[scoped-review] t", assignment_id="scoped-2",
                type="review",
            )
        ]
        _dispatch_board_pending_reviews(config)

    mock_scoped.assert_called_once()
    mock_write.assert_called_once()


def test_notify_does_not_write_board_when_nothing_dispatched(config: Config) -> None:
    from coord.notify import _dispatch_board_pending_reviews

    with patch("coord.board_service.read_board", return_value=Board()), \
         patch("coord.board_service.write_board") as mock_write, \
         patch("coord.review.dispatch_pending_reviews", return_value=[]), \
         patch("coord.review.dispatch_scoped_reviews_for_queue", return_value=[]):
        _dispatch_board_pending_reviews(config)

    mock_write.assert_not_called()
