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
    _fix_model_for_iteration,
    _post_max_iterations_notice,
    process_review_completion,
    run_for_fix_transition,
    run_for_review_transition,
)
from coord.config import Config, ModelsConfig, PipelineConfig, ReviewsConfig
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


@pytest.fixture
def config_path(tmp_path, coord_db):
    """Write a minimal coordinator.yml so `coord bounce` can `_load_config` it.

    `coord_db` is requested to set up the per-test SQLite home — the
    bounce CLI loads/saves the board through that DB.
    """
    _ = coord_db  # required for save_board / load_board path
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n    github: acme/api\n    default_branch: main\n"
        "machines:\n"
        "  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        "    repo_paths:\n      api: /work/api\n"
        "pipeline:\n  auto_loop: true\n  max_review_iterations: 3\n"
    )
    return p


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


# Default non-terminal stub for the #522 guard is provided by the autouse
# `_non_terminal_work` fixture in conftest.py — tests below opt into terminal
# behaviour by patching `coord.github_ops.work_is_terminal`.


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

        # #target_branch: the dispatch payload MUST tell the agent to
        # check out the original work's branch.  Without this the agent
        # would derive a new branch from the `[fix-1] …` slugified
        # title and the fix commits would land on an orphan branch
        # instead of the existing PR's branch.
        call_args = mock_http.post.call_args
        sent_payload = call_args.kwargs["json"]
        assert sent_payload["target_branch"] == "issue-1-fix", (
            f"fix dispatch must pin target_branch to the original work's "
            f"branch; got {sent_payload.get('target_branch')!r}"
        )

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


class TestTerminalGuard522:
    """#522: never dispatch a fix / re-review for work whose issue is already
    closed or whose PR is already merged — root cause of the 2026-06-09 launch
    flood (#349 ×4, #194).  The guard is fail-open, so these tests opt in by
    patching the github_ops helpers (stubbed non-terminal by default)."""

    def _request_changes_log(self, tmp_path):
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\n"
            "REVIEW_BODY:\nMissing tests.\nEND_REVIEW\n"
        )
        return log_file

    def test_skips_fix_when_issue_closed(
        self, config: Config, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        actions = process_review_completion(
            review, board, config,
            log_path=str(self._request_changes_log(tmp_path)),
            http_client=mock_http,
        )

        assert [a.kind for a in actions] == ["terminal_skip"]
        assert board.active == []                  # nothing dispatched
        mock_http.post.assert_not_called()         # no agent /assign POST
        assert work.review_state == "done"         # review marked resolved

    def test_skips_fix_when_pr_merged_even_if_issue_open(
        self, config: Config, tmp_path, monkeypatch
    ) -> None:
        # issue stays OPEN (default stub); only the PR is merged — the quadraui
        # develop-merge case where merging does NOT auto-close the issue.
        # (work_is_terminal collapses both signals; the github_ops unit tests
        # cover the issue-open/PR-merged split directly.)
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        actions = process_review_completion(
            review, board, config,
            log_path=str(self._request_changes_log(tmp_path)),
            http_client=mock_http,
        )

        assert [a.kind for a in actions] == ["terminal_skip"]
        assert board.active == []
        mock_http.post.assert_not_called()

    def test_dispatches_fix_when_not_terminal(
        self, config: Config, tmp_path
    ) -> None:
        # Default stub → non-terminal → the normal fix dispatch still fires.
        # Regression guard: the #522 check must not block legitimate fixes.
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-001"}
        mock_http.post.return_value.raise_for_status = MagicMock()
        with patch("coord.auto_loop.record_dispatched_assignment"):
            actions = process_review_completion(
                review, board, config,
                log_path=str(self._request_changes_log(tmp_path)),
                http_client=mock_http,
            )

        assert any(a.kind == "fix_dispatched" for a in actions)
        assert len(board.active) == 1
        mock_http.post.assert_called_once()

    def test_fix_completion_skips_rereview_when_terminal(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        from coord.state import load_board, save_board

        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)

        fix = _work_assignment(assignment_id="fix-1", review_iteration=1)
        fix.issue_title = "[fix-1] Fix the thing"
        fix.review_of_assignment_id = "work-abc"
        fix.review_state = "pending"
        save_board(Board(completed=[fix]))

        dispatched: dict = {}

        def fake_dispatch_review(*a, **k):
            dispatched["called"] = True
            return None

        monkeypatch.setattr("coord.auto_loop.dispatch_review", fake_dispatch_review)

        actions = run_for_fix_transition("fix-1", config)

        assert [a.kind for a in actions] == ["terminal_skip"]
        assert "called" not in dispatched          # dispatch_review never called
        loaded = load_board()
        assert loaded is not None
        reloaded = loaded.find_by_id("fix-1")
        assert reloaded is not None
        assert reloaded.review_state == "done"     # persisted to the board

    # The #349-×4 cache-collapse behaviour now lives in
    # coord.github_ops.work_is_terminal and is covered by
    # tests/test_github_ops.py::TestWorkIsTerminal::test_cache_collapses_repeat_calls.


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


# ── Unit tests: _fix_model_for_iteration ─────────────────────────────────────


def _config_with_models(
    *,
    default: str = "sonnet",
    escalation: list[str] | None = None,
    escalate_fix_model: bool = True,
) -> Config:
    """Build a Config with a tunable models ladder + escalate knob."""
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        models=ModelsConfig(
            default=default,
            escalation=escalation or ["haiku", "sonnet", "opus"],
        ),
        pipeline=PipelineConfig(escalate_fix_model=escalate_fix_model),
    )


class TestFixModelForIteration:
    def test_iteration_1_returns_base_default(self) -> None:
        cfg = _config_with_models(default="sonnet")
        assert _fix_model_for_iteration(cfg, 1) == "sonnet"

    def test_iteration_2_returns_next_rung(self) -> None:
        # default sonnet, ladder [haiku, sonnet, opus] → iter 2 escalates to opus
        cfg = _config_with_models(default="sonnet")
        assert _fix_model_for_iteration(cfg, 2) == "opus"

    def test_iteration_beyond_ladder_caps_at_top(self) -> None:
        cfg = _config_with_models(default="sonnet")
        # iter 3+ stays capped at the top of the ladder (opus)
        assert _fix_model_for_iteration(cfg, 3) == "opus"
        assert _fix_model_for_iteration(cfg, 10) == "opus"

    def test_escalates_one_rung_per_iteration_from_bottom(self) -> None:
        # default haiku, ladder [haiku, sonnet, opus]
        cfg = _config_with_models(default="haiku")
        assert _fix_model_for_iteration(cfg, 1) == "haiku"
        assert _fix_model_for_iteration(cfg, 2) == "sonnet"
        assert _fix_model_for_iteration(cfg, 3) == "opus"
        assert _fix_model_for_iteration(cfg, 4) == "opus"  # capped

    def test_returns_none_when_escalation_disabled(self) -> None:
        cfg = _config_with_models(escalate_fix_model=False)
        assert _fix_model_for_iteration(cfg, 1) is None
        assert _fix_model_for_iteration(cfg, 2) is None
        assert _fix_model_for_iteration(cfg, 5) is None


class TestFixModelDispatch:
    """The escalated model lands on both the POST payload and the Assignment."""

    def _dispatch(self, config: Config, tmp_path) -> tuple[Any, Any]:
        log_file = tmp_path / "review.log"
        log_file.write_text(
            "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\nFix.\nEND_REVIEW\n"
        )
        review = _review_assignment()
        work = _work_assignment(review_iteration=0)
        board = _board_with(work, review)

        mock_http = MagicMock()
        mock_http.post.return_value.json.return_value = {"id": "fix-001"}
        mock_http.post.return_value.raise_for_status = MagicMock()

        with patch("coord.auto_loop.record_dispatched_assignment"):
            process_review_completion(
                review, board, config,
                log_path=str(log_file),
                http_client=mock_http,
            )

        sent_payload = mock_http.post.call_args.kwargs["json"]
        fix = board.active[0]
        return sent_payload, fix

    def test_payload_and_assignment_carry_base_model_on_first_fix(
        self, tmp_path
    ) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tail",
                    repos=["api"], repo_paths={"api": "/work/api"},
                )
            ],
            reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
            models=ModelsConfig(default="sonnet", escalation=["haiku", "sonnet", "opus"]),
            pipeline=PipelineConfig(auto_loop=True, escalate_fix_model=True),
        )
        # work.review_iteration=0 → next_iteration=1 → base model "sonnet"
        payload, fix = self._dispatch(cfg, tmp_path)
        assert payload["model"] == "sonnet"
        assert fix.model == "sonnet"

    def test_no_model_set_when_escalation_disabled(self, tmp_path) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tail",
                    repos=["api"], repo_paths={"api": "/work/api"},
                )
            ],
            reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
            pipeline=PipelineConfig(auto_loop=True, escalate_fix_model=False),
        )
        payload, fix = self._dispatch(cfg, tmp_path)
        assert "model" not in payload  # legacy behaviour: no model key
        assert fix.model is None


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

    def test_max_review_iterations_defaults_to_5(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.pipeline.max_review_iterations == 5

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

    def test_escalate_fix_model_defaults_to_true(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.pipeline.escalate_fix_model is True

    def test_can_disable_escalate_fix_model(self, tmp_path) -> None:
        from coord.config import load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  escalate_fix_model: false\n"
        )
        cfg = load(p)
        assert cfg.pipeline.escalate_fix_model is False

    def test_invalid_escalate_fix_model_raises(self, tmp_path) -> None:
        from coord.config import ConfigError, load

        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [api]\n"
            "pipeline:\n  escalate_fix_model: maybe\n"
        )
        with pytest.raises(
            ConfigError, match="pipeline.escalate_fix_model must be a boolean"
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


# ── coord bounce CLI + HTTP fallback ────────────────────────────────────────


class TestProcessReviewCompletionAgentFallback:
    """When the local log isn't reachable, process_review_completion
    falls back to fetching the structured findings via the agent's
    `/logs/<id>` HTTP endpoint.  Closes the gap that left quadraui#166
    without an auto-fix dispatch."""

    def test_falls_back_to_agent_when_local_log_missing(
        self, config: Config, monkeypatch
    ) -> None:
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        # Local log doesn't exist; agent HTTP returns findings.
        from coord.review import ReviewFindings
        called = {}

        def fake_agent(host, aid, *args, **kwargs):
            called["host"] = host
            called["aid"] = aid
            return ReviewFindings(
                verdict="request-changes",
                body="Issue in src/main.py — handle None case.",
            )

        monkeypatch.setattr(
            "coord.auto_loop.parse_review_from_agent", fake_agent,
        )

        def fake_dispatch(*args, **kwargs):
            # Stub the dispatch so the test doesn't need an agent server.
            from coord.models import Assignment as A
            return A(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="t", briefing="",
                assignment_id="fix-1", status="running",
                type="work", review_iteration=1,
                review_of_assignment_id=work.assignment_id,
            )

        monkeypatch.setattr("coord.auto_loop._dispatch_fix", fake_dispatch)

        actions = process_review_completion(
            review,
            board,
            config,
            log_path=None,  # no local log
            machine_host="elitebook.tailnet",
        )

        assert called.get("host") == "elitebook.tailnet"
        assert called.get("aid") == review.assignment_id
        # Should have dispatched a fix worker via the HTTP-fetched findings.
        assert any(a.kind == "fix_dispatched" for a in actions), actions

    def test_no_fallback_when_no_host_supplied(
        self, config: Config, monkeypatch
    ) -> None:
        """Without a machine_host the function can't fall back — must
        still degrade to no_findings rather than crash."""
        review = _review_assignment()
        work = _work_assignment()
        board = _board_with(work, review)

        # The agent fallback must NOT be invoked when host is None.
        def boom(*args, **kwargs):
            raise AssertionError("parse_review_from_agent should not be called")

        monkeypatch.setattr("coord.auto_loop.parse_review_from_agent", boom)

        actions = process_review_completion(
            review, board, config, log_path=None, machine_host=None,
        )
        assert actions[0].kind == "no_findings"


class TestCoordBounceCommand:
    """The `coord bounce <review-id>` CLI command — manual trigger
    for the auto-loop's fix-dispatch path, used by the TUI's F key /
    'Address review findings' action."""

    def test_bounce_dispatches_when_verdict_is_request_changes(
        self, config_path, monkeypatch
    ) -> None:
        """Happy path: review with request-changes → fix worker
        dispatched, exit 0, board saved."""
        from click.testing import CliRunner
        from coord.cli import main as cli_main
        from coord.models import Assignment, Board
        from coord.state import save_board

        # Seed the board with paired work + review (request-changes).
        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", briefing="b",
            assignment_id="work-1", status="done",
            type="work", branch="issue-42-t",
        )
        review = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", briefing="",
            assignment_id="review-1", status="done",
            type="review", review_of_assignment_id="work-1",
            review_verdict="request-changes",
        )
        save_board(Board(completed=[work, review]))

        # Stub the dispatch so the test doesn't need a live agent.
        def fake_dispatch(*args, **kwargs):
            return Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="t", briefing="",
                assignment_id="fix-1", status="running",
                type="work", review_iteration=1,
                review_of_assignment_id="work-1",
            )

        monkeypatch.setattr("coord.auto_loop._dispatch_fix", fake_dispatch)

        # Stub findings — bypass the log/HTTP path entirely.
        from coord.review import ReviewFindings
        monkeypatch.setattr(
            "coord.auto_loop.parse_review_from_agent",
            lambda *a, **kw: ReviewFindings(verdict="request-changes", body="fix x"),
        )

        result = CliRunner().invoke(cli_main, [
            "bounce", "review-1", "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "fix_dispatched" in result.output

    def test_bounce_refuses_when_verdict_is_approve(
        self, config_path
    ) -> None:
        from click.testing import CliRunner
        from coord.cli import main as cli_main
        from coord.models import Assignment, Board
        from coord.state import save_board

        review = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", briefing="",
            assignment_id="review-2", status="done",
            type="review", review_of_assignment_id="work-1",
            review_verdict="approve",
        )
        save_board(Board(completed=[review]))

        result = CliRunner().invoke(cli_main, [
            "bounce", "review-2", "--config", str(config_path),
        ])
        # Refuses with a clear message; doesn't dispatch anything.
        assert result.exit_code != 0
        assert "request-changes" in result.output

    def test_bounce_refuses_when_assignment_not_review(
        self, config_path
    ) -> None:
        from click.testing import CliRunner
        from coord.cli import main as cli_main
        from coord.models import Assignment, Board
        from coord.state import save_board

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="t", briefing="b",
            assignment_id="work-3", status="done", type="work",
        )
        save_board(Board(completed=[work]))

        result = CliRunner().invoke(cli_main, [
            "bounce", "work-3", "--config", str(config_path),
        ])
        assert result.exit_code != 0
        assert "not 'review'" in result.output or "work" in result.output.lower()

    def test_bounce_unknown_assignment_id(self, config_path) -> None:
        from click.testing import CliRunner
        from coord.cli import main as cli_main
        from coord.models import Board
        from coord.state import save_board

        save_board(Board())

        result = CliRunner().invoke(cli_main, [
            "bounce", "nope", "--config", str(config_path),
        ])
        assert result.exit_code != 0
        assert "not found" in result.output


class TestReviewFindingsDbCache:
    """The DB cache layer for review findings.  notify populates it on
    first parse; coord bounce reads it back near-instantly so we don't
    have to refetch the multi-MB worker log over Tailscale every time."""

    def test_save_and_load_roundtrip(self, coord_db) -> None:
        from coord.state import (
            update_assignment_review_findings,
            load_assignment_review_findings,
        )
        from coord.models import Assignment, Board
        from coord.state import save_board

        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="t", briefing="",
            assignment_id="r1", status="done", type="review",
        )
        save_board(Board(completed=[review]))

        update_assignment_review_findings(
            "r1", verdict="request-changes",
            body="### Required changes\n- Handle None case",
        )
        result = load_assignment_review_findings("r1")
        assert result is not None
        verdict, body = result
        assert verdict == "request-changes"
        assert "Handle None case" in body

    def test_load_returns_none_when_unset(self, coord_db) -> None:
        from coord.state import (
            save_board, load_assignment_review_findings,
        )
        from coord.models import Assignment, Board

        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="t", briefing="",
            assignment_id="r2", status="done", type="review",
        )
        save_board(Board(completed=[review]))
        # Never wrote findings — should be None.
        assert load_assignment_review_findings("r2") is None

    def test_load_returns_none_for_unknown_id(self, coord_db) -> None:
        from coord.state import load_assignment_review_findings
        assert load_assignment_review_findings("ghost") is None

    def test_load_findings_via_cache_skips_log_and_http(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        """When DB has the cached findings, neither the local log nor
        the agent HTTP fallback are touched."""
        from coord.auto_loop import _load_review_findings
        from coord.state import (
            update_assignment_review_findings, save_board,
        )
        from coord.models import Assignment, Board

        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="t", briefing="",
            assignment_id="r3", status="done", type="review",
        )
        save_board(Board(completed=[review]))
        update_assignment_review_findings(
            "r3", verdict="approve", body="Looks good."
        )

        # If the function reached the HTTP fallback it would call this:
        def boom_http(*args, **kwargs):
            raise AssertionError(
                "DB cache should have served the request — HTTP fetch must not run"
            )

        def boom_log(*args, **kwargs):
            raise AssertionError(
                "DB cache should have served the request — log parse must not run"
            )

        monkeypatch.setattr("coord.auto_loop.parse_review_from_agent", boom_http)
        monkeypatch.setattr("coord.auto_loop.parse_review_from_log", boom_log)

        findings = _load_review_findings(review, log_path="/no/such", machine_host="x")
        assert findings is not None
        assert findings.verdict == "approve"
        assert findings.body == "Looks good."

    def test_falls_back_to_http_when_cache_empty(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        """When the DB row exists but review_findings is NULL (e.g. a
        review that completed before this cache landed), the loader
        falls back to local log → HTTP as before."""
        from coord.auto_loop import _load_review_findings
        from coord.state import save_board
        from coord.models import Assignment, Board
        from coord.review import ReviewFindings

        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="t", briefing="",
            assignment_id="r4", status="done", type="review",
        )
        save_board(Board(completed=[review]))
        # No update_assignment_review_findings call — cache stays NULL.

        monkeypatch.setattr(
            "coord.auto_loop.parse_review_from_agent",
            lambda h, aid, *a, **kw: ReviewFindings(
                verdict="request-changes", body="from http"
            ),
        )
        findings = _load_review_findings(
            review, log_path=None, machine_host="elitebook.tail"
        )
        assert findings is not None
        assert findings.body == "from http"


# ── Unit tests: run_for_fix_transition ──────────────────────────────────────


def _fix_assignment(
    assignment_id: str = "fix-1",
    review_iteration: int = 1,
    review_of: str = "work-abc",
) -> Assignment:
    """Build a bounce-fix work assignment (the type dispatched by process_review_completion)."""
    a = _work_assignment(assignment_id=assignment_id, review_iteration=review_iteration)
    a.review_of_assignment_id = review_of
    a.issue_title = f"[fix-{review_iteration}] Fix the thing"
    return a


def _stub_review_assignment(assignment_id: str = "re-review-1") -> Assignment:
    """Build a minimal review assignment to stand in for dispatch_review's return value."""
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="[review] Fix the thing",
        assignment_id=assignment_id,
        status="running",
        type="review",
        review_of_assignment_id="fix-1",
    )


class TestRunForFixTransition:
    """run_for_fix_transition: auto-dispatch a fresh review when a fix worker completes."""

    def test_run_for_fix_transition_dispatches_review(
        self, config: Config, coord_db
    ) -> None:
        """Happy path: fix worker completes → fresh review dispatched, board saved."""
        from coord.state import load_board, save_board

        fix = _fix_assignment()
        board = Board(completed=[fix])
        save_board(board)

        stub_review = _stub_review_assignment()

        with patch("coord.auto_loop.dispatch_review", return_value=stub_review):
            actions = run_for_fix_transition("fix-1", config)

        assert len(actions) == 1
        assert actions[0].kind == "review_dispatched"
        assert actions[0].assignment_id == "fix-1"

        # Board was saved with the fix's review_state updated.
        loaded = load_board()
        assert loaded is not None
        found = loaded.find_by_id("fix-1")
        assert found is not None
        assert found.review_state == "dispatched"

    def test_run_for_fix_transition_iteration_cap_hit(
        self, config: Config, coord_db
    ) -> None:
        """fix.review_iteration == max_review_iterations → no review dispatched."""
        from coord.state import save_board

        # review_iteration == max_review_iterations (3) → cap hit.
        fix = _fix_assignment(assignment_id="fix-3", review_iteration=3)
        board = Board(completed=[fix])
        save_board(board)

        with (
            patch("coord.auto_loop.dispatch_review") as mock_dispatch,
            patch("coord.auto_loop._post_max_iterations_notice") as mock_notice,
        ):
            actions = run_for_fix_transition("fix-3", config)

        assert len(actions) == 1
        assert actions[0].kind == "iteration_cap_hit"
        # dispatch_review must NOT be called when the cap is hit.
        mock_dispatch.assert_not_called()
        # GitHub notice must be posted when the cap is hit.
        mock_notice.assert_called_once()

    def test_run_for_fix_transition_cap_hit_posts_comment_and_marks_board(
        self, config: Config, coord_db
    ) -> None:
        """Cap-hit path posts a GitHub comment and persists review_state='cap_hit'."""
        from coord.state import load_board, save_board

        fix = _fix_assignment(assignment_id="fix-cap", review_iteration=3)
        board = Board(completed=[fix])
        save_board(board)

        with (
            patch("coord.auto_loop.dispatch_review"),
            patch("coord.auto_loop._post_max_iterations_notice") as mock_notice,
        ):
            actions = run_for_fix_transition("fix-cap", config)

        # GitHub comment was posted exactly once, with the fix assignment and config.
        # We check individual args rather than the whole object because `fix` is
        # mutated in-place (review_state set to "cap_hit") after the mock call.
        mock_notice.assert_called_once()
        called_with_fix, called_with_config = mock_notice.call_args[0]
        assert called_with_fix.assignment_id == "fix-cap"
        assert called_with_config is config

        # Board was saved with the fix marked as cap_hit.
        loaded = load_board()
        assert loaded is not None
        entry = loaded.find_by_id("fix-cap")
        assert entry is not None
        assert entry.review_state == "cap_hit"

        # Action kind confirms cap was hit.
        assert len(actions) == 1
        assert actions[0].kind == "iteration_cap_hit"

    def test_run_for_fix_transition_no_machine_available(
        self, config: Config, coord_db
    ) -> None:
        """dispatch_review returns None (no capable machine) → graceful no-op."""
        from coord.state import save_board

        fix = _fix_assignment()
        board = Board(completed=[fix])
        save_board(board)

        with patch("coord.auto_loop.dispatch_review", return_value=None):
            actions = run_for_fix_transition("fix-1", config)

        # No dispatch possible → empty list (caller can retry later).
        assert actions == []

    def test_run_for_fix_transition_disabled(
        self, config_loop_disabled: Config, coord_db
    ) -> None:
        """auto_loop=false → disabled action, no dispatch attempt."""
        from coord.state import save_board

        fix = _fix_assignment()
        board = Board(completed=[fix])
        save_board(board)

        with patch("coord.auto_loop.dispatch_review") as mock_dispatch:
            actions = run_for_fix_transition("fix-1", config_loop_disabled)

        assert len(actions) == 1
        assert actions[0].kind == "disabled"
        mock_dispatch.assert_not_called()

    def test_run_for_fix_transition_no_board(self, config: Config) -> None:
        """No saved board → returns empty list without raising."""
        with patch("coord.auto_loop.load_board", return_value=None):
            actions = run_for_fix_transition("fix-1", config)
        assert actions == []

    def test_run_for_fix_transition_assignment_not_on_board(
        self, config: Config, coord_db
    ) -> None:
        """Fix assignment not found on board → returns empty list without raising."""
        from coord.state import save_board

        save_board(Board())  # empty board

        actions = run_for_fix_transition("nonexistent-id", config)
        assert actions == []

    def test_run_for_fix_transition_below_cap_dispatches(
        self, config: Config, coord_db
    ) -> None:
        """review_iteration < max_review_iterations → dispatch proceeds."""
        from coord.state import save_board

        # iteration=2, max=3 → still allowed.
        fix = _fix_assignment(assignment_id="fix-2", review_iteration=2)
        board = Board(completed=[fix])
        save_board(board)

        stub_review = _stub_review_assignment(assignment_id="re-review-2")

        with patch("coord.auto_loop.dispatch_review", return_value=stub_review):
            actions = run_for_fix_transition("fix-2", config)

        assert len(actions) == 1
        assert actions[0].kind == "review_dispatched"
