"""Unit tests for coord/drive_queue.py — the pure half of the drive queue (#1754).

The CLI-level suite (tests/test_cli_drive_queue.py) is the acceptance bar; this
file pins the decisions themselves, the same way tests/test_drive.py pins
``coord.drive.decide`` rather than ``Driver.run``. The two rules that get the
most attention here are the ones that caused real incidents:

* capacity counted from BOARD state, so a drive whose observer hit its
  ``EXIT_DEADLINE`` (#1660) still occupies a slot (2026-08-01);
* unsatisfiable vs merely-unsatisfied, so a pre-req that will never land
  escalates instead of deferring forever.
"""

from __future__ import annotations

import pytest

from coord.drive_queue import (
    DEFAULT_MAX_ATTEMPTS,
    STATE_BLOCKED,
    STATE_DONE,
    STATE_RUNNING,
    STATE_WAITING,
    BoardView,
    IssueFacts,
    QueueEntry,
    QueueError,
    build_board_view,
    entries_from_rows,
    entry_key,
    find_cycle,
    parse_after_spec,
    parse_key,
    plan_tick,
    render_plan,
    validate_enqueue,
)

REPO = "claude-coordinator"


def entry(issue: int, **kw) -> QueueEntry:
    base: dict = {"repo": REPO, "issue": issue, "position": issue}
    base.update(kw)
    return QueueEntry(**base)


def board(
    *,
    merged: tuple[int, ...] = (),
    closed: tuple[int, ...] = (),
    open_: tuple[int, ...] = (),
    active: tuple[int, ...] = (),
    sessions: tuple[int, ...] = (),
) -> BoardView:
    facts: dict[str, IssueFacts] = {}
    for issue in {*merged, *closed, *open_, *active}:
        facts[entry_key(REPO, issue)] = IssueFacts(
            known=True,
            issue_state=(
                "closed" if issue in closed else ("open" if issue in open_ else "")
            ),
            merged=issue in merged,
            active_work=issue in active,
        )
    return BoardView(
        issues=facts,
        live_sessions=frozenset(entry_key(REPO, i) for i in sessions),
    )


# ── keys and --after parsing ─────────────────────────────────────────────────


def test_entry_key_and_parse_key_round_trip():
    assert entry_key(REPO, 1650) == f"{REPO}#1650"
    assert parse_key(f"{REPO}#1650") == (REPO, 1650)


def test_parse_key_rejects_a_non_numeric_tail():
    assert parse_key("claude-coordinator#abc") is None
    assert parse_key("claude-coordinator") is None


def test_bare_numbers_resolve_against_the_entrys_own_repo():
    assert parse_after_spec("1650,1651", REPO) == [
        f"{REPO}#1650",
        f"{REPO}#1651",
    ]


def test_qualified_and_bare_after_entries_mix():
    assert parse_after_spec(("1650", "quadraui#302"), REPO) == [
        f"{REPO}#1650",
        "quadraui#302",
    ]


def test_duplicate_after_entries_collapse_in_declaration_order():
    assert parse_after_spec("1650,1651,1650", REPO) == [
        f"{REPO}#1650",
        f"{REPO}#1651",
    ]


def test_a_malformed_after_entry_raises_rather_than_being_dropped():
    # A silently dropped pre-req launches work early — the exact failure this
    # feature exists to prevent.
    with pytest.raises(QueueError, match="malformed"):
        parse_after_spec("not-an-issue", REPO)


# ── cycle validation (the `add` gate) ────────────────────────────────────────


def test_find_cycle_reports_the_loop_members():
    cycle = find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]})
    assert cycle is not None
    assert set(cycle) == {"a", "b", "c"}


def test_find_cycle_ignores_edges_pointing_outside_the_queue():
    assert find_cycle({"a": ["not-queued"]}) is None


def test_validate_enqueue_refuses_a_self_edge():
    with pytest.raises(QueueError, match="cannot depend on itself"):
        validate_enqueue([], REPO, 1650, [entry_key(REPO, 1650)])


def test_validate_enqueue_refuses_a_two_node_cycle():
    existing = [entry(1650, after=(entry_key(REPO, 1654),))]
    with pytest.raises(QueueError, match="dependency cycle"):
        validate_enqueue(existing, REPO, 1654, [entry_key(REPO, 1650)])


def test_validate_enqueue_allows_a_prereq_that_is_not_queued():
    # `--after` is often "run this after that other thing merges", and that
    # other thing may never be queued at all. Satisfiability is a tick
    # question, not a write-time one.
    validate_enqueue([], REPO, 1654, ["quadraui#302"])


def test_validate_enqueue_uses_the_new_edges_not_the_stored_ones():
    # enqueue upserts, so re-adding 1650 with no `--after` must be judged on
    # the edges being WRITTEN — which is what makes remove+add the documented
    # escape from a queue that has somehow acquired a cycle.
    existing = [
        entry(1650, after=(entry_key(REPO, 1654),)),
        entry(1654, after=(entry_key(REPO, 1650),)),
    ]
    validate_enqueue(existing, REPO, 1650, [])


# ── building the board view ──────────────────────────────────────────────────


def test_build_board_view_reads_merge_and_activity_from_work_like_rows():
    view = build_board_view(
        {
            "assignments": [
                {"repo_name": REPO, "issue_number": 1650, "type": "work", "status": "merged"},
                {"repo_name": REPO, "issue_number": 1654, "type": "work", "status": "running"},
                # A review row must not make 1660 look like live WORK.
                {"repo_name": REPO, "issue_number": 1660, "type": "review", "status": "running"},
            ],
            "issues": [
                {"repo_name": REPO, "number": 1654, "state": "open"},
                {"repo_name": REPO, "number": 1650, "state": "closed"},
            ],
        },
        [{"repo": REPO, "issue": 1654}],
    )
    assert view.facts(entry_key(REPO, 1650)).landed
    assert view.facts(entry_key(REPO, 1654)).active_work
    assert view.facts(entry_key(REPO, 1654)).open
    assert not view.facts(entry_key(REPO, 1660)).active_work
    assert view.live_sessions == frozenset({entry_key(REPO, 1654)})


def test_unknown_issues_report_nothing_rather_than_raising():
    view = build_board_view({}, [])
    facts = view.facts("nope#1")
    assert not facts.known and not facts.landed and not facts.active_work


def test_entries_from_rows_types_after_json_in_either_encoding():
    typed = entries_from_rows(
        [
            {"repo_name": REPO, "issue_number": 2, "position": 1, "after_json": '["a#1"]'},
            {"repo_name": REPO, "issue_number": 1, "position": 0, "after_json": ["b#2"]},
            {"repo_name": REPO, "issue_number": 3, "position": 2, "after_json": "{oops"},
        ]
    )
    assert [e.issue for e in typed] == [1, 2, 3]
    assert typed[0].after == ("b#2",)
    assert typed[1].after == ("a#1",)
    assert typed[2].after == ()


# ── plan_tick: the launch decision ───────────────────────────────────────────


def test_first_eligible_wins_the_head_is_not_special():
    entries = [
        entry(1650, position=0, after=("quadraui#302",)),
        entry(1654, position=1),
    ]
    plan = plan_tick(entries, board(open_=(302,)), capacity=1)
    assert plan.launch is not None
    assert plan.launch.issue == 1654


def test_a_deferred_entry_keeps_its_position_and_counts_a_deferral():
    entries = [
        entry(1650, position=0, after=(entry_key(REPO, 1),), deferrals=3),
        entry(1654, position=1),
    ]
    plan = plan_tick(entries, board(open_=(1,)), capacity=1)
    assert [d.key for d in plan.deferrals] == [entry_key(REPO, 1650)]
    updates = plan.deferrals[0].updates
    assert updates["deferrals"] == 4
    assert "position" not in updates  # deferral never reorders (#1750 design note)
    assert "1" in updates["last_reason"]


def test_a_merged_prereq_satisfies_and_so_does_a_closed_issue():
    merged_dep = plan_tick(
        [entry(1654, after=(entry_key(REPO, 1650),))], board(merged=(1650,)), capacity=1
    )
    closed_dep = plan_tick(
        [entry(1654, after=(entry_key(REPO, 1650),))], board(closed=(1650,)), capacity=1
    )
    assert merged_dep.launch is not None
    assert closed_dep.launch is not None


def test_an_unknown_prereq_is_unsatisfiable_and_does_not_consume_an_attempt():
    entries = [entry(1654, after=("ghost#99",))]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.launch is None
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1654)]
    updates = plan.blocked[0].updates
    assert updates["state"] == STATE_BLOCKED
    assert "attempts" not in updates
    assert "ghost#99" in updates["last_reason"]


def test_a_prereq_queued_but_blocked_is_unsatisfiable():
    entries = [
        entry(1650, position=0, state=STATE_BLOCKED),
        entry(1654, position=1, after=(entry_key(REPO, 1650),)),
    ]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.launch is None
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1654)]
    assert "never satisfy" in plan.blocked[0].reason


def test_a_prereq_with_live_work_but_no_issue_row_defers_rather_than_blocks():
    # The standalone `serialize_board` payload ships assignments only, so the
    # daemon host sees no `issues` rows — an in-flight pre-req must still read
    # as "not yet", not as "unknown".
    entries = [entry(1654, after=(entry_key(REPO, 1650),))]
    plan = plan_tick(entries, board(active=(1650,)), capacity=1)
    assert plan.launch is None
    assert plan.blocked == ()
    assert "work in flight" in plan.deferrals[0].reason


def test_a_cycle_discovered_at_tick_time_blocks_every_member():
    entries = [
        entry(1650, position=0, after=(entry_key(REPO, 1654),)),
        entry(1654, position=1, after=(entry_key(REPO, 1650),)),
    ]
    plan = plan_tick(entries, board(), capacity=2)
    assert plan.launch is None
    assert {b.key for b in plan.blocked} == {
        entry_key(REPO, 1650),
        entry_key(REPO, 1654),
    }
    assert all("cycle" in b.reason for b in plan.blocked)


def test_nothing_eligible_records_exactly_one_queue_level_alert():
    entries = [
        entry(1650, position=0, after=("ghost#1",)),
        entry(1654, position=1, after=("ghost#2",)),
    ]
    plan = plan_tick(entries, board(), capacity=2)
    assert plan.launch is None
    assert plan.alert is not None
    assert "nothing eligible" in plan.alert.reason
    assert len(plan.alert.details) == 2


def test_an_empty_queue_raises_no_alert():
    assert plan_tick([], board(), capacity=1).alert is None


def test_terminal_entries_are_neither_launched_nor_alerted_on():
    entries = [entry(1650, state=STATE_DONE), entry(1654, state=STATE_BLOCKED)]
    plan = plan_tick(entries, board(), capacity=2)
    assert plan.launch is None
    assert plan.alert is None
    assert plan.writes() == []


# ── plan_tick: capacity ──────────────────────────────────────────────────────


def test_a_live_session_occupies_a_slot_and_blocks_the_launch():
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
    ]
    plan = plan_tick(entries, board(sessions=(1650,), active=(1650,)), capacity=1)
    assert plan.occupied == 1
    assert plan.launch is None
    assert plan.alert is None  # at capacity is the queue working, not a problem


def test_a_deadline_expired_drive_still_occupies_a_slot():
    # #1660 / the 2026-08-01 incident: `coord drive` returned EXIT_DEADLINE, so
    # the tmux session is gone — but the worker, test and review are still
    # running on the fleet. Counting this as free is how a sequential batch
    # became concurrent.
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
    ]
    plan = plan_tick(entries, board(active=(1650,)), capacity=1)
    assert plan.occupied == 1
    assert plan.launch is None
    assert [r.outcome for r in plan.reconciles] == ["held"]
    # The row stays `running` — nothing relaunches it while its work is live.
    assert "state" not in plan.reconciles[0].updates


def test_capacity_above_one_launches_while_another_drive_runs():
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
    ]
    plan = plan_tick(entries, board(sessions=(1650,)), capacity=2)
    assert plan.occupied == 1
    assert plan.launch is not None and plan.launch.issue == 1654


def test_only_one_entry_launches_per_tick():
    entries = [entry(1650, position=0), entry(1654, position=1)]
    plan = plan_tick(entries, board(), capacity=5)
    assert plan.launch is not None and plan.launch.issue == 1650
    assert len(plan.writes()) == 0  # nothing else touched


def test_entries_after_the_launch_are_reported_but_never_counted():
    entries = [
        entry(1650, position=0),
        entry(1654, position=1, after=(entry_key(REPO, 1650),), deferrals=0),
    ]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.launch is not None and plan.launch.issue == 1650
    assert [d.key for d in plan.deferrals] == [entry_key(REPO, 1654)]
    assert plan.deferrals[0].counted is False
    assert plan.deferrals[0].updates == {}
    assert plan.writes() == []  # a launch tick mutates only the launched row
    text = "\n".join(render_plan(plan))
    assert f"defer {entry_key(REPO, 1654)}" in text
    assert entry_key(REPO, 1650) in text
    assert "not reached this tick" in text


# ── plan_tick: reconciliation ────────────────────────────────────────────────


def test_a_finished_drive_becomes_done():
    entries = [entry(1650, state=STATE_RUNNING)]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=1)
    assert plan.reconciles[0].outcome == "done"
    assert plan.reconciles[0].updates["state"] == STATE_DONE
    assert plan.occupied == 0


def test_a_dead_drive_is_requeued_at_the_same_position_with_an_attempt_spent():
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "retry"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 1
    assert "position" not in reconcile.updates
    # …and it is eligible again on this same tick.
    assert plan.launch is not None and plan.launch.issue == 1650


def test_a_dead_drive_out_of_attempts_blocks_and_escalates():
    entries = [
        entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles[0].outcome == "exhausted"
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1650)]
    assert plan.blocked[0].updates["state"] == STATE_BLOCKED
    assert plan.blocked[0].updates["attempts"] == DEFAULT_MAX_ATTEMPTS
    assert plan.launch is None


def test_max_attempts_is_injectable():
    entries = [entry(1650, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1, max_attempts=1)
    assert plan.reconciles[0].outcome == "exhausted"


# ── rendering ────────────────────────────────────────────────────────────────


def test_render_plan_names_the_launch_and_the_defer_reason():
    entries = [
        entry(1650, position=0, after=(entry_key(REPO, 1),)),
        entry(1654, position=1, machine="dellserver"),
    ]
    lines = render_plan(
        plan_tick(entries, board(open_=(1,)), capacity=1), dry_run=True
    )
    text = "\n".join(lines)
    assert "would launch claude-coordinator#1654 on dellserver" in text
    assert "defer claude-coordinator#1650" in text
    assert "0/1 occupied" in text
