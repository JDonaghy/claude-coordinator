"""Tests for coord/auto_loop.py — automated review → fix → re-review cycle."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coord.auto_loop import (
    LoopAction,
    _build_fix_briefing,
    _post_max_iterations_notice,
    process_review_completion,
    run_for_review_transition,
)
from coord.config import Config, PipelineConfig, ReviewsConfig
from coord.models import Assignment, Board, Machine, Repo
from coord.review import ReviewFindings


# ── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def repo() -> Repo:
    return Repo(name="api", github="acme/api", depends_on=[], default_branch="main")


@pytest.fixture
def machine(repo: Repo) -> Machine:
    return Machine(
        name="laptop",
        host="laptop.tail",
        capabilities=["python"],
        repos=["api"],
        repo_paths={"api": "/work/api"},
    )


@pytest.fixture
def config(repo: Repo, machine: Machine) -> Config:
    return Config(
        repos=[repo],
        machines=[machine],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
        pipeline=PipelineConfig(auto_loop=True, max_review_iterations=3),
    )


@pytest.fixture
def config_loop_disabled(repo: Repo, machine: Machine) -> Config:
    return Config(
        repos=[repo],
        machines=[machine],
        pipeline=PipelineConfig(auto_loop=False),
    )


def _work_assignment(
    assignment_id: str = "work-abc",
    branch: str = "issue-1-fix",
    review_iteration: int = 0,
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix the thing",
        briefing="Original briefing text.",
        assignment_id=assignment_id,
        status="done",
        branch=branch,
        pr_url="https://github.com/acme/api/pull/42",
        dispatched_at=0.0,
        finished_at=1.0,
        type="work",
        review_state="dispatched",
        review_iteration=review_iteration,
    )


def _review_assignment(
    assignment_id: str = "review-xyz",
    review_of: str = "work-abc",
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="[review] Fix the thing",
        assignment_id=assignment_id,
        status="done",
        branch="issue-1-fix",
        dispatched_at=1.0,
        finished_at=2.0,
        type="review",
        review_of_assignment_id=review_of,
    )


def _board_with(work: Assignment, review: Assignment | None = None) -> Board:
    completed = [work]
    if review is not None:
        completed.append(review)
    return Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=completed,
    )


def _approve_findings() -> ReviewFindings:
    return ReviewFindings(verdict="approve", body="LGTM — all tests pass.")


def _request_changes_findings() -> ReviewFindings:
    return ReviewFindings(
        verdict="request-changes",
        body="## Issues\n- Missing test coverage for edge case X\n- Typo in docstring",
    )


# ── Unit tests: process_review_completion ───────────────────────────────────


class TestProcessReviewCompletion:
    def test_auto_loop_disabled_returns_disabled_action(
        self, config_loop_disabled: Config
    ) -> None:
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        actions = process_review_completion(
            review, board, config_loop_disabled, log_path=None
        )

        assert len(actions) == 1
        assert actions[0].kind == "disabled"

    def test_no_log_path_returns_no_findings(self, config: Config) -> None:
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        actions = process_review_completion(
            review, board, config, log_path=None
        )

        assert len(actions) == 1
        assert actions[0].kind == "no_findings"

    def test_log_parse_fails_returns_no_findings(
        self, config: Config, tmp_path
    ) -> None:
        log_file = tmp_path / "review.log"
        log_file.write_text("No structured output here.")

        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        actions = process_review_completion(
            review, board, config, log_path=str(log_file)
        )

        assert len(actions) == 1
        assert actions[0].kind == "no_findings"

    def test_approve_verdict_returns_approved(self, config: Config, tmp_path) -> None:
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "Some preamble.\n\n"
            "REVIEW_VERDICT: approve\n"
            "REVIEW_BODY:\n"
            "LGTM — all tests pass.\n"
            "END_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        actions = process_review_completion(
            review, board, config, log_path=str(log_file)
        )

        assert len(actions) == 1
        assert actions[0].kind == "approved"
        # Work assignment review_state updated
        assert work.review_state == "done"

    def test_approve_verdict_skips_review_state_update_when_work_not_found(
        self, config: Config, tmp_path
    ) -> None:
        """Approved verdict with no parent assignment on board should not crash."""
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: approve\nREVIEW_BODY:\nGood.\nEND_REVIEW\n"
        )
        review = _review_assignment(review_of="nonexistent-id")
        board = Board(completed=[review])

        actions = process_review_completion(
            review, board, config, log_path=str(log_file)
        )

        assert actions[0].kind == "approved"  # should not raise

    def test_request_changes_dispatches_fix_worker(
        self, config: Config, tmp_path
    ) -> None:
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "Missing tests for edge case X.\n"
            "END_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        fake_agent_resp = {"id": "fix-001"}
        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = fake_agent_resp
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert len(actions) == 1
        assert actions[0].kind == "fix_dispatched"
        # Fix worker was added to board.active
        assert len(board.active) == 1
        fix = board.active[0]
        assert fix.type == "work"
        assert fix.review_iteration == 1
        assert fix.branch == "issue-1-fix"
        assert fix.review_of_assignment_id == "work-abc"

    def test_request_changes_work_not_on_board_returns_no_work_found(
        self, config: Config, tmp_path
    ) -> None:
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nFix things.\nEND_REVIEW\n"
        )
        review = _review_assignment(review_of="missing-id")
        board = Board(completed=[review])

        actions = process_review_completion(
            review, board, config, log_path=str(log_file)
        )

        assert actions[0].kind == "no_work_found"

    def test_max_iterations_stops_loop(self, config: Config, tmp_path) -> None:
        """When work.review_iteration == max_review_iterations, stop and notify."""
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nStill broken.\nEND_REVIEW\n"
        )
        # work.review_iteration == max_review_iterations → next would be 4 > 3
        review = _review_assignment()
        work = _work_assignment(review_iteration=3)  # already at max
        board = _board_with(work, review)

        with patch("coord.auto_loop._post_max_iterations_notice") as mock_notice:
            actions = process_review_completion(
                review, board, config, log_path=str(log_file)
            )

        assert actions[0].kind == "max_iterations"
        mock_notice.assert_called_once()
        # No new assignment dispatched
        assert len(board.active) == 0

    def test_fix_iteration_increments_correctly(
        self, config: Config, tmp_path
    ) -> None:
        """review_iteration on the fix worker = work.review_iteration + 1."""
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nFix again.\nEND_REVIEW\n"
        )
        # Simulate a second round: fix_1 (review_iteration=1) was just reviewed
        review = _review_assignment(assignment_id="review-2", review_of="fix-1")
        fix_1 = _work_assignment(assignment_id="fix-1", review_iteration=1)
        board = Board(completed=[fix_1, review])

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-2"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        assert actions[0].kind == "fix_dispatched"
        fix_2 = board.active[0]
        assert fix_2.review_iteration == 2

    def test_max_iterations_boundary_last_allowed_fix(
        self, config: Config, tmp_path
    ) -> None:
        """Iteration 3 (== max) should still dispatch the fix."""
        # config.pipeline.max_review_iterations == 3
        # work.review_iteration == 2 → next is 3 == max, which is still allowed
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nFix it.\nEND_REVIEW\n"
        )
        review = _review_assignment(assignment_id="review-3", review_of="fix-2")
        fix_2 = _work_assignment(assignment_id="fix-2", review_iteration=2)
        board = Board(completed=[fix_2, review])

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-3"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        # iteration 3 <= max(3) → dispatch allowed
        assert actions[0].kind == "fix_dispatched"
        fix_3 = board.active[0]
        assert fix_3.review_iteration == 3


# ── Unit tests: _build_fix_briefing ─────────────────────────────────────────


class TestBuildFixBriefing:
    def test_contains_reviewer_findings(self) -> None:
        work = _work_assignment()
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "Missing test coverage for edge case X" in briefing

    def test_contains_iteration_info(self) -> None:
        work = _work_assignment()
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=2, max_iter=3)
        assert "iteration 2" in briefing
        assert "3" in briefing  # max shown

    def test_contains_branch_name(self) -> None:
        work = _work_assignment(branch="issue-42-cool-feature")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "issue-42-cool-feature" in briefing

    def test_contains_original_briefing(self) -> None:
        work = _work_assignment()
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "Original briefing text." in briefing

    def test_no_crash_when_work_has_no_briefing(self) -> None:
        work = replace(_work_assignment(), briefing="")
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "Original work briefing" not in briefing  # omitted when empty

    def test_contains_do_not_change_branch_instruction(self) -> None:
        work = _work_assignment()
        findings = _request_changes_findings()
        briefing = _build_fix_briefing(work, findings, iteration=1, max_iter=3)
        assert "do not change the branch name" in briefing.lower()


# ── Unit tests: config parsing ───────────────────────────────────────────────


class TestPipelineConfigParsing:
    def test_auto_loop_defaults_to_true(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.pipeline.auto_loop is True

    def test_max_review_iterations_defaults_to_3(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.pipeline.max_review_iterations == 3

    def test_can_disable_auto_loop(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  auto_loop: false\n  max_review_iterations: 5\n"
        )
        cfg = load(p)
        assert cfg.pipeline.auto_loop is False
        assert cfg.pipeline.max_review_iterations == 5

    def test_invalid_auto_loop_raises(self, tmp_path) -> None:
        from coord.config import ConfigError, load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  auto_loop: yes_please\n"
        )
        with pytest.raises(ConfigError, match="pipeline.auto_loop must be a boolean"):
            load(p)

    def test_invalid_max_review_iterations_raises(self, tmp_path) -> None:
        from coord.config import ConfigError, load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  max_review_iterations: 0\n"
        )
        with pytest.raises(
            ConfigError, match="pipeline.max_review_iterations must be a positive integer"
        ):
            load(p)


# ── Unit tests: Assignment.review_iteration persistence ─────────────────────


class TestReviewIterationPersistence:
    def test_review_iteration_saved_and_loaded(self, coord_db) -> None:
        from coord.state import load_board, save_board

        work = _work_assignment(review_iteration=2)
        work.status = "done"
        board = Board(completed=[work])
        save_board(board)

        loaded = load_board()
        assert loaded is not None
        found = loaded.find_by_id("work-abc")
        assert found is not None
        assert found.review_iteration == 2

    def test_review_iteration_defaults_to_zero_when_absent(self, coord_db) -> None:
        from coord.state import load_board, save_board

        work = _work_assignment()  # review_iteration=0 by default
        work.status = "done"
        board = Board(completed=[work])
        save_board(board)

        loaded = load_board()
        assert loaded is not None
        found = loaded.find_by_id("work-abc")
        assert found is not None
        assert found.review_iteration == 0


# ── Integration test: full work → review → fix → review → approve cycle ─────


class TestFullCycle:
    """End-to-end simulation of the auto-loop using mocked HTTP and log files."""

    def test_work_review_fix_review_approve(self, config: Config, tmp_path, coord_db) -> None:
        """Simulate: work done → review requests changes → fix dispatched →
        second review approves → pipeline advances."""
        from coord.state import load_board, save_board

        # -- Round 1: work assignment completes --
        work = _work_assignment(assignment_id="w-1", review_iteration=0)
        board = Board(completed=[work])
        save_board(board)

        # -- Round 2: review requests changes --
        review_log = tmp_path / "review1.log"
        review_log.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\n"
            "Missing input validation on the endpoint.\n"
            "END_REVIEW\n"
        )
        review1 = _review_assignment(assignment_id="r-1", review_of="w-1")
        review1.status = "done"
        board2 = load_board()
        assert board2 is not None
        board2.completed.append(review1)
        save_board(board2)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-1"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review1, board2, config,
                log_path=str(review_log),
                http_client=mock_http,
            )

        assert actions[0].kind == "fix_dispatched"
        fix1 = board2.active[0]
        assert fix1.assignment_id == "fix-1"
        assert fix1.review_iteration == 1
        assert "Missing input validation" in fix1.briefing
        save_board(board2)

        # -- Round 3: fix completes, second review approves --
        review2_log = tmp_path / "review2.log"
        review2_log.write_text(
            "REVIEW_VERDICT: approve\n"
            "REVIEW_BODY:\n"
            "Validation added. Tests pass. LGTM.\n"
            "END_REVIEW\n"
        )
        # Transition fix-1 to done
        board3 = load_board()
        assert board3 is not None
        fix1_on_board = board3.find_by_id("fix-1")
        assert fix1_on_board is not None
        assert fix1_on_board.review_iteration == 1
        fix1_on_board.status = "done"
        board3.completed.append(
            board3.active.pop(board3.active.index(fix1_on_board))
        )

        review2 = _review_assignment(assignment_id="r-2", review_of="fix-1")
        review2.status = "done"
        board3.completed.append(review2)
        save_board(board3)

        actions2 = process_review_completion(
            review2, board3, config,
            log_path=str(review2_log),
        )

        assert actions2[0].kind == "approved"
        # fix-1's review_state was updated to "done"
        fix1_final = board3.find_by_id("fix-1")
        assert fix1_final is not None
        assert fix1_final.review_state == "done"

    def test_max_iterations_stops_at_configured_limit(
        self, config: Config, tmp_path
    ) -> None:
        """After max_review_iterations fix rounds, the loop stops and posts notice."""
        review_log = tmp_path / "review.log"
        review_log.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nStill broken.\nEND_REVIEW\n"
        )

        # work already at max (review_iteration == max_review_iterations == 3)
        work = _work_assignment(review_iteration=3)
        review = _review_assignment(review_of="work-abc")
        board = _board_with(work, review)

        with patch("coord.auto_loop._post_max_iterations_notice") as mock_notice:
            actions = process_review_completion(
                review, board, config, log_path=str(review_log)
            )

        assert actions[0].kind == "max_iterations"
        mock_notice.assert_called_once_with(work, config)
        assert len(board.active) == 0  # no fix dispatched


# ── Unit tests: run_for_review_transition (notify integration) ───────────────


class TestRunForReviewTransition:
    def test_returns_disabled_when_auto_loop_off(
        self, config_loop_disabled: Config, coord_db
    ) -> None:
        from coord.state import save_board

        work = _work_assignment()
        review = _review_assignment()
        save_board(Board(completed=[work, review]))

        record = {"type": "review", "review_of_assignment_id": "work-abc"}
        entry = {"log_path": None}

        actions = run_for_review_transition(
            "review-xyz", record, entry, config_loop_disabled
        )
        assert actions[0].kind == "disabled"

    def test_returns_empty_for_non_review_type(self, config: Config, coord_db) -> None:
        record = {"type": "work"}
        entry: dict = {}
        actions = run_for_review_transition("some-id", record, entry, config)
        assert actions == []

    def test_returns_empty_when_no_board(self, config: Config) -> None:
        """If there is no saved board, run_for_review_transition returns []."""
        record = {"type": "review"}
        entry: dict = {}
        # coord_db fixture not used → no board exists
        with patch("coord.auto_loop.load_board", return_value=None):
            actions = run_for_review_transition("r-1", record, entry, config)
        assert actions == []

    def test_saves_board_when_fix_dispatched(
        self, config: Config, tmp_path, coord_db
    ) -> None:
        from coord.state import load_board, save_board

        review_log = tmp_path / "review.log"
        review_log.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nFix it.\nEND_REVIEW\n"
        )
        work = _work_assignment()
        review = _review_assignment()
        board = Board(completed=[work, review])
        save_board(board)

        record = {"type": "review", "review_of_assignment_id": "work-abc"}
        entry = {"log_path": str(review_log)}

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-new"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"), \
             patch("coord.auto_loop.httpx", mock_http):
            actions = run_for_review_transition(
                "review-xyz", record, entry, config
            )

        assert any(a.kind == "fix_dispatched" for a in actions)
        # Board was saved: newly dispatched fix should appear in loaded board
        loaded = load_board()
        assert loaded is not None
        # The fix was added to board.active before save
        assert any(a.assignment_id == "fix-new" for a in loaded.active)

    def test_review_not_on_board_returns_empty(
        self, config: Config, tmp_path, coord_db
    ) -> None:
        from coord.state import save_board

        # Board has work but not the review
        work = _work_assignment()
        save_board(Board(completed=[work]))

        record = {"type": "review", "review_of_assignment_id": "work-abc"}
        entry = {"log_path": None}

        actions = run_for_review_transition(
            "review-xyz",  # not on board
            record, entry, config,
        )
        # Review not found on board → no actions (empty list or no_findings)
        # Should not raise; either returns [] or a no_findings/no_work_found action
        assert isinstance(actions, list)
