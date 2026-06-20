"""Tests for review_state lifecycle: transitions, persistence, and display.

Covers:
- review_state set to 'pending' when work assignment completes in reconcile
- review_state set to 'dispatched' on successful review dispatch
- review_state stays 'pending' when dispatch fails (retry on next reconcile)
- review_state set to 'done' when the review assignment itself completes
- review_state persisted through save/load board cycle
- review_state inferred by build_board from dispatched+notified ledgers
- review_state shown in coord status output
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coord.config import Config, ReviewsConfig
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import reconcile
from coord import state as state_mod
from coord.state import build_board, load_board, save_board


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def repo() -> Repo:
    return Repo(name="api", github="acme/api", default_branch="main")


@pytest.fixture
def two_machine_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tail",
                capabilities=["python"],
                repos=["api"],
                repo_paths={"api": "/work/api"},
            ),
            Machine(
                name="server",
                host="server.tail",
                capabilities=["python"],
                repos=["api"],
                repo_paths={"api": "/srv/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )


@pytest.fixture
def no_review_config(repo: Repo) -> Config:
    """Config with reviews disabled — review dispatch should never fire."""
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tail",
                repos=["api"],
                repo_paths={"api": "/work/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=False, auto_dispatch=False),
    )


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db) -> Path:
    return tmp_path


def _work_assignment(
    *,
    machine: str = "laptop",
    branch: str | None = "issue-1-fix",
    assignment_id: str = "work-001",
    review_state: str | None = None,
    status: str = "running",
    test_state: str | None = "passed",
) -> Assignment:
    # Default test_state="passed" so review-dispatch tests can use the helper
    # without each having to explicitly clear the #200 Test gate. Tests that
    # need to exercise the pre-gate path pass test_state=None explicitly.
    return Assignment(
        machine_name=machine,
        repo_name="api",
        issue_number=1,
        issue_title="Fix the thing",
        assignment_id=assignment_id,
        status=status,
        branch=branch,
        type="work",
        review_state=review_state,
        test_state=test_state,
    )


def _fake_agent_status(
    assignment_id: str, *, status: str = "done", branch: str | None = "issue-1-fix"
) -> dict:
    entry: dict = {"id": assignment_id, "status": status, "finished_at": 100.0}
    if branch:
        entry["branch"] = branch
    return {"active": [], "completed": [entry]}


# ── review_state transition: pending ─────────────────────────────────────────


class TestPendingTransition:
    def test_work_done_sets_review_state_pending(
        self, two_machine_config: Config
    ) -> None:
        """When a work assignment transitions to done in reconcile, review_state → 'pending'."""
        board = Board(
            active=[_work_assignment(status="running", branch=None)],
        )
        fake_status = _fake_agent_status("work-001", branch=None)

        with patch("coord.reconcile._query_agent", return_value=fake_status), \
             patch("coord.review.dispatch_review", return_value=None):
            reconcile(board, two_machine_config)

        completed = board.completed[0]
        assert completed.review_state == "pending"

    def test_non_work_type_stays_none(self, two_machine_config: Config) -> None:
        """Plan and review assignments must NOT get review_state='pending'."""
        plan_a = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=2,
            issue_title="Plan it",
            assignment_id="plan-001",
            status="running",
            type="plan",
        )
        board = Board(active=[plan_a])
        fake = {"active": [], "completed": [{"id": "plan-001", "status": "done", "finished_at": 1.0}]}

        with patch("coord.reconcile._query_agent", return_value=fake), \
             patch("coord.review.dispatch_review", return_value=None):
            reconcile(board, two_machine_config)

        assert board.completed[0].review_state is None

    def test_review_type_stays_none(self, two_machine_config: Config) -> None:
        """review assignments themselves don't get review_state set."""
        review_a = Assignment(
            machine_name="server",
            repo_name="api",
            issue_number=1,
            issue_title="[review] Fix the thing",
            assignment_id="rev-001",
            status="running",
            type="review",
            review_of_assignment_id="work-001",
        )
        board = Board(active=[review_a])
        fake = {"active": [], "completed": [{"id": "rev-001", "status": "done", "finished_at": 1.0}]}

        with patch("coord.reconcile._query_agent", return_value=fake), \
             patch("coord.review.dispatch_review", return_value=None):
            reconcile(board, two_machine_config)

        assert board.completed[0].review_state is None


# ── review_state transition: dispatched ──────────────────────────────────────


class TestDispatchedTransition:
    def _make_review_assignment(self) -> Assignment:
        return Assignment(
            machine_name="server",
            repo_name="api",
            issue_number=1,
            issue_title="[review] Fix the thing",
            assignment_id="rev-001",
            status="running",
            type="review",
            review_of_assignment_id="work-001",
        )

    def test_successful_dispatch_sets_dispatched(
        self, two_machine_config: Config
    ) -> None:
        """When dispatch_review succeeds, review_state → 'dispatched'."""
        completed_work = _work_assignment(
            status="done", branch="issue-1-fix", review_state="pending"
        )
        board = Board(
            active=[],
            completed=[completed_work],
        )
        review_result = self._make_review_assignment()

        with patch("coord.reconcile._query_agent", return_value={"active": [], "completed": []}), \
             patch("coord.review.dispatch_review", return_value=review_result):
            reconcile(board, two_machine_config)

        assert completed_work.review_state == "dispatched"

    def test_failed_dispatch_leaves_pending_for_retry(
        self, two_machine_config: Config
    ) -> None:
        """When dispatch_review returns None (e.g. machine offline), review_state stays 'pending'."""
        completed_work = _work_assignment(
            status="done", branch="issue-1-fix", review_state="pending"
        )
        board = Board(completed=[completed_work])

        with patch("coord.reconcile._query_agent", return_value={"active": [], "completed": []}), \
             patch("coord.review.dispatch_review", return_value=None):
            reconcile(board, two_machine_config)

        # Still pending — retry on next reconcile
        assert completed_work.review_state == "pending"

    def test_pending_state_retried_on_next_reconcile(
        self, two_machine_config: Config
    ) -> None:
        """'pending' assignments are attempted again on subsequent reconcile calls."""
        completed_work = _work_assignment(
            status="done", branch="issue-1-fix", review_state="pending"
        )
        board = Board(completed=[completed_work])
        review_result = Assignment(
            machine_name="server",
            repo_name="api",
            issue_number=1,
            issue_title="[review] Fix the thing",
            assignment_id="rev-002",
            status="running",
            type="review",
        )

        # First reconcile: dispatch fails
        with patch("coord.reconcile._query_agent", return_value={"active": [], "completed": []}), \
             patch("coord.review.dispatch_review", return_value=None):
            reconcile(board, two_machine_config)
        assert completed_work.review_state == "pending"

        # Second reconcile: dispatch succeeds
        with patch("coord.reconcile._query_agent", return_value={"active": [], "completed": []}), \
             patch("coord.review.dispatch_review", return_value=review_result):
            reconcile(board, two_machine_config)
        assert completed_work.review_state == "dispatched"

    def test_already_dispatched_not_re_dispatched(
        self, two_machine_config: Config
    ) -> None:
        """Assignments with review_state='dispatched' are not dispatched again."""
        completed_work = _work_assignment(
            status="done", branch="issue-1-fix", review_state="dispatched"
        )
        board = Board(completed=[completed_work])

        mock_dispatch = MagicMock(return_value=None)
        with patch("coord.reconcile._query_agent", return_value={"active": [], "completed": []}), \
             patch("coord.review.dispatch_review", mock_dispatch):
            reconcile(board, two_machine_config)

        mock_dispatch.assert_not_called()
        assert completed_work.review_state == "dispatched"

    # ── Test-before-Review: review is HELD until a passed/skipped verdict ──

    def test_review_held_when_smoke_missing(
        self, two_machine_config: Config
    ) -> None:
        """Test precedes Review: review must NOT dispatch when test_state is None
        (no smoke verdict yet) — it is held until the work is tested.  (Inverts
        the old #465 behavior, which fired review regardless of smoke.)"""
        completed_work = _work_assignment(
            status="done", branch="issue-1-fix",
            review_state="pending", test_state=None,
        )
        board = Board(completed=[completed_work])
        review_result = self._make_review_assignment()

        with patch("coord.reconcile._query_agent", return_value={"active": [], "completed": []}), \
             patch("coord.review.dispatch_review", return_value=review_result):
            reconcile(board, two_machine_config)

        assert completed_work.review_state in (None, "pending"), (
            "review must be held until the work has a passed/skipped test verdict"
        )

    def test_review_held_when_smoke_failed(
        self, two_machine_config: Config
    ) -> None:
        """Test precedes Review: a FAILED smoke test must NOT dispatch a review —
        the failure routes to a fix, not a PR review."""
        completed_work = _work_assignment(
            status="done", branch="issue-1-fix",
            review_state="pending", test_state="failed",
        )
        board = Board(completed=[completed_work])
        review_result = self._make_review_assignment()

        with patch("coord.reconcile._query_agent", return_value={"active": [], "completed": []}), \
             patch("coord.review.dispatch_review", return_value=review_result):
            reconcile(board, two_machine_config)

        assert completed_work.review_state in (None, "pending"), (
            "a failed smoke test must NOT dispatch a review (it routes to a fix)"
        )

    def test_review_dispatches_when_test_gate_skipped(
        self, two_machine_config: Config
    ) -> None:
        """A skipped Test verdict allows review to dispatch."""
        completed_work = _work_assignment(
            status="done", branch="issue-1-fix",
            review_state="pending", test_state="skipped",
        )
        board = Board(completed=[completed_work])
        review_result = self._make_review_assignment()

        with patch("coord.reconcile._query_agent", return_value={"active": [], "completed": []}), \
             patch("coord.review.dispatch_review", return_value=review_result):
            reconcile(board, two_machine_config)

        assert completed_work.review_state == "dispatched"

    def test_review_dispatches_immediately_when_no_test_gate_configured(
        self, two_machine_config: Config
    ) -> None:
        """When the pipeline has no Test gate, review auto-dispatches on Work
        done as before."""
        # Override the default gates on the config to skip Test.
        two_machine_config.pipeline.default_gates = ["review", "merge"]
        completed_work = _work_assignment(
            status="done", branch="issue-1-fix",
            review_state="pending", test_state=None,
        )
        board = Board(completed=[completed_work])
        review_result = self._make_review_assignment()

        with patch("coord.reconcile._query_agent", return_value={"active": [], "completed": []}), \
             patch("coord.review.dispatch_review", return_value=review_result):
            reconcile(board, two_machine_config)

        assert completed_work.review_state == "dispatched"


# ── review_state transition: done ────────────────────────────────────────────


class TestDoneTransition:
    def test_review_completion_sets_work_review_state_done(
        self, two_machine_config: Config
    ) -> None:
        """When the review assignment completes, the original work assignment's review_state → 'done'."""
        completed_work = _work_assignment(
            status="done", branch="issue-1-fix", review_state="dispatched"
        )
        review_assignment = Assignment(
            machine_name="server",
            repo_name="api",
            issue_number=1,
            issue_title="[review] Fix the thing",
            assignment_id="rev-001",
            status="running",
            type="review",
            review_of_assignment_id="work-001",
        )
        board = Board(
            active=[review_assignment],
            completed=[completed_work],
        )

        # Agent reports the review assignment as done.
        fake_status = {
            "active": [],
            "completed": [{"id": "rev-001", "status": "done", "finished_at": 200.0}],
        }
        with patch("coord.reconcile._query_agent", return_value=fake_status), \
             patch("coord.review.dispatch_review", return_value=None):
            reconcile(board, two_machine_config)

        assert completed_work.review_state == "done"

    def test_review_done_state_not_overwritten_on_next_reconcile(
        self, two_machine_config: Config
    ) -> None:
        """Once 'done', review_state is not reset by subsequent reconcile calls."""
        completed_work = _work_assignment(
            status="done", branch="issue-1-fix", review_state="done"
        )
        board = Board(completed=[completed_work])

        with patch("coord.reconcile._query_agent", return_value={"active": [], "completed": []}), \
             patch("coord.review.dispatch_review", return_value=None):
            reconcile(board, two_machine_config)

        assert completed_work.review_state == "done"


# ── Persistence ───────────────────────────────────────────────────────────────


class TestReviewStatePersistence:
    def test_review_state_serialised_and_reloaded(self, coord_dir: Path) -> None:
        """save_board/load_board round-trips review_state correctly."""
        board = Board(
            completed=[
                _work_assignment(
                    status="done", branch="issue-1-fix", review_state="dispatched"
                ),
            ]
        )
        save_board(board)
        loaded = load_board()
        assert loaded is not None
        assert loaded.completed[0].review_state == "dispatched"

    def test_all_review_states_survive_round_trip(self, coord_dir: Path) -> None:
        """All three non-null review states round-trip through SQLite."""
        for rs in ("pending", "dispatched", "done"):
            board = Board(
                completed=[
                    _work_assignment(
                        status="done",
                        branch="issue-1-fix",
                        assignment_id=f"work-{rs}",
                        review_state=rs,
                    )
                ]
            )
            save_board(board)
            loaded = load_board()
            assert loaded is not None
            a = loaded.find_by_id(f"work-{rs}")
            assert a is not None, f"Assignment work-{rs} not found after save"
            assert a.review_state == rs, f"Failed for review_state={rs!r}"

    def test_old_board_without_review_state_loads_as_none(
        self, coord_dir: Path
    ) -> None:
        """Assignments saved without review_state load as None."""
        board = Board(
            completed=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=1,
                    issue_title="Old assignment",
                    assignment_id="old-001",
                    status="done",
                    type="work",
                    review_state=None,
                )
            ]
        )
        save_board(board)
        loaded = load_board()
        assert loaded is not None
        assert loaded.completed[0].review_state is None


# ── build_board review_state inference ───────────────────────────────────────


class TestBuildBoardReviewState:
    def test_build_board_sets_dispatched_when_review_in_dispatched_ledger(
        self, coord_dir: Path
    ) -> None:
        """build_board sets review_state='dispatched' when a review is in the dispatched ledger but not notified."""
        from coord.models import Proposal
        from coord.state import mark_notified, record_dispatched

        work_proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Fix", rationale="",
        )
        record_dispatched(assignment_id="work-001", proposal=work_proposal, repo_github="acme/api")
        mark_notified("work-001", "completion")

        # Review assignment dispatched but not notified
        review_a = Assignment(
            machine_name="server", repo_name="api", issue_number=1,
            issue_title="[review] Fix", assignment_id="rev-001",
            status="running", type="review", review_of_assignment_id="work-001",
            dispatched_at=2.0,
        )
        from coord.state import record_dispatched_assignment
        record_dispatched_assignment(assignment=review_a, repo_github="acme/api")

        board = build_board()
        work = board.find_by_id("work-001")
        assert work is not None
        assert work.status == "done"
        assert work.review_state == "dispatched"

    def test_build_board_sets_done_when_review_also_notified(
        self, coord_dir: Path
    ) -> None:
        """build_board sets review_state='done' when both work and review are in notified."""
        from coord.models import Proposal
        from coord.state import mark_notified, record_dispatched, record_dispatched_assignment

        work_proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Fix", rationale="",
        )
        record_dispatched(assignment_id="work-001", proposal=work_proposal, repo_github="acme/api")
        mark_notified("work-001", "completion")

        review_a = Assignment(
            machine_name="server", repo_name="api", issue_number=1,
            issue_title="[review] Fix", assignment_id="rev-001",
            status="running", type="review", review_of_assignment_id="work-001",
            dispatched_at=2.0,
        )
        record_dispatched_assignment(assignment=review_a, repo_github="acme/api")
        mark_notified("rev-001", "completion")

        board = build_board()
        work = board.find_by_id("work-001")
        assert work is not None
        assert work.review_state == "done"

    def test_build_board_leaves_none_when_no_review_dispatched(
        self, coord_dir: Path
    ) -> None:
        """build_board leaves review_state=None when no review exists in the ledger."""
        from coord.models import Proposal
        from coord.state import mark_notified, record_dispatched

        work_proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Fix", rationale="",
        )
        record_dispatched(assignment_id="work-001", proposal=work_proposal, repo_github="acme/api")
        mark_notified("work-001", "completion")

        board = build_board()
        work = board.find_by_id("work-001")
        assert work is not None
        assert work.review_state is None

    def test_record_dispatched_assignment_stores_review_of_assignment_id(
        self, coord_dir: Path
    ) -> None:
        """record_dispatched_assignment persists review_of_assignment_id in the ledger."""
        from coord.state import load_dispatched, record_dispatched_assignment

        review_a = Assignment(
            machine_name="server",
            repo_name="api",
            issue_number=1,
            issue_title="[review] Fix",
            assignment_id="rev-001",
            status="running",
            type="review",
            review_of_assignment_id="work-001",
            dispatched_at=10.0,
        )
        record_dispatched_assignment(assignment=review_a, repo_github="acme/api")

        records = load_dispatched()
        assert len(records) == 1
        assert records[0]["review_of_assignment_id"] == "work-001"


# ── notify.py review dispatch ─────────────────────────────────────────────────


class TestNotifyPendingReviewDispatch:
    def test_notify_run_dispatches_pending_reviews_from_board(
        self, coord_dir: Path, repo: Repo
    ) -> None:
        """notify.run() dispatches pending reviews found on the saved board."""
        from coord.notify import run as run_notify

        config = Config(
            repos=[repo],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tail",
                    repos=["api"],
                    repo_paths={"api": "/work/api"},
                ),
            ],
            reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
        )

        # Save a board with a pending review.
        board = Board(
            completed=[
                _work_assignment(
                    status="done", branch="issue-1-fix", review_state="pending"
                )
            ]
        )
        save_board(board)

        mock_review = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="[review] Fix the thing",
            assignment_id="rev-new",
            status="running",
            type="review",
        )

        with patch("coord.notify._agent_status", return_value=None), \
             patch("coord.review.dispatch_review", return_value=mock_review):
            run_notify(config)

        # Board should be updated with review_state='dispatched'.
        loaded = load_board()
        assert loaded is not None
        assert loaded.completed[0].review_state == "dispatched"

    def test_notify_run_tolerates_missing_board(
        self, coord_dir: Path, repo: Repo
    ) -> None:
        """notify.run() is a no-op for review dispatch when no board file exists."""
        from coord.notify import run as run_notify

        config = Config(
            repos=[repo],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tail",
                    repos=["api"],
                    repo_paths={"api": "/work/api"},
                )
            ],
            reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
        )

        # No board file saved — should not raise.
        with patch("coord.notify._agent_status", return_value=None), \
             patch("coord.review.dispatch_review", return_value=None):
            run_notify(config)  # must not raise


# ── CLI status display ────────────────────────────────────────────────────────


class TestStatusReviewStateDisplay:
    """Verify that coord status shows review_state annotations for completed work."""

    @pytest.fixture
    def config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        )
        return p

    @pytest.fixture
    def cli_coord_dir(self, tmp_path: Path, coord_db) -> Path:
        d = tmp_path / "state"
        return d

    def _run_status(self, config_file: Path) -> str:
        from click.testing import CliRunner
        from coord.cli import main

        result = CliRunner().invoke(
            main,
            [
                "status",
                "--config", str(config_file),
                "--no-reconcile",
                "--timeout", "0.1",
            ],
        )
        return result.output

    def test_status_shows_awaiting_review(
        self, config_file: Path, cli_coord_dir: Path
    ) -> None:
        board = Board(
            completed=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=7,
                    issue_title="Do something",
                    assignment_id="w1",
                    status="done",
                    type="work",
                    review_state="pending",
                    finished_at=1.0,
                )
            ]
        )
        save_board(board)

        with patch("coord.network.check_all", return_value=[]):
            output = self._run_status(config_file)
        assert "[awaiting review]" in output
        assert "Do something" in output

    def test_status_shows_review_dispatched(
        self, config_file: Path, cli_coord_dir: Path
    ) -> None:
        board = Board(
            completed=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=8,
                    issue_title="Another task",
                    assignment_id="w2",
                    status="done",
                    type="work",
                    review_state="dispatched",
                    finished_at=2.0,
                )
            ]
        )
        save_board(board)

        with patch("coord.network.check_all", return_value=[]):
            output = self._run_status(config_file)
        assert "[review dispatched]" in output
        assert "Another task" in output

    def test_status_shows_review_done(
        self, config_file: Path, cli_coord_dir: Path
    ) -> None:
        board = Board(
            completed=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=9,
                    issue_title="Third task",
                    assignment_id="w3",
                    status="done",
                    type="work",
                    review_state="done",
                    finished_at=3.0,
                )
            ]
        )
        save_board(board)

        with patch("coord.network.check_all", return_value=[]):
            output = self._run_status(config_file)
        assert "[review done]" in output
        assert "Third task" in output

    def test_status_no_annotation_when_review_state_none(
        self, config_file: Path, cli_coord_dir: Path
    ) -> None:
        board = Board(
            completed=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=10,
                    issue_title="No review task",
                    assignment_id="w4",
                    status="done",
                    type="work",
                    review_state=None,
                    finished_at=4.0,
                )
            ]
        )
        save_board(board)

        with patch("coord.network.check_all", return_value=[]):
            output = self._run_status(config_file)
        # Task shown but without a review annotation
        assert "No review task" in output
        assert "[awaiting review]" not in output
        assert "[review dispatched]" not in output
        assert "[review done]" not in output

    def test_status_shows_cap_hit_blocker(
        self, config_file: Path, cli_coord_dir: Path
    ) -> None:
        """review_state='cap_hit' shows the ⚠ Auto-loop blockers section and
        the [⚠ iteration cap hit tag in the completed work listing."""
        board = Board(
            completed=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=42,
                    issue_title="Cap hit task",
                    assignment_id="w-cap",
                    status="done",
                    type="work",
                    review_state="cap_hit",
                    finished_at=5.0,
                )
            ]
        )
        save_board(board)

        with patch("coord.network.check_all", return_value=[]):
            output = self._run_status(config_file)

        assert "⚠ Auto-loop blockers" in output
        assert "[⚠ iteration cap hit" in output
        assert "Cap hit task" in output

    def test_status_only_shows_work_type_assignments(
        self, config_file: Path, cli_coord_dir: Path
    ) -> None:
        """Review and smoke assignments must not appear in completed work section."""
        board = Board(
            completed=[
                Assignment(
                    machine_name="laptop",
                    repo_name="api",
                    issue_number=1,
                    issue_title="Work task",
                    assignment_id="w1",
                    status="done",
                    type="work",
                    review_state="pending",
                    finished_at=1.0,
                ),
                Assignment(
                    machine_name="server",
                    repo_name="api",
                    issue_number=1,
                    issue_title="[review] Work task",
                    assignment_id="r1",
                    status="done",
                    type="review",
                    review_state=None,
                    finished_at=2.0,
                ),
            ]
        )
        save_board(board)

        with patch("coord.network.check_all", return_value=[]):
            output = self._run_status(config_file)

        # Work task appears, review assignment does not.
        assert "Work task" in output
        # The review assignment title should not appear in the work section.
        assert "[review] Work task" not in output
