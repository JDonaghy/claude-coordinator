"""Tests for the #1036 audit trail: the `audit_log` schema, `record_audit()`
(coord/audit.py), and the hooks at the state._*_local / issue_store write
choke points.

Scope per the issue's acceptance bar:
- exactly one `audit_log` row per transition, with the right
  event_type/actor/assignment_id/tier;
- `details_json` round-trips;
- `record_audit` swallows a bad write without breaking the board write it
  rode on.
"""

from __future__ import annotations

import json

import pytest

from coord.audit import record_audit
from coord.models import Proposal
from coord.state import record_dispatched, record_test_verdict, mark_notified


def _audit_rows(conn, *, assignment_id: str | None = None) -> list:
    if assignment_id is None:
        return conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM audit_log WHERE assignment_id=? ORDER BY id", (assignment_id,)
    ).fetchall()


def _dispatch(coord_db, *, assignment_id: str = "aid-1", issue_number: int = 42) -> None:
    proposal = Proposal(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="Fix auth",
        rationale="best fit",
        briefing="Fix the auth module",
    )
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github="acme/api",
    )


class TestSchema:
    def test_audit_log_table_exists_with_expected_columns(self, coord_db) -> None:
        cols = {row[1] for row in coord_db.execute("PRAGMA table_info(audit_log)").fetchall()}
        assert cols == {
            "id", "ts", "tier", "category", "event_type", "actor",
            "repo", "issue", "assignment_id", "machine", "summary", "details_json",
        }

    def test_indexes_exist(self, coord_db) -> None:
        names = {
            row[1] for row in coord_db.execute("PRAGMA index_list(audit_log)").fetchall()
        }
        assert "idx_audit_log_ts" in names
        assert "idx_audit_log_assignment" in names


class TestRecordAudit:
    def test_basic_insert(self, coord_db) -> None:
        record_audit(
            tier="business",
            category="test",
            event_type="test_passed",
            actor="user",
            summary="Test passed",
            repo="api",
            issue=42,
            assignment_id="aid-1",
            machine="laptop",
        )
        rows = _audit_rows(coord_db)
        assert len(rows) == 1
        row = rows[0]
        assert row["tier"] == "business"
        assert row["category"] == "test"
        assert row["event_type"] == "test_passed"
        assert row["actor"] == "user"
        assert row["repo"] == "api"
        assert row["issue"] == 42
        assert row["assignment_id"] == "aid-1"
        assert row["machine"] == "laptop"
        assert row["ts"] is not None

    def test_details_json_roundtrips(self, coord_db) -> None:
        details = {"test_reason": "flaky assertion", "count": 3, "nested": {"a": [1, 2]}}
        record_audit(
            tier="business",
            category="test",
            event_type="test_failed",
            actor="user",
            summary="Test failed",
            assignment_id="aid-1",
            details=details,
        )
        row = _audit_rows(coord_db)[0]
        assert json.loads(row["details_json"]) == details

    def test_details_none_stores_null(self, coord_db) -> None:
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="coordinator", summary="merged",
        )
        row = _audit_rows(coord_db)[0]
        assert row["details_json"] is None

    def test_swallows_bad_write_without_raising(self, coord_db, monkeypatch) -> None:
        def _boom():
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr("coord.audit.get_connection", _boom)
        # Must not raise.
        record_audit(
            tier="business", category="test", event_type="test_passed",
            actor="user", summary="should not blow up",
        )

    def test_bad_write_does_not_break_the_board_write_it_rode_on(
        self, coord_db, monkeypatch
    ) -> None:
        """The acceptance-bar scenario: record_test_verdict's assignments
        UPDATE must succeed even when the audit_log write fails."""
        _dispatch(coord_db, assignment_id="aid-1")

        def _boom():
            raise RuntimeError("audit_log write exploded")

        monkeypatch.setattr("coord.audit.get_connection", _boom)

        # Must not raise, despite the audit layer being completely broken.
        record_test_verdict(assignment_id="aid-1", test_state="passed")

        row = coord_db.execute(
            "SELECT test_state FROM assignments WHERE assignment_id=?", ("aid-1",)
        ).fetchone()
        assert row["test_state"] == "passed"
        # No test-verdict audit row landed, since the write genuinely
        # failed (the earlier dispatch's own row, written before the
        # monkeypatch took effect, is unaffected).
        assert [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "test"
        ] == []


class TestHookedTransitions:
    """One audit_log row per real transition at the state.py choke points."""

    def test_dispatch_writes_one_row(self, coord_db) -> None:
        _dispatch(coord_db, assignment_id="aid-1")
        rows = _audit_rows(coord_db, assignment_id="aid-1")
        assert len(rows) == 1
        assert rows[0]["tier"] == "business"
        assert rows[0]["category"] == "dispatch"
        assert rows[0]["event_type"] == "dispatched"
        assert rows[0]["repo"] == "api"
        assert rows[0]["issue"] == 42

    def test_test_verdict_writes_one_row_with_right_fields(self, coord_db) -> None:
        _dispatch(coord_db, assignment_id="aid-1")
        record_test_verdict(
            assignment_id="aid-1", test_state="passed", test_reason=None,
        )
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "test"
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row["event_type"] == "test_passed"
        assert row["actor"] == "user"
        assert row["assignment_id"] == "aid-1"
        assert row["tier"] == "business"

    def test_test_verdict_failed_reason_in_details(self, coord_db) -> None:
        _dispatch(coord_db, assignment_id="aid-1")
        record_test_verdict(
            assignment_id="aid-1", test_state="failed", test_reason="boom",
        )
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == "test_failed"
        ]
        assert len(rows) == 1
        assert json.loads(rows[0]["details_json"])["test_reason"] == "boom"

    def test_mark_notified_completion_writes_one_row(self, coord_db) -> None:
        _dispatch(coord_db, assignment_id="aid-1")
        from coord.comments import EVENT_COMPLETION

        mark_notified("aid-1", EVENT_COMPLETION, branch="issue-42-fix")
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == EVENT_COMPLETION
        ]
        assert len(rows) == 1
        assert rows[0]["actor"] == "worker"
        assert rows[0]["repo"] == "api"
        assert rows[0]["issue"] == 42

    def test_mark_notified_stuck_strips_composite_key(self, coord_db) -> None:
        _dispatch(coord_db, assignment_id="aid-1")
        from coord.comments import EVENT_STUCK

        mark_notified("aid-1:stuck", EVENT_STUCK)
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == EVENT_STUCK
        ]
        assert len(rows) == 1
        assert rows[0]["actor"] == "daemon"
        assert rows[0]["repo"] == "api"
        assert rows[0]["issue"] == 42

    def test_mark_assignment_merged_writes_one_row_only_on_real_transition(
        self, coord_db
    ) -> None:
        from coord.state import mark_assignment_merged

        _dispatch(coord_db, assignment_id="aid-1")
        # Not 'done' yet — mark_assignment_merged is a no-op, no audit row.
        mark_assignment_merged("aid-1")
        assert [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "merge"
        ] == []

        coord_db.execute(
            "UPDATE assignments SET status='done' WHERE assignment_id=?", ("aid-1",)
        )
        coord_db.commit()
        mark_assignment_merged("aid-1")
        merge_rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "merge"
        ]
        assert len(merge_rows) == 1
        assert merge_rows[0]["event_type"] == "merged"

        # Idempotent: calling again after it's already merged writes no
        # second row.
        mark_assignment_merged("aid-1")
        merge_rows_2 = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["category"] == "merge"
        ]
        assert len(merge_rows_2) == 1

    def test_update_assignment_branch_writes_one_row_and_is_idempotent(
        self, coord_db
    ) -> None:
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", rationale="x",
        )
        # Dispatch with no target_branch so the row gets the auto-slugified
        # branch (non-empty) — use record_dispatched_assignment instead to
        # land a NULL branch, matching #611's scenario.
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment, update_assignment_branch

        record_dispatched_assignment(
            assignment=Assignment(
                assignment_id="aid-2", machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth", type="work", branch=None,
            ),
            repo_github="acme/api",
        )
        update_assignment_branch("aid-2", "issue-42-fix-auth")
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-2")
            if r["event_type"] == "branch_set"
        ]
        assert len(rows) == 1

        # Second call is a no-op (branch already set) — no new row.
        update_assignment_branch("aid-2", "issue-42-fix-auth")
        rows_2 = [
            r for r in _audit_rows(coord_db, assignment_id="aid-2")
            if r["event_type"] == "branch_set"
        ]
        assert len(rows_2) == 1

    def test_dispatch_duplicate_assignment_id_writes_no_second_row(
        self, coord_db
    ) -> None:
        """#1036 fix review finding 1: a second dispatch with the same
        assignment_id hits ON CONFLICT DO NOTHING — the INSERT is a no-op,
        so it must not emit a phantom second 'dispatched' row."""
        _dispatch(coord_db, assignment_id="aid-1", issue_number=42)
        rows = _audit_rows(coord_db, assignment_id="aid-1")
        assert len(rows) == 1

        # Same assignment_id, different issue — simulates a retry/collision;
        # the INSERT no-ops so the original row's issue must be untouched
        # and no new audit row should appear.
        _dispatch(coord_db, assignment_id="aid-1", issue_number=99)
        rows_2 = _audit_rows(coord_db, assignment_id="aid-1")
        assert len(rows_2) == 1
        assert rows_2[0]["issue"] == 42

    def test_launch_failure_reason_no_audit_for_unknown_assignment_id(
        self, coord_db
    ) -> None:
        """#1036 fix review finding 2: the UPDATE ... WHERE assignment_id=?
        touches no row for a bad/stale id — must not emit an audit row for
        a transition that didn't happen."""
        from coord.state import set_assignment_failure_reason

        set_assignment_failure_reason("no-such-assignment", "boom")
        rows = [
            r for r in _audit_rows(coord_db)
            if r["event_type"] == "launch_failed"
        ]
        assert rows == []

    def test_launch_failure_reason_writes_one_row_with_coordinator_actor(
        self, coord_db
    ) -> None:
        """#1036 fix review finding 3: this is a coordinator/launcher-side
        backstop (fires before the worker session starts), not a worker
        self-report — actor should be 'coordinator'."""
        from coord.state import set_assignment_failure_reason

        _dispatch(coord_db, assignment_id="aid-1")
        set_assignment_failure_reason("aid-1", "worktree add failed")
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == "launch_failed"
        ]
        assert len(rows) == 1
        assert rows[0]["actor"] == "coordinator"
        assert rows[0]["category"] == "error"

    def test_review_findings_retry_with_same_verdict_writes_one_row(
        self, coord_db
    ) -> None:
        """#1036 fix review finding 4: a retried write of the identical
        (verdict, body) pair — the shape of issue_store._persist_review_
        verdict's retry loop when a successful UPDATE is followed by a
        readback that looks mismatched — must not double the audit row."""
        from coord.state import update_assignment_review_findings

        _dispatch(coord_db, assignment_id="aid-1")
        update_assignment_review_findings(
            "aid-1", verdict="approved", body="looks good"
        )
        rows = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == "review_approved"
        ]
        assert len(rows) == 1

        # Retry with the identical verdict + body — no second row.
        update_assignment_review_findings(
            "aid-1", verdict="approved", body="looks good"
        )
        rows_2 = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] == "review_approved"
        ]
        assert len(rows_2) == 1

        # A genuinely new verdict/body IS a real transition — new row.
        update_assignment_review_findings(
            "aid-1", verdict="request-changes", body="needs work"
        )
        rows_3 = [
            r for r in _audit_rows(coord_db, assignment_id="aid-1")
            if r["event_type"] in ("review_approved", "review_request-changes")
        ]
        assert len(rows_3) == 2
