"""Tests for coord.notify — polling agents and posting GH comments."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.config import Config
from coord.models import Machine, Proposal, Repo
from coord import notify as notify_mod
from coord import state as state_mod


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    """Provide an isolated in-memory DB for state."""
    return tmp_path


def _record(assignment_id: str) -> None:
    proposal = Proposal(
        id=1, machine_name="laptop", repo_name="api",
        issue_number=42, issue_title="t", rationale="r",
        files_likely=["src/a.py"], briefing="b",
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github="acme/api",
    )


def _agent_completed(assignment_id: str, status: str, **overrides) -> dict:
    base = {
        "id": assignment_id,
        "status": status,
        "exit_code": 0 if status == "done" else 1,
        "started_at": 1000.0,
        "finished_at": 1004.0,
        "log_path": f"/var/log/{assignment_id}.log",
        "error": None,
    }
    base.update(overrides)
    return base


class TestDetectTransitions:
    def test_no_dispatched_returns_empty(self, coord_dir: Path, config: Config) -> None:
        assert notify_mod.detect_transitions(config) == []

    def test_done_transition_detected(self, coord_dir: Path, config: Config) -> None:
        _record("abc123")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("abc123", "done")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            transitions = notify_mod.detect_transitions(config)
        assert len(transitions) == 1
        t, _, _ = transitions[0]
        assert t.event == "completion"
        assert t.assignment_id == "abc123"

    def test_failed_transition_detected(self, coord_dir: Path, config: Config) -> None:
        _record("xyz789")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("xyz789", "failed", error="boom")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            transitions = notify_mod.detect_transitions(config)
        assert transitions[0][0].event == "failure"

    def test_already_notified_skipped(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        state_mod.mark_notified("abc", "completion")
        agent_status = {"active": [], "completed": [_agent_completed("abc", "done")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            assert notify_mod.detect_transitions(config) == []

    def test_offline_machine_yields_no_transitions(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        with patch.object(notify_mod, "_agent_status", return_value=None):
            assert notify_mod.detect_transitions(config) == []


class TestRun:
    def test_posts_completion_and_marks_notified(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        agent_status = {"active": [], "completed": [_agent_completed("abc", "done")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck = notify_mod.run(config)
        assert len(posted) == 1
        mock_post.assert_called_once()
        # Comment body includes the completion marker
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Complete" in body
        # Notified ledger persisted
        assert "abc" in state_mod.load_notified()

    def test_idempotent_second_run_posts_nothing(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        agent_status = {"active": [], "completed": [_agent_completed("abc", "done")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
            posted_again, _stuck = notify_mod.run(config)
        # Comment posted exactly once across both runs
        assert mock_post.call_count == 1
        assert posted_again == []

    def test_failure_posts_failure_comment(self, coord_dir: Path, config: Config) -> None:
        _record("xyz")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("xyz", "failed", error="bad config")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Failed" in body
        assert "bad config" in body


class TestBranchCapture:
    def test_branch_stored_in_notified_ledger(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("abc", "done", branch="issue-42-fix")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
        notified = state_mod.load_notified()
        assert notified["abc"]["branch"] == "issue-42-fix"

    def test_branch_propagates_to_build_board(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("abc", "done", branch="issue-42-fix")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
        board = state_mod.build_board()
        assert len(board.completed) == 1
        assert board.completed[0].branch == "issue-42-fix"

    def test_no_branch_still_works(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("abc", "done")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
        board = state_mod.build_board()
        assert len(board.completed) == 1
        assert board.completed[0].branch is None


class TestDispatchedLedger:
    def test_record_and_load_roundtrip(self, coord_dir: Path) -> None:
        _record("abc")
        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["assignment_id"] == "abc"
        assert records[0]["repo_github"] == "acme/api"
        assert records[0]["files_likely"] == ["src/a.py"]


# ── Review assignment notifications ────────────────────────────────────────


def _record_review_assignment(
    assignment_id: str,
    review_target: str,
    *,
    repo_github: str = "acme/api",
    issue_number: int = 42,
) -> None:
    """Insert a review assignment directly into the DB as if it were dispatched."""
    from coord.models import Assignment
    from coord.state import record_dispatched_assignment

    assignment = Assignment(
        assignment_id=assignment_id,
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="[review] Fix the thing",
        briefing="review briefing",
        type="review",
        review_target=review_target,
        dispatched_at=1000.0,
    )
    record_dispatched_assignment(assignment=assignment, repo_github=repo_github)


def _make_log_with_review(tmp_path: Path, verdict: str, body: str) -> str:
    """Write a plain-text log with a structured review block and return the path."""
    log = tmp_path / "review.log"
    log.write_text(
        f"REVIEW_VERDICT: {verdict}\nREVIEW_BODY:\n{body}\nEND_REVIEW\n",
        encoding="utf-8",
    )
    return str(log)


class TestReviewNotify:
    def test_review_approve_posts_pr_review(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """A completed review with 'approve' verdict calls gh pr review --approve."""
        _record_review_assignment("rev1", review_target="99")
        log_path = _make_log_with_review(tmp_path, "approve", "LGTM — all good.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev1", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            posted, _stuck = notify_mod.run(config)

        assert len(posted) == 1
        mock_review.assert_called_once_with("acme/api", 99, "approve", "LGTM — all good.")
        assert "rev1" in state_mod.load_notified()

    def test_review_request_changes_posts_pr_review(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """A completed review with 'request-changes' verdict calls gh pr review --request-changes."""
        _record_review_assignment("rev2", review_target="77")
        log_path = _make_log_with_review(tmp_path, "request-changes", "Bug at line 42.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev2", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            posted, _stuck = notify_mod.run(config)

        assert len(posted) == 1
        mock_review.assert_called_once_with("acme/api", 77, "request-changes", "Bug at line 42.")

    def test_review_fallback_when_log_parse_fails(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """When the log has no structured output, a fallback completion comment is posted."""
        _record_review_assignment("rev3", review_target="55")
        log = tmp_path / "no_verdict.log"
        log.write_text("I read the diff. It looks fine.\n", encoding="utf-8")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev3", "done", log_path=str(log))],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck = notify_mod.run(config)

        assert len(posted) == 1
        mock_review.assert_not_called()
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        assert "could not be extracted" in body
        assert "REVIEW_VERDICT" in body

    def test_review_fallback_when_no_log_path(
        self, coord_dir: Path, config: Config
    ) -> None:
        """When the agent entry has no log_path, a fallback completion comment is posted."""
        _record_review_assignment("rev4", review_target="33")
        agent_status = {
            "active": [],
            # No log_path in the entry
            "completed": [_agent_completed("rev4", "done", log_path=None)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck = notify_mod.run(config)

        assert len(posted) == 1
        mock_review.assert_not_called()
        # Falls back to a completion comment
        mock_post.assert_called_once()

    def test_review_branch_target_posts_issue_comment(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """When review_target is a branch (no PR), findings are posted as an issue comment."""
        _record_review_assignment("rev5", review_target="issue-42-feature")
        log_path = _make_log_with_review(tmp_path, "approve", "Branch looks clean.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev5", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            posted, _stuck = notify_mod.run(config)

        assert len(posted) == 1
        mock_review.assert_not_called()
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        assert "Branch looks clean." in body
        assert "Approved" in body

    def test_review_idempotent(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """Running notify twice for the same review only posts once."""
        _record_review_assignment("rev6", review_target="10")
        log_path = _make_log_with_review(tmp_path, "approve", "Clean.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev6", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
            posted_again, _ = notify_mod.run(config)

        assert posted_again == []
        assert mock_review.call_count == 1

    def test_review_fallback_to_issue_comment_when_pr_review_raises(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """When gh pr review raises (e.g. self-review rejected), findings are
        posted as an issue comment — never silently dropped."""
        _record_review_assignment("rev7", review_target="173")
        log_path = _make_log_with_review(tmp_path, "request-changes", "Bug at line 42.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev7", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch(
                 "coord.notify.github_ops.post_pr_review",
                 side_effect=RuntimeError("GraphQL: Can't request changes on your own pull request"),
             ) as mock_pr_review, \
             patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            posted, _stuck = notify_mod.run(config)

        assert len(posted) == 1
        # PR review was attempted then failed.
        mock_pr_review.assert_called_once_with("acme/api", 173, "request-changes", "Bug at line 42.")
        # Findings posted to the issue as a comment instead.
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        assert "Bug at line 42." in body
        assert "Changes Requested" in body
        # Fallback message should reference the PR number so the reader knows context.
        assert "173" in body
