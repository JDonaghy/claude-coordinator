"""Tests for coord.state — proposal persistence."""

from __future__ import annotations

import time

import pytest

from coord.models import Proposal
from coord.state import (
    save_proposals,
    load_proposals,
    clear_proposals,
    record_dispatched,
    update_assignment_claude_session_id,
)


@pytest.fixture
def proposals() -> list[Proposal]:
    return [
        Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix auth",
            rationale="best fit",
            files_likely=["auth.py"],
            briefing="Fix the auth module",
        ),
        Proposal(
            id=2,
            machine_name="server",
            repo_name="shared",
            issue_number=5,
            issue_title="Add logging",
            rationale="only option",
        ),
    ]


class TestStatePersistence:
    def test_save_and_load_roundtrip(self, coord_db, proposals: list[Proposal]) -> None:
        save_proposals(proposals)
        loaded = load_proposals()

        assert len(loaded) == 2
        assert loaded[0].id == 1
        assert loaded[0].machine_name == "laptop"
        assert loaded[0].files_likely == ["auth.py"]
        assert loaded[1].id == 2
        assert loaded[1].briefing == ""

    def test_load_empty_returns_empty(self, coord_db) -> None:
        assert load_proposals() == []

    def test_clear_removes_proposals(self, coord_db, proposals: list[Proposal]) -> None:
        save_proposals(proposals)
        assert len(load_proposals()) == 2
        clear_proposals()
        assert load_proposals() == []

    def test_clear_when_empty_is_noop(self, coord_db) -> None:
        clear_proposals()  # should not raise
        assert load_proposals() == []

    def test_save_replaces_previous(self, coord_db, proposals: list[Proposal]) -> None:
        save_proposals(proposals)
        save_proposals([proposals[0]])  # save only first
        loaded = load_proposals()
        assert len(loaded) == 1
        assert loaded[0].id == 1


class TestClaudeSessionId:
    """#315: claude_session_id column on the assignments table."""

    def test_schema_has_claude_session_id_column(self, coord_db) -> None:
        """The assignments table must have a claude_session_id column."""
        from coord.db import get_connection
        conn = get_connection()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(assignments)").fetchall()}
        assert "claude_session_id" in cols, (
            "assignments table is missing claude_session_id column — "
            "check _migrate_add_columns in coord/db.py"
        )

    def test_update_assignment_claude_session_id(self, coord_db) -> None:
        """update_assignment_claude_session_id persists the value on the row."""
        # Insert a minimal assignment row using record_dispatched.
        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="Chat test",
            rationale="test",
            briefing="hello",
            type="refinement",
        )
        assignment_id = "test-sess-001"
        record_dispatched(
            assignment_id=assignment_id,
            proposal=proposal,
            repo_github="acme/api",
        )

        # Starts as NULL.
        from coord.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT claude_session_id FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        assert row is not None
        assert row[0] is None

        # Persist the session ID.
        update_assignment_claude_session_id(assignment_id, "ses-xyz-42")

        row = conn.execute(
            "SELECT claude_session_id FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        assert row[0] == "ses-xyz-42"

    def test_update_assignment_claude_session_id_noop_on_missing(self, coord_db) -> None:
        """Calling with a nonexistent assignment_id silently does nothing."""
        update_assignment_claude_session_id("no-such-id", "ses-123")  # must not raise

    def test_update_assignment_claude_session_id_noop_on_empty(self, coord_db) -> None:
        """Calling with empty strings silently does nothing."""
        update_assignment_claude_session_id("", "ses-123")  # must not raise
        update_assignment_claude_session_id("some-id", "")  # must not raise


class TestRecordDispatchedAssignmentBranch:
    """#557: record_dispatched_assignment must persist the branch column so
    coord reattach can find it for the remote push-back finalize."""

    def test_branch_persisted_when_set(self, coord_db) -> None:
        """A fix/rework assignment created with branch=<name> must have that
        branch written to the DB row, not left as NULL."""
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment, get_connection

        assignment = Assignment(
            machine_name="precision",
            repo_name="myrepo",
            issue_number=514,
            issue_title="[fix-1] migrate terminal",
            assignment_id="971a1947ad91",
            status="running",
            branch="issue-514-migrate-terminal-onto-quadraui",
            type="work",
            provider_name="claude-pty",
            dispatched_at=0.0,
        )
        record_dispatched_assignment(
            assignment=assignment,
            repo_github="acme/myrepo",
        )

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            ("971a1947ad91",),
        ).fetchone()
        assert row is not None
        assert row[0] == "issue-514-migrate-terminal-onto-quadraui", (
            "record_dispatched_assignment must persist assignment.branch to the DB"
        )

    def test_branch_none_when_not_set(self, coord_db) -> None:
        """A review assignment (branch=None) must leave the DB branch as NULL."""
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment, get_connection

        assignment = Assignment(
            machine_name="precision",
            repo_name="myrepo",
            issue_number=514,
            issue_title="[review] migrate terminal",
            assignment_id="6873d9f346d0",
            status="running",
            branch=None,
            type="review",
            provider_name="claude-pty",
            dispatched_at=0.0,
        )
        record_dispatched_assignment(
            assignment=assignment,
            repo_github="acme/myrepo",
        )

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            ("6873d9f346d0",),
        ).fetchone()
        assert row is not None
        assert row[0] is None

    def test_redispatch_does_not_clear_existing_branch(self, coord_db) -> None:
        """ON CONFLICT: re-dispatching with branch=None must not overwrite a
        branch that was already recorded (COALESCE guard)."""
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment, get_connection

        # First dispatch — with a branch.
        assignment_v1 = Assignment(
            machine_name="precision",
            repo_name="myrepo",
            issue_number=1,
            issue_title="First dispatch",
            assignment_id="abc123",
            status="running",
            branch="issue-1-some-branch",
            type="work",
            dispatched_at=0.0,
        )
        record_dispatched_assignment(assignment=assignment_v1, repo_github="acme/myrepo")

        # Re-dispatch without a branch (e.g. a retry that doesn't know the branch).
        assignment_v2 = Assignment(
            machine_name="precision",
            repo_name="myrepo",
            issue_number=1,
            issue_title="Re-dispatch",
            assignment_id="abc123",
            status="running",
            branch=None,
            type="work",
            dispatched_at=1.0,
        )
        record_dispatched_assignment(assignment=assignment_v2, repo_github="acme/myrepo")

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?",
            ("abc123",),
        ).fetchone()
        assert row is not None
        assert row[0] == "issue-1-some-branch", (
            "COALESCE must prevent a branch-less re-dispatch from clearing the existing branch"
        )


class TestReconcileBoardWriteHelpers:
    """#611/#609: targeted, idempotent UPDATE helpers used by the
    reconcile-merges sweep."""

    def _insert_done_work(
        self, *, assignment_id: str, branch: str | None, status: str = "done"
    ) -> None:
        from coord.db import get_connection
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        assignment = Assignment(
            machine_name="laptop",
            repo_name="myrepo",
            issue_number=42,
            issue_title="t",
            assignment_id=assignment_id,
            status=status,
            branch=branch,
            type="work",
            dispatched_at=0.0,
        )
        record_dispatched_assignment(
            assignment=assignment, repo_github="acme/myrepo"
        )
        # record_dispatched_assignment always inserts status='running' (it
        # mirrors a fresh dispatch); set the desired terminal status directly.
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET status=? WHERE assignment_id=?",
            (status, assignment_id),
        )
        conn.commit()

    def test_update_assignment_branch_backfills_when_empty(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import update_assignment_branch

        self._insert_done_work(assignment_id="bf1", branch=None)
        update_assignment_branch("bf1", "issue-42-fix")

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?", ("bf1",)
        ).fetchone()
        assert row[0] == "issue-42-fix"

    def test_update_assignment_branch_does_not_clobber_existing(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import update_assignment_branch

        self._insert_done_work(assignment_id="bf2", branch="issue-42-original")
        update_assignment_branch("bf2", "issue-42-other")

        conn = get_connection()
        row = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id=?", ("bf2",)
        ).fetchone()
        assert row[0] == "issue-42-original"

    def test_update_assignment_branch_noop_on_empty_args(self, coord_db) -> None:
        from coord.state import update_assignment_branch

        update_assignment_branch("", "x")  # must not raise
        update_assignment_branch("some-id", "")  # must not raise

    def test_mark_assignment_merged_flips_done(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import mark_assignment_merged

        self._insert_done_work(assignment_id="mg1", branch="issue-42-fix")
        mark_assignment_merged("mg1")

        conn = get_connection()
        row = conn.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", ("mg1",)
        ).fetchone()
        assert row[0] == "merged"

    def test_mark_assignment_merged_only_acts_on_done(self, coord_db) -> None:
        from coord.db import get_connection
        from coord.state import mark_assignment_merged

        self._insert_done_work(
            assignment_id="mg2", branch="issue-42-fix", status="running"
        )
        mark_assignment_merged("mg2")

        conn = get_connection()
        row = conn.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", ("mg2",)
        ).fetchone()
        assert row[0] == "running"

    def test_mark_assignment_merged_noop_on_empty_id(self, coord_db) -> None:
        from coord.state import mark_assignment_merged

        mark_assignment_merged("")  # must not raise
