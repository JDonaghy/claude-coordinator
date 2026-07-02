"""Tests for coord/stage_projection.py (#550).

Truth-table cases mirror the Rust behaviour documented in
``tui/src/app/pipeline.rs`` (``stage_status_for``, ``merge_stage_status_for``,
``test_stage_status_for``, ``issue_has_any_approved_review``) so both sides
encode the same expected outcomes for the same inputs.
"""

from __future__ import annotations

from coord import stage_projection as sp
from coord.merge_queue import QueuedMerge
from coord.models import Assignment


def _work(**kw) -> Assignment:
    base = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="t",
        type="work",
        status="done",
    )
    base.update(kw)
    return Assignment(**base)


def _review(**kw) -> Assignment:
    base = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="t",
        type="review",
        status="done",
    )
    base.update(kw)
    return Assignment(**base)


def _entry(**kw) -> QueuedMerge:
    base = dict(
        assignment_id="w1",
        repo_name="api",
        repo_github="acme/api",
        branch="issue-1-impl",
        target_branch="main",
        issue_number=1,
        issue_title="t",
    )
    base.update(kw)
    return QueuedMerge(**base)


# ── stage_status_for: generic stage ─────────────────────────────────────────


def test_stage_status_running_is_active():
    a = [_review(status="running", dispatched_at=1.0)]
    assert sp.stage_status_for(a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.ACTIVE


def test_stage_status_review_approve_is_done():
    a = [_review(status="done", review_verdict="approve", dispatched_at=1.0)]
    assert sp.stage_status_for(a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.DONE


def test_stage_status_review_request_changes_is_failed():
    a = [_review(status="done", review_verdict="request-changes", dispatched_at=1.0)]
    assert sp.stage_status_for(a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.FAILED


def test_stage_status_review_no_verdict_is_failed():
    """#812: a terminal done row with no verdict is a dead end, not in-progress."""
    a = [_review(status="done", review_verdict=None, dispatched_at=1.0)]
    assert sp.stage_status_for(a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.FAILED


def test_stage_status_no_assignment_open_issue_is_pending():
    assert sp.stage_status_for([], "review", stage_names=["work", "review"], is_closed=False, require_plan=False) == sp.PENDING


def test_stage_status_no_assignment_closed_issue_is_skipped():
    assert sp.stage_status_for([], "review", stage_names=["work", "review"], is_closed=True, require_plan=False) == sp.SKIPPED


def test_stage_status_stale_when_upstream_redispatched():
    """#193: a Done verdict against an older revision renders Stale."""
    a = [
        _work(status="done", dispatched_at=1.0),
        _review(status="done", review_verdict="approve", dispatched_at=2.0),
        _work(status="running", dispatched_at=5.0),  # re-dispatched after review
    ]
    # "work" is upstream of "review" in stage_names.
    assert sp.stage_status_for(
        a, "review", stage_names=["work", "review"], is_closed=False, require_plan=False
    ) == sp.STALE


# ── merge_stage_status_for ──────────────────────────────────────────────────


def test_merge_stage_active_conflict_fix_wins():
    a = [Assignment(machine_name="m", repo_name="api", issue_number=1, issue_title="t", type="conflict-fix", status="running")]
    entry = _entry(state="failed")
    assert sp.merge_stage_status_for(a, entry, is_closed=False) == sp.ACTIVE


def test_merge_stage_merged_entry_is_done():
    entry = _entry(state="merged")
    assert sp.merge_stage_status_for([], entry, is_closed=False) == sp.DONE


def test_merge_stage_open_entry_is_active():
    entry = _entry(state="open")
    assert sp.merge_stage_status_for([], entry, is_closed=False) == sp.ACTIVE


def test_merge_stage_human_required_is_failed():
    entry = _entry(state="human_required")
    assert sp.merge_stage_status_for([], entry, is_closed=False) == sp.FAILED


def test_merge_stage_pruned_entry_falls_back_to_merged_work_assignment():
    """#775: the queue row can be pruned after the work assignment flips to
    status='merged' — that's still sufficient evidence Merge is Done."""
    a = [_work(status="merged")]
    assert sp.merge_stage_status_for(a, None, is_closed=False) == sp.DONE


def test_merge_stage_no_entry_open_issue_is_pending():
    assert sp.merge_stage_status_for([], None, is_closed=False) == sp.PENDING


def test_merge_stage_no_entry_closed_issue_is_skipped():
    assert sp.merge_stage_status_for([], None, is_closed=True) == sp.SKIPPED


# ── test_stage_status_for ───────────────────────────────────────────────────


def test_test_stage_work_not_done_is_pending():
    a = [_work(status="running")]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.PENDING


def test_test_stage_passed_verdict_is_done():
    a = [_work(status="done", test_state="passed")]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.DONE


def test_test_stage_failed_verdict_is_failed():
    a = [_work(status="done", test_state="failed")]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.FAILED


def test_test_stage_active_smoke_session_overrides_prior_pass():
    """#585: an in-flight manual smoke session keeps Test Active even over a
    prior passed verdict."""
    a = [
        _work(status="done", test_state="passed", dispatched_at=1.0),
        Assignment(machine_name="m", repo_name="api", issue_number=1, issue_title="t", type="smoke", status="running"),
    ]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.ACTIVE


def test_test_stage_bounce_fix_work_inherits_prior_passed_verdict():
    """#310: a bounce-created fix-work assignment with empty test_state
    doesn't strand Test at Pending — the most recent assignment *carrying* a
    verdict wins."""
    a = [
        _work(status="done", test_state="passed", dispatched_at=1.0),
        _work(status="done", test_state=None, dispatched_at=2.0, assignment_id="fix1"),
    ]
    assert sp.test_stage_status_for(a, is_closed=False, require_plan=False) == sp.DONE


def test_test_stage_no_verdict_no_work_yet_running_is_pending_not_skipped():
    assert sp.test_stage_status_for([], is_closed=False, require_plan=False) == sp.PENDING


def test_test_stage_no_work_closed_issue_is_skipped():
    assert sp.test_stage_status_for([], is_closed=True, require_plan=False) == sp.SKIPPED


# ── issue_has_any_approved_review ───────────────────────────────────────────


def test_approved_review_linked_to_work_id():
    a = [
        _work(assignment_id="w1", status="done"),
        _review(review_of_assignment_id="w1", review_verdict="approve"),
    ]
    assert sp.issue_has_any_approved_review(a) is True


def test_approved_review_self_stamped_on_work():
    """#331: verdict stamped directly on the work row (no separate review worker)."""
    a = [_work(assignment_id="w1", status="done", review_verdict="approve")]
    assert sp.issue_has_any_approved_review(a) is True


def test_approved_review_seed_work_id_covers_pruned_row():
    """#292: entry is keyed to a work id whose row has been pruned from the
    board — seed_work_id still finds an approval linked to it."""
    a = [_review(review_of_assignment_id="pruned-w1", review_verdict="approve")]
    assert sp.issue_has_any_approved_review(a, seed_work_id="pruned-w1") is True


def test_no_approved_review_returns_false():
    a = [_work(assignment_id="w1", status="done")]
    assert sp.issue_has_any_approved_review(a) is False


def test_request_changes_is_not_approved():
    a = [
        _work(assignment_id="w1", status="done"),
        _review(review_of_assignment_id="w1", review_verdict="request-changes"),
    ]
    assert sp.issue_has_any_approved_review(a) is False


# ── compute_board_stage_projection ──────────────────────────────────────────


def test_compute_board_stage_projection_covers_issue_and_merge_state():
    issues = [{"repo_name": "api", "number": 1, "title": "t", "state": "open"}]
    assignments = [
        _work(assignment_id="w1", status="done", test_state="passed", dispatched_at=1.0),
        _review(review_of_assignment_id="w1", review_verdict="approve", dispatched_at=2.0),
    ]
    mq_items = [_entry(state="open")]
    out = sp.compute_board_stage_projection(
        issues=issues,
        assignments=assignments,
        merge_queue_items=mq_items,
        default_gates=["test", "review", "merge"],
    )
    assert len(out) == 1
    entry = out[0]
    assert entry["repo_name"] == "api"
    assert entry["issue_number"] == 1
    assert entry["has_approved_review"] is True
    assert entry["stages"]["work"] == sp.DONE
    assert entry["stages"]["test"] == sp.DONE
    assert entry["stages"]["review"] == sp.DONE
    assert entry["stages"]["merge"] == sp.ACTIVE


def test_compute_board_stage_projection_includes_closed_issue_with_assignments_only():
    """An issue with assignment history but absent from the issues table
    (e.g. pruned/never synced) still gets a projection, treated as open."""
    assignments = [_work(assignment_id="w1", status="done")]
    out = sp.compute_board_stage_projection(
        issues=[],
        assignments=assignments,
        merge_queue_items=[],
        default_gates=["test", "review", "merge"],
    )
    assert len(out) == 1
    assert out[0]["issue_number"] == 1
