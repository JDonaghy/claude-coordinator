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
