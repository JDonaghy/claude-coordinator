"""Tests for coord.milestone_order — #768 Phase 0 (work-order parser + DAG +
ready frontier).

Black-box shape per the issue's acceptance criteria: seed a tracking-issue
body (+ a Board for claim detection), assert the parsed DAG and the
computed ready frontier, and assert that a cycle / unknown-target /
non-milestone-issue each raise a clear WorkOrderError.
"""

from __future__ import annotations

import pytest

from coord.milestone_order import (
    Frontier,
    WorkOrder,
    WorkOrderError,
    WorkOrderNode,
    parse_work_order,
    ready_frontier,
    render_work_order,
    replace_work_order_section,
    validate_milestone_membership,
)
from coord.models import Assignment, Board


SAMPLE_BODY = """\
Some intro prose about the milestone.

## Work order
- [ ] #762  {group: A}        # may run concurrently (cohort A)
- [ ] #763  {group: A}
- [ ] #765  {after: #762,#763}   # hard dependency edge
- [ ] #766  {after: #765}
- [ ] #767

## Refs
Not part of the work order.
"""


def _active(
    *, issue: int, repo: str = "api", branch: str | None = None
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name=repo,
        issue_number=issue,
        issue_title="test",
        status="running",
        branch=branch or f"issue-{issue}-fix",
        assignment_id=f"a{issue}",
        type="work",
    )


# ── parse_work_order: happy path ────────────────────────────────────────────


class TestParseWorkOrder:
    def test_parses_groups_and_after_edges(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        assert wo.issue_numbers == (762, 763, 765, 766, 767)
        assert wo.node(762) == WorkOrderNode(762, group="A", after=())
        assert wo.node(763) == WorkOrderNode(763, group="A", after=())
        assert wo.node(765) == WorkOrderNode(765, group=None, after=(762, 763))
        assert wo.node(766) == WorkOrderNode(766, group=None, after=(765,))
        # A bare line (no annotation) means no constraint.
        assert wo.node(767) == WorkOrderNode(767, group=None, after=())

    def test_stops_at_the_next_heading(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        assert 768 not in wo.issue_numbers  # nothing from '## Refs' leaks in

    def test_no_work_order_heading_returns_empty(self) -> None:
        wo = parse_work_order("just some prose, no heading here")
        assert wo == WorkOrder(nodes=())

    def test_checked_item_is_tracked(self) -> None:
        wo = parse_work_order(
            "## Work order\n- [x] #1\n- [ ] #2 {after: #1}\n"
        )
        assert wo.node(1).checked is True
        assert wo.node(2).checked is False

    def test_combined_group_and_after_annotation(self) -> None:
        wo = parse_work_order(
            "## Work order\n- [ ] #1\n- [ ] #2 {group: B, after: #1}\n"
        )
        assert wo.node(2).group == "B"
        assert wo.node(2).after == (1,)


# ── parse_work_order: validation errors ─────────────────────────────────────


class TestParseWorkOrderErrors:
    def test_cycle_raises_clear_error(self) -> None:
        body = "## Work order\n- [ ] #1 {after: #2}\n- [ ] #2 {after: #1}\n"
        with pytest.raises(WorkOrderError, match=r"cycle.*#1.*#2"):
            parse_work_order(body)

    def test_unknown_after_target_raises_clear_error(self) -> None:
        body = "## Work order\n- [ ] #1 {after: #99}\n"
        with pytest.raises(WorkOrderError, match=r"#1.*after:#99.*not declared"):
            parse_work_order(body)

    def test_duplicate_issue_raises(self) -> None:
        body = "## Work order\n- [ ] #1\n- [ ] #1\n"
        with pytest.raises(WorkOrderError, match=r"#1.*more than once"):
            parse_work_order(body)

    def test_unknown_annotation_key_raises(self) -> None:
        body = "## Work order\n- [ ] #1 {bogus: x}\n"
        with pytest.raises(WorkOrderError, match="unknown annotation key"):
            parse_work_order(body)

    def test_malformed_after_entry_raises(self) -> None:
        body = "## Work order\n- [ ] #1 {after: not-a-number}\n"
        with pytest.raises(WorkOrderError, match="malformed after-entry"):
            parse_work_order(body)

    def test_unparseable_checklist_line_raises(self) -> None:
        body = "## Work order\n- this is not a work-order item\n"
        with pytest.raises(WorkOrderError, match="unparseable line"):
            parse_work_order(body)

    def test_self_loop_is_a_cycle(self) -> None:
        body = "## Work order\n- [ ] #1 {after: #1}\n"
        with pytest.raises(WorkOrderError, match="cycle"):
            parse_work_order(body)


# ── validate_milestone_membership ───────────────────────────────────────────


class TestValidateMilestoneMembership:
    def test_all_nodes_under_milestone_passes(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        validate_milestone_membership(wo, {762, 763, 765, 766, 767})  # no raise

    def test_foreign_issue_raises_clear_error(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        with pytest.raises(WorkOrderError, match=r"#767.*not an issue under this milestone"):
            validate_milestone_membership(wo, {762, 763, 765, 766})

    def test_closed_dependency_still_counts_as_membership(self) -> None:
        """A node that has already closed is still a valid DAG member —
        membership doesn't require currently-open state (see module
        docstring design note)."""
        body = "## Work order\n- [ ] #1\n- [ ] #2 {after: #1}\n"
        wo = parse_work_order(body)
        # #1 closed, #2 still open — both are legitimately "under the
        # milestone"; the caller supplies membership regardless of state.
        validate_milestone_membership(wo, {1, 2})


# ── ready_frontier ───────────────────────────────────────────────────────────


class TestReadyFrontier:
    def test_frontier_with_empty_board_and_no_terminal_issues(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues=set(),
            branch_lookup=lambda repo, n: [],
        )
        # Only nodes with a fully-satisfied (empty) after-set are ready.
        ready_numbers = {e.issue_number for e in frontier.ready}
        assert ready_numbers == {762, 763, 767}
        blocked_numbers = {b.issue_number for b in frontier.blocked}
        assert blocked_numbers == {765, 766}
        blocked_by = {b.issue_number: b.waiting_on_deps for b in frontier.blocked}
        assert blocked_by[765] == (762, 763)
        assert blocked_by[766] == (765,)

    def test_frontier_advances_as_deps_go_terminal(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues={762, 763},
            branch_lookup=lambda repo, n: [],
        )
        ready_numbers = {e.issue_number for e in frontier.ready}
        # 762/763 are terminal (dropped from the frontier entirely), 765's
        # after-set is now fully satisfied, 766 still waits on 765, 767 is
        # unconstrained and stays ready.
        assert ready_numbers == {765, 767}
        blocked_numbers = {b.issue_number for b in frontier.blocked}
        assert blocked_numbers == {766}

    def test_claimed_node_is_blocked_not_ready(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        board.active.append(_active(issue=762))
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues=set(),
            branch_lookup=lambda repo, n: [],
        )
        ready_numbers = {e.issue_number for e in frontier.ready}
        assert 762 not in ready_numbers
        blocked = {b.issue_number: b for b in frontier.blocked}
        assert blocked[762].claim is not None
        assert blocked[762].claim.source == "board"
        assert "claimed" in blocked[762].reason

    def test_remote_branch_claim_blocks_via_branch_lookup(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues=set(),
            branch_lookup=lambda repo, n: (
                ["issue-763-already-started"] if n == 763 else []
            ),
        )
        ready_numbers = {e.issue_number for e in frontier.ready}
        assert 763 not in ready_numbers
        blocked = {b.issue_number: b for b in frontier.blocked}
        assert blocked[763].claim.source == "remote_branch"

    def test_conflict_checker_blocks_a_node(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues=set(),
            branch_lookup=lambda repo, n: [],
            conflict_checker=lambda n: n == 767,
        )
        ready_numbers = {e.issue_number for e in frontier.ready}
        assert 767 not in ready_numbers
        assert ready_numbers == {762, 763}
        blocked = {b.issue_number: b for b in frontier.blocked}
        assert blocked[767].conflict is True
        assert blocked[767].reason == "conflict-blocked"

    def test_fully_terminal_work_order_yields_empty_frontier(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        board = Board()
        frontier = ready_frontier(
            wo,
            board,
            repo_name="api",
            repo_github="acme/api",
            terminal_issues={762, 763, 765, 766, 767},
            branch_lookup=lambda repo, n: [],
        )
        assert frontier == Frontier(ready=(), blocked=())


# ── render_work_order / replace_work_order_section (#770 Phase 2 write path) ─


class TestRenderWorkOrder:
    def test_round_trips_through_parse(self) -> None:
        wo = parse_work_order(SAMPLE_BODY)
        rendered = render_work_order(wo)
        reparsed = parse_work_order("## Work order\n" + rendered)
        assert reparsed == wo

    def test_renders_group_and_after_annotations(self) -> None:
        wo = WorkOrder(nodes=(
            WorkOrderNode(1, group="A"),
            WorkOrderNode(2, after=(1,)),
            WorkOrderNode(3),
        ))
        rendered = render_work_order(wo)
        assert rendered == (
            "- [ ] #1  {group: A}\n"
            "- [ ] #2  {after: #1}\n"
            "- [ ] #3"
        )

    def test_renders_checked_box(self) -> None:
        wo = WorkOrder(nodes=(WorkOrderNode(1, checked=True),))
        assert render_work_order(wo) == "- [x] #1"

    def test_empty_work_order_renders_empty_string(self) -> None:
        assert render_work_order(WorkOrder()) == ""


class TestReplaceWorkOrderSection:
    def test_replaces_existing_section_in_place(self) -> None:
        body = (
            "Intro.\n\n"
            "## Work order\n"
            "- [ ] #1\n\n"
            "## Refs\n"
            "other stuff\n"
        )
        new_body = replace_work_order_section(body, "- [ ] #1  {group: A}\n- [ ] #2  {after: #1}")
        assert "## Refs\nother stuff" in new_body
        assert "Intro." in new_body
        wo = parse_work_order(new_body)
        assert wo.issue_numbers == (1, 2)
        assert wo.node(2).after == (1,)
        # Old single-line block is gone, not duplicated alongside the new one.
        assert new_body.count("## Work order") == 1

    def test_appends_section_when_absent(self) -> None:
        body = "Just prose, no work order yet.\n"
        new_body = replace_work_order_section(body, "- [ ] #1")
        assert "Just prose, no work order yet." in new_body
        wo = parse_work_order(new_body)
        assert wo.issue_numbers == (1,)

    def test_appends_section_to_empty_body(self) -> None:
        new_body = replace_work_order_section("", "- [ ] #1")
        wo = parse_work_order(new_body)
        assert wo.issue_numbers == (1,)

    def test_is_idempotent(self) -> None:
        body = "## Work order\n- [ ] #1  {group: A}\n"
        once = replace_work_order_section(body, "- [ ] #1  {group: A}")
        twice = replace_work_order_section(once, "- [ ] #1  {group: A}")
        assert once == twice

    def test_round_trip_with_render_work_order(self) -> None:
        """render → replace → parse recovers the same WorkOrder (the shape
        `coord milestone write-order` actually exercises)."""
        wo = parse_work_order(SAMPLE_BODY)
        tracking_body = "Milestone plan.\n\n## Work order\n(stale)\n"
        new_body = replace_work_order_section(tracking_body, render_work_order(wo))
        assert parse_work_order(new_body) == wo
        assert "Milestone plan." in new_body

    def test_preserves_content_after_next_heading_of_any_level(self) -> None:
        body = "## Work order\n- [ ] #1\n\n### Sub-heading\nkept\n"
        new_body = replace_work_order_section(body, "- [ ] #1\n- [ ] #2")
        assert "### Sub-heading\nkept" in new_body
        assert parse_work_order(new_body).issue_numbers == (1, 2)
