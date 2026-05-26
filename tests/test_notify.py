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
        # #248: the body is now prefixed with a machine-readable header.
        mock_review.assert_called_once()
        repo_arg, pr_arg, verdict_arg, body_arg = mock_review.call_args.args
        assert (repo_arg, pr_arg, verdict_arg) == ("acme/api", 99, "approve")
        assert body_arg.startswith("<!-- coord:review verdict=approve")
        assert "LGTM — all good." in body_arg
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
        # #248: header is prefixed; verdict + prose preserved.
        mock_review.assert_called_once()
        _, _, verdict_arg, body_arg = mock_review.call_args.args
        assert verdict_arg == "request-changes"
        assert body_arg.startswith("<!-- coord:review verdict=request-changes")
        assert "Bug at line 42." in body_arg

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
        # PR review was attempted then failed.  #248: body carries the header.
        mock_pr_review.assert_called_once()
        _, _, verdict_arg, pr_body = mock_pr_review.call_args.args
        assert verdict_arg == "request-changes"
        assert pr_body.startswith("<!-- coord:review verdict=request-changes")
        assert "Bug at line 42." in pr_body
        # Findings posted to the issue as a comment instead.
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        assert "Bug at line 42." in body
        assert "Changes Requested" in body
        # The fallback issue comment also carries the header so coord/TUI
        # can surface the verdict without re-ingesting prose.
        assert "<!-- coord:review verdict=request-changes" in body
        # Fallback message should reference the PR number so the reader knows context.
        assert "173" in body

    def test_review_posted_at_set_on_success(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """review_posted_at is set on the assignment when findings are successfully posted."""
        _record_review_assignment("rev8", review_target="10")
        log_path = _make_log_with_review(tmp_path, "approve", "Looks good.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev8", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        # Assignment should have review_posted_at set
        from coord.state import build_board
        board = build_board()
        rev = next((a for a in board.completed if a.assignment_id == "rev8"), None)
        assert rev is not None
        assert rev.review_posted_at is not None

    def test_review_posted_at_not_set_on_fallback(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """review_posted_at stays None when only a fallback comment (no findings) is posted."""
        _record_review_assignment("rev9", review_target="20")
        log = tmp_path / "no_verdict.log"
        log.write_text("I looked at the diff. Seems fine.\n", encoding="utf-8")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev9", "done", log_path=str(log))],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        from coord.state import build_board
        board = build_board()
        rev = next((a for a in board.completed if a.assignment_id == "rev9"), None)
        assert rev is not None
        assert rev.review_posted_at is None


# ── Orphaned review findings ────────────────────────────────────────────────


class TestPostOrphanedReviewFindings:
    def test_posts_orphaned_review_findings(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """post_orphaned_review_findings posts findings when the agent has the log."""
        _record_review_assignment("orphan1", review_target="50")
        # Mark done in DB without going through notify (simulates manual mark or missed transition).
        state_mod.mark_notified.__module__  # ensure module loaded
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan1'")
        conn.commit()

        log_path = _make_log_with_review(tmp_path, "approve", "Orphaned LGTM.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan1", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.notify.github_ops.post_issue_comment"):
            posted = notify_mod.post_orphaned_review_findings(config)

        assert "orphan1" in posted
        # #248: header prefixed onto the orphan-path body as well.
        mock_review.assert_called_once()
        _, _, verdict_arg, body_arg = mock_review.call_args.args
        assert verdict_arg == "approve"
        assert body_arg.startswith("<!-- coord:review verdict=approve")
        assert "Orphaned LGTM." in body_arg

        # review_posted_at should now be set
        from coord.state import load_done_reviews_needing_post
        still_pending = load_done_reviews_needing_post()
        assert not any(r["assignment_id"] == "orphan1" for r in still_pending)

    def test_skips_when_agent_offline(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """post_orphaned_review_findings silently skips when agent is offline."""
        _record_review_assignment("orphan2", review_target="55")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan2'")
        conn.commit()

        with patch.object(notify_mod, "_agent_status", return_value=None):
            posted = notify_mod.post_orphaned_review_findings(config)

        assert posted == []
        from coord.state import load_done_reviews_needing_post
        still_pending = load_done_reviews_needing_post()
        assert any(r["assignment_id"] == "orphan2" for r in still_pending)

    def test_skips_when_no_structured_findings(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """post_orphaned_review_findings skips when log has no structured output."""
        _record_review_assignment("orphan3", review_target="60")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan3'")
        conn.commit()

        log = tmp_path / "no_verdict.log"
        log.write_text("Just looking at the diff.\n", encoding="utf-8")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan3", "done", log_path=str(log))],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review:
            posted = notify_mod.post_orphaned_review_findings(config)

        assert posted == []
        mock_review.assert_not_called()

    def test_idempotent_after_posting(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """post_orphaned_review_findings is idempotent — once review_posted_at is set, skips."""
        _record_review_assignment("orphan4", review_target="70")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan4'")
        conn.commit()

        log_path = _make_log_with_review(tmp_path, "approve", "Good.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan4", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.notify.github_ops.post_issue_comment"):
            notify_mod.post_orphaned_review_findings(config)
            posted_again = notify_mod.post_orphaned_review_findings(config)

        assert posted_again == []
        assert mock_review.call_count == 1

    def test_adds_notification_record_for_truly_orphaned(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """Assignments with no notification record get one added after orphan posting."""
        _record_review_assignment("orphan5", review_target="80")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan5'")
        conn.commit()

        # Confirm no notification record yet
        assert "orphan5" not in state_mod.load_notified()

        log_path = _make_log_with_review(tmp_path, "request-changes", "Has a bug.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan5", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.notify.github_ops.post_issue_comment"):
            notify_mod.post_orphaned_review_findings(config)

        assert "orphan5" in state_mod.load_notified()

    def test_run_calls_orphaned_posting(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """notify.run() also invokes orphaned-findings posting, not just direct transitions."""
        _record_review_assignment("orphan6", review_target="90")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan6'")
        conn.commit()

        log_path = _make_log_with_review(tmp_path, "approve", "All clear.")
        # Agent says nothing new (no direct transitions for orphan6)
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan6", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        # Findings should have been posted via the orphaned path inside run()
        # #248: header is prefixed; preserve original prose.
        mock_review.assert_called_once()
        _, _, verdict_arg, body_arg = mock_review.call_args.args
        assert verdict_arg == "approve"
        assert body_arg.startswith("<!-- coord:review verdict=approve")
        assert "All clear." in body_arg

    def test_load_done_reviews_needing_post_filters_by_repo(
        self, coord_dir: Path, config: Config
    ) -> None:
        """load_done_reviews_needing_post respects the optional repo_name filter."""
        _record_review_assignment("rp1", review_target="1", repo_github="acme/api")
        _record_review_assignment(
            "rp2", review_target="2",
            repo_github="acme/other",
            issue_number=43,
        )
        # Override repo_name for rp2
        from coord.db import get_connection
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET repo_name='other', repo_github='acme/other', "
            "status='done', finished_at=1234.0 WHERE assignment_id='rp2'"
        )
        conn.execute(
            "UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='rp1'"
        )
        conn.commit()

        from coord.state import load_done_reviews_needing_post
        api_only = load_done_reviews_needing_post(repo_name="api")
        all_repos = load_done_reviews_needing_post()

        assert all(r["assignment_id"] == "rp1" for r in api_only)
        assert len(all_repos) == 2
