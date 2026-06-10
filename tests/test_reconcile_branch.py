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


# ── #448 fix iter 2: cli.py inline reconcile mirrors reconcile.py for advisory ──


def test_cli_status_reconcile_advisory_sets_status_and_review_state(
    tmp_path, coord_db
) -> None:
    """Regression for fix-iter-2 of #448: the inline reconcile inside
    `coord status` must also set status='advisory' and review_state='advisory'
    on advisory entries.

    Without this fix, cli.py's reconcile path silently left advisory work
    assignments as status='done', review_state=None — meaning a follow-up
    `coord notify` would dispatch a spurious review for a branch with zero
    commits (the reviewer gets an empty diff).
    """
    from click.testing import CliRunner

    from coord import state as state_mod
    from coord.cli import main

    # Minimal config: one machine, one repo.
    config_file = tmp_path / "coordinator.yml"
    config_file.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
    )

    # Active assignment that the agent will report as advisory.
    active = Assignment(
        machine_name="laptop", repo_name="api",
        issue_number=42, issue_title="Maybe already done",
        status="running", assignment_id="adv-cli-1",
        type="work",
    )
    state_mod.save_board(Board(active=[active]))

    # Fake the live agent /status to return an advisory completion.
    from coord.network import MachineStatus, StatusResult, ONLINE

    status_data = {
        "active": [],
        "completed": [{
            "id": "adv-cli-1",
            "status": "advisory",
            "finished_at": 100.0,
            "branch": "issue-42-maybe-already-done",
            "zero_commit_reason": "worker exited cleanly but pushed 0 commits",
            "spec": {
                "type": "work",
                "issue_number": 42,
                "issue_title": "Maybe already done",
                "repo_name": "api",
            },
        }],
        "version": "0.0.0",
    }

    fake_machine_status = MachineStatus(
        machine=Machine(name="laptop", host="laptop.tail", repos=["api"]),
        state=ONLINE,
        latency_ms=1.0,
    )

    with patch("coord.network.check_all", return_value=[fake_machine_status]), \
         patch("coord.network.fetch_status", return_value=StatusResult(data=status_data)):
        result = CliRunner().invoke(
            main, ["status", "--config", str(config_file), "--timeout", "0.1"],
        )

    assert result.exit_code == 0, result.output

    # Verify the saved board: advisory must be persisted with status="advisory"
    # AND review_state="advisory" — not status="done" + review_state=None,
    # which would trigger a spurious review on the next coord notify.
    board = state_mod.load_board()
    assert board is not None
    assert board.active == [], (
        "advisory should have moved out of active"
    )
    completed = [a for a in board.completed if a.assignment_id == "adv-cli-1"]
    assert len(completed) == 1, (
        f"advisory must be in board.completed (got {len(completed)} matches)"
    )
    done = completed[0]
    assert done.status == "advisory", (
        f"cli.py reconcile must set status='advisory' (got {done.status!r}); "
        "leaving it as 'done' lets _dispatch_board_pending_reviews mistake "
        "an advisory for a normal completion"
    )
    assert done.review_state == "advisory", (
        f"cli.py reconcile must set review_state='advisory' (got "
        f"{done.review_state!r}); leaving it as None lets the notify "
        "review-dispatch loop fire a spurious review for a 0-commit branch"
    )


# ── #448 fix iter 2: conflict-fix advisory must flip merge entry to HUMAN_REQUIRED ──


def _enqueue_pending_merge(parent_id: str = "parent-merge") -> None:
    """Drop a PENDING merge queue entry pointing at *parent_id* into the DB.

    The conflict-fix worker's ``review_of_assignment_id`` references this
    entry; ``_on_conflict_fix_done`` looks it up by that id to flip its
    state to HUMAN_REQUIRED.
    """
    from coord import merge_queue as mq
    mq.save_queue([
        mq.QueuedMerge(
            assignment_id=parent_id,
            repo_name="api",
            repo_github="acme/api",
            branch="issue-42-fix",
            target_branch="main",
            issue_number=42,
            issue_title="t",
            state=mq.CONFLICT,
            error="Merge conflict in foo.py",
        )
    ])


def test_advisory_conflict_fix_marks_merge_entry_human_required(
    config: Config,
) -> None:
    """A conflict-fix worker that finishes with 0 commits did not resolve
    anything — the parent merge queue entry must be flipped to
    HUMAN_REQUIRED so the next ``coord merge`` does not silently retry the
    same broken rebase forever.

    Regression for the elif branch in reconcile.py:
        elif done.type == "conflict-fix":
            _on_conflict_fix_done(done, succeeded=False)
    """
    from coord import merge_queue as mq

    _enqueue_pending_merge(parent_id="parent-merge")

    fix_assignment = Assignment(
        machine_name="laptop", repo_name="api",
        issue_number=42, issue_title="t",
        status="running", assignment_id="cf-1",
        type="conflict-fix",
        review_of_assignment_id="parent-merge",
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[fix_assignment],
    )
    fake_status = {
        "active": [],
        "completed": [{"id": "cf-1", "status": "advisory", "finished_at": 1.0}],
    }

    # Patch the GitHub post so the test doesn't try to reach the network when
    # the failure path emits the HUMAN_REQUIRED comment.
    with patch("coord.reconcile._query_agent", return_value=fake_status), \
         patch("coord.github_ops.post_issue_comment"):
        reconcile(board, config)

    # The fix assignment itself is moved to completed with status="advisory".
    assert board.completed[0].status == "advisory"

    # The parent merge queue entry must be flipped to HUMAN_REQUIRED so the
    # next `coord merge` does not retry the same broken rebase.
    entries = mq.load_queue()
    assert len(entries) == 1
    assert entries[0].state == mq.HUMAN_REQUIRED, (
        f"expected HUMAN_REQUIRED, got {entries[0].state!r} — "
        "reconcile() advisory branch must call _on_conflict_fix_done(succeeded=False) "
        "for conflict-fix workers that pushed 0 commits"
    )
    assert "Manual rebase required" in (entries[0].error or "")


def test_cli_status_reconcile_advisory_conflict_fix_marks_human_required(
    tmp_path, coord_db
) -> None:
    """Regression for fix-iter-2 of #448: the inline reconcile inside
    ``coord status`` must also call ``_on_conflict_fix_done(succeeded=False)``
    when an advisory conflict-fix entry is observed.

    Without this fix, ``coord status`` is the first command to reconcile a
    conflict-fix worker's advisory exit, the parent merge queue entry stays
    PENDING, and the next ``coord merge`` re-tries the same broken rebase
    forever. The full ``reconcile()`` path eventually catches it, but the
    window of inconsistency is the gap the reviewer flagged in iter-2.
    """
    from click.testing import CliRunner

    from coord import merge_queue as mq
    from coord import state as state_mod
    from coord.cli import main

    config_file = tmp_path / "coordinator.yml"
    config_file.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
    )

    # Pre-seed the merge queue with a parent entry the conflict-fix worker
    # references via review_of_assignment_id.
    _enqueue_pending_merge(parent_id="parent-merge")

    active = Assignment(
        machine_name="laptop", repo_name="api",
        issue_number=42, issue_title="Fix conflict",
        status="running", assignment_id="cf-cli-1",
        type="conflict-fix",
        review_of_assignment_id="parent-merge",
    )
    state_mod.save_board(Board(active=[active]))

    from coord.network import MachineStatus, StatusResult, ONLINE

    status_data = {
        "active": [],
        "completed": [{
            "id": "cf-cli-1",
            "status": "advisory",
            "finished_at": 100.0,
            "branch": "issue-42-fix",
            "zero_commit_reason": "worker exited cleanly but pushed 0 commits",
            "spec": {
                "type": "conflict-fix",
                "issue_number": 42,
                "issue_title": "Fix conflict",
                "repo_name": "api",
            },
        }],
        "version": "0.0.0",
    }

    fake_machine_status = MachineStatus(
        machine=Machine(name="laptop", host="laptop.tail", repos=["api"]),
        state=ONLINE,
        latency_ms=1.0,
    )

    with patch("coord.network.check_all", return_value=[fake_machine_status]), \
         patch("coord.network.fetch_status", return_value=StatusResult(data=status_data)), \
         patch("coord.github_ops.post_issue_comment"):
        result = CliRunner().invoke(
            main, ["status", "--config", str(config_file), "--timeout", "0.1"],
        )

    assert result.exit_code == 0, result.output

    # The parent merge queue entry must be HUMAN_REQUIRED after cli.py's
    # inline reconcile observes the advisory conflict-fix exit.
    entries = mq.load_queue()
    assert len(entries) == 1
    assert entries[0].state == mq.HUMAN_REQUIRED, (
        f"expected HUMAN_REQUIRED, got {entries[0].state!r} — "
        "cli.py's inline reconcile must mirror reconcile.py's advisory "
        "branch for type='conflict-fix' workers"
    )
