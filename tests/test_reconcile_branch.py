"""Tests that reconcile propagates the worker branch from agent /status to board."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from coord.config import Config, ReviewsConfig
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
    # Mock dispatch_review so the review-dispatch loop doesn't try real gh calls.
    with patch("coord.reconcile._query_agent", return_value=fake_status), \
         patch("coord.review.dispatch_review", return_value=None):
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


# ── #448: reconcile must not mark advisory as failed ─────────────────────────


def test_advisory_status_moves_to_completed_not_failed(config: Config) -> None:
    """An advisory entry from the agent must be moved to board.completed with
    status='advisory', NOT 'failed'. Bug 1: previously the else-branch called
    mark_failed_by_id on any non-done, non-cancelled entry, which triggered
    auto_reassign loops for 0-commit advisory completions."""
    board = _board()
    fake_status = {
        "active": [],
        "completed": [{"id": "abc", "status": "advisory", "finished_at": 1.0}],
    }
    with patch("coord.reconcile._query_agent", return_value=fake_status):
        changed = reconcile(board, config)

    assert changed == ["abc"]
    # Assignment must have moved from active to completed.
    assert board.active == []
    assert len(board.completed) == 1
    advisory = board.completed[0]
    # Status must be "advisory", not "done" or "failed".
    assert advisory.status == "advisory", (
        f"expected advisory, got {advisory.status!r} — "
        "reconcile() wrongly called mark_failed_by_id on advisory entry"
    )


def test_advisory_work_review_state_is_not_pending(config: Config) -> None:
    """Advisory work assignments must not enter the review-dispatch loop.
    review_state should be set to 'advisory' (not 'pending') so no review
    worker is spawned for a 0-commit branch."""
    board = _board()
    fake_status = {
        "active": [],
        "completed": [{"id": "abc", "status": "advisory", "finished_at": 1.0}],
    }
    dispatch_calls: list[str] = []

    def _fake_dispatch_review(completed, board, config, **kwargs):
        dispatch_calls.append(completed.assignment_id)
        return None

    with patch("coord.reconcile._query_agent", return_value=fake_status), \
         patch("coord.review.dispatch_review", _fake_dispatch_review):
        reconcile(board, config)

    advisory = board.completed[0]
    assert advisory.review_state == "advisory", (
        "advisory work should have review_state='advisory', not 'pending'"
    )
    assert dispatch_calls == [], (
        "dispatch_review must not be called for an advisory work assignment"
    )


def test_advisory_does_not_trigger_auto_reassign(config: Config) -> None:
    """auto_reassign must not fire for advisory assignments (the exact loop
    the issue aimed to prevent)."""
    cfg_with_reassign = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )
    # Monkey-patch auto_reassign onto the concurrency object.
    cfg_with_reassign.concurrency.auto_reassign = True  # type: ignore[attr-defined]

    board = _board()
    fake_status = {
        "active": [],
        "completed": [{"id": "abc", "status": "advisory", "finished_at": 1.0}],
    }
    reassign_calls: list[str] = []

    with patch("coord.reconcile._query_agent", return_value=fake_status), \
         patch("coord.reconcile._reassign", side_effect=reassign_calls.append) as mock_reassign:
        mock_reassign.return_value = None
        reconcile(board, cfg_with_reassign)

    assert reassign_calls == [], (
        "_reassign must not be called for advisory — that would create an infinite loop"
    )


# ── #459: reconcile skips review dispatch when a work assignment is active ──


def test_reconcile_skips_review_when_active_work_for_same_issue(config: Config) -> None:
    """reconcile must NOT dispatch a review for a completed assignment when an
    active work assignment is rewriting the same issue's branch (#459)."""
    # The board already has a completed "work-old" and an active "work-new"
    # for the same (repo, issue). Reconcile should leave review_state as
    # "pending" without calling dispatch_review.
    cfg_with_reviews = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"],
                          repo_paths={"api": "/w"})],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )

    completed_work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=42,
        issue_title="t", status="done", branch="issue-42-fix",
        assignment_id="work-old", type="work", review_state="pending",
        test_state="passed",  # satisfy the default test gate
    )
    active_fix = Assignment(
        machine_name="laptop", repo_name="api", issue_number=42,
        issue_title="t", status="running", branch="issue-42-fix2",
        assignment_id="work-new", type="work",
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        active=[active_fix],
        completed=[completed_work],
    )

    review_dispatches: list[str] = []

    def _fake_dispatch_review(completed, board, config, **kwargs):
        review_dispatches.append(completed.assignment_id)
        return None

    # Agent reports nothing new — we only care about the review-dispatch loop.
    fake_status = {"active": [], "completed": []}
    with patch("coord.reconcile._query_agent", return_value=fake_status), \
         patch("coord.review.dispatch_review", _fake_dispatch_review):
        reconcile(board, cfg_with_reviews)

    assert review_dispatches == [], (
        "dispatch_review must not be called while an active work assignment "
        "is rewriting the same issue's branch"
    )
    # review_state should remain "pending" for the next reconcile pass.
    assert completed_work.review_state == "pending"


def test_reconcile_dispatches_review_when_no_active_work(config: Config) -> None:
    """reconcile should dispatch review when no active work exists for the issue."""
    cfg_with_reviews = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"],
                          repo_paths={"api": "/w"})],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )

    completed_work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=42,
        issue_title="t", status="done", branch="issue-42-fix",
        assignment_id="work-done", type="work", review_state="pending",
        test_state="passed",  # satisfy the default test gate
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        active=[],
        completed=[completed_work],
    )

    review_dispatches: list[str] = []

    def _fake_dispatch_review(completed, board, config, **kwargs):
        review_dispatches.append(completed.assignment_id)
        # Return a fake review Assignment to trigger review_state = "dispatched".
        return Assignment(
            machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="[review] t", status="running",
            assignment_id="rev-new", type="review",
        )

    fake_status = {"active": [], "completed": []}
    with patch("coord.reconcile._query_agent", return_value=fake_status), \
         patch("coord.review.dispatch_review", _fake_dispatch_review):
        reconcile(board, cfg_with_reviews)

    assert review_dispatches == ["work-done"], (
        "dispatch_review should be called when there's no active work for the issue"
    )
    assert completed_work.review_state == "dispatched"
