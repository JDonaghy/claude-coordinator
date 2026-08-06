"""Unit tests for coord/drive_queue.py — the pure half of the drive queue (#1754).

The CLI-level suite (tests/test_cli_drive_queue.py) is the acceptance bar; this
file pins the decisions themselves, the same way tests/test_drive.py pins
``coord.drive.decide`` rather than ``Driver.run``. The two rules that get the
most attention here are the ones that caused real incidents:

* capacity counted from BOARD state, so a drive whose observer hit its
  ``EXIT_DEADLINE`` (#1660) still occupies a slot (2026-08-01);
* unsatisfiable vs merely-unsatisfied, so a pre-req that will never land
  escalates instead of deferring forever;
* the startup grace window (#1794), so a tick firing seconds after a launch
  cannot declare a still-starting drive dead (2026-08-03).
"""

from __future__ import annotations

import pytest

from coord.drive_queue import (
    DEFAULT_MAX_ATTEMPTS,
    DRIVE_STARTUP_GRACE_SECONDS,
    HOLD_ARMED,
    HOLD_FIRED,
    HOLD_RELEASED,
    ProbeResult,
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

# A fixed wall clock for the #1794 startup-window tests. `plan_tick` takes
# `now` as a parameter precisely so these need no monkeypatching and no real
# sleeping — the module still never reads the clock itself.
NOW = 1_800_000_000.0


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


# ── plan_tick: a `waiting` entry whose issue already landed (#1873) ─────────
#
# The launch-side counterpart to test_a_finished_drive_becomes_done above:
# that test covers an entry `_reconcile_running` catches because it was
# actually launched.  A `waiting` entry never reaches that function at all —
# #1864 was the live incident: its work landed inside #1862's PR and the
# issue closed, but the queue row was never touched and `drive-queue tick`
# was about to burn a full drive re-discovering that.


def test_a_waiting_entry_whose_issue_is_closed_reconciles_to_done_unlaunched():
    entries = [entry(1864)]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    assert plan.launch is None
    assert [r.outcome for r in plan.reconciles] == ["done"]
    reconcile = plan.reconciles[0]
    assert reconcile.updates["state"] == STATE_DONE
    assert "never launched" in reconcile.reason
    assert "closed" in reconcile.reason


def test_a_waiting_entry_whose_work_merged_but_issue_still_open_also_reconciles():
    # #611 is why both witnesses exist: merged work can leave an issue open.
    entries = [entry(1864)]
    plan = plan_tick(entries, board(merged=(1864,), open_=(1864,)), capacity=1)
    assert plan.launch is None
    assert [r.outcome for r in plan.reconciles] == ["done"]
    reconcile = plan.reconciles[0]
    assert reconcile.updates["state"] == STATE_DONE
    assert "merged" in reconcile.reason


def test_a_landed_waiting_entry_does_not_consume_an_attempt():
    entries = [entry(1864, attempts=2)]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    updates = plan.reconciles[0].updates
    assert "attempts" not in updates


def test_a_landed_waiting_entrys_reason_is_distinct_from_a_real_completion():
    # The reason text must not read as "a drive ran and finished" — nothing
    # was ever launched for this entry.
    entries = [entry(1864)]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    reason = plan.reconciles[0].reason
    assert "drive finished" not in reason
    assert "never launched" in reason


def test_a_genuinely_open_waiting_entry_still_launches():
    entries = [entry(1864)]
    plan = plan_tick(entries, board(open_=(1864,)), capacity=1)
    assert plan.launch is not None and plan.launch.issue == 1864
    assert plan.reconciles == ()


def test_a_landed_entry_does_not_block_downstream_after_entries():
    # `_resolve_prereqs` already reads `facts.landed` straight off the board
    # (:707) — it does not care whether the pre-req's own queue row ever
    # transitioned to `done`.  A landed-but-still-`waiting` upstream entry
    # must not stall its successor.
    entries = [
        entry(1864, position=0, after=()),
        entry(1866, position=1, after=(entry_key(REPO, 1864),)),
    ]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=2)
    assert plan.launch is not None and plan.launch.issue == 1866
    outcomes = {r.key: r.outcome for r in plan.reconciles}
    assert outcomes[entry_key(REPO, 1864)] == "done"


def test_a_landed_entry_writes_are_applied_through_the_normal_writes_path():
    # The `Reconcile` this produces must flow through `TickPlan.writes()` the
    # same as every other reconcile — no separate plumbing for #1873's case.
    entries = [entry(1864)]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    writes = dict(plan.writes())
    assert writes[entry_key(REPO, 1864)]["state"] == STATE_DONE
    assert "attempts" not in writes[entry_key(REPO, 1864)]


def test_a_landed_waiting_entry_does_not_raise_a_stalled_alert():
    # The exact #1864 reproduction from the review: a single `waiting` entry
    # whose issue is closed.  `plan.launch` being `None` is correct, but
    # `plan.alert` must ALSO be `None` — the tick reconciled the entry
    # cleanly, it did not stall.  Before this fix, the `waiting` snapshot
    # taken before the walk still counted this entry as "considered", and it
    # has no `details` line (it was never deferred or blocked), so the queue
    # escalated a `QUEUE: STALLED` record for a tick that had nothing wrong.
    entries = [entry(1864)]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    assert plan.launch is None
    assert plan.alert is None


def test_a_mixed_queue_only_counts_the_genuinely_blocked_entry_in_the_alert():
    # One entry reconciles via #1873 (closed, never launched); the other is
    # genuinely unsatisfiable and blocks.  The alert must describe ONLY the
    # blocked entry — "considered N" and `len(details)` must agree, or the
    # alert contradicts `coord drive-queue status` two lines below it.
    entries = [
        entry(1864, position=0),
        entry(1654, position=1, after=("ghost#99",)),
    ]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=2)
    assert plan.launch is None
    assert plan.alert is not None
    assert "considered 1 waiting entry" in plan.alert.reason
    assert len(plan.alert.details) == 1
    assert entry_key(REPO, 1654) in plan.alert.details[0]
    assert entry_key(REPO, 1864) not in " ".join(plan.alert.details)


def test_a_landed_entry_with_an_unsatisfiable_prereq_still_reconciles_to_done():
    # Ordering matters: the entry's own board state is checked BEFORE its
    # `after=` graph, so a landed entry whose pre-req is unsatisfiable (here,
    # unknown) reconciles to `done` rather than being routed into BLOCKED —
    # which would escalate and demand a manual `remove && add` for an entry
    # that is already finished.
    entries = [entry(1864, after=("ghost#99",))]
    plan = plan_tick(entries, board(closed=(1864,)), capacity=1)
    assert plan.launch is None
    assert plan.blocked == ()
    assert [r.outcome for r in plan.reconciles] == ["done"]
    assert plan.reconciles[0].updates["state"] == STATE_DONE
    assert plan.alert is None


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
    # No `launched_at` and no `now`, so #1794's startup window does not apply:
    # a row with nothing to measure keeps the pre-#1794 behaviour exactly.
    assert entries[0].launched_at is None


def test_a_dead_drive_with_a_launch_stamp_still_dies_once_the_window_passes():
    """The `launched_at` path, not just the "no stamp to measure" one."""
    entries = [
        entry(
            1650,
            position=3,
            state=STATE_RUNNING,
            attempts=0,
            launched_at=NOW - DRIVE_STARTUP_GRACE_SECONDS - 1,
        )
    ]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "retry"
    assert plan.reconciles[0].updates["attempts"] == 1
    # …and the relaunch is allowed, because the window is demonstrably past.
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


# ── plan_tick: the startup grace window (#1794) ──────────────────────────────
#
# 2026-08-03, the first unattended run of the #1756 timer: a tick 40s after a
# launch found no tmux session and no board work (the drive was still coming
# up — measured 19:13:09 launch → 19:15:22 `drive loop started`), fell through
# every branch of `_reconcile_running` to `retry`, spent an attempt, and
# launched a SECOND `coord drive` for the same issue. The two ticks were 40s
# apart because `docs/DRIVE_QUEUE.md` §2's install sequence fires one
# (`enable --now`) and then its own verification step fires another.


def running_since(issue: int, age: float, **kw) -> QueueEntry:
    """A `running` entry launched *age* seconds before :data:`NOW`."""
    return entry(issue, state=STATE_RUNNING, launched_at=NOW - age, **kw)


def test_a_tick_seconds_after_the_launch_leaves_the_entry_running():
    """THE regression for #1794 — the 40s-later tick from the incident."""
    entries = [running_since(1762, 40.0, position=1, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)

    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "starting"
    assert reconcile.occupies is True
    # The three things the incident got wrong, asserted one by one.
    assert "state" not in reconcile.updates  # stays `running`
    assert "attempts" not in reconcile.updates  # no attempt spent
    assert plan.launch is None  # no duplicate drive
    assert plan.occupied == 1
    assert "40s ago" in reconcile.reason


def test_a_starting_drive_holds_its_slot_against_the_rest_of_the_queue():
    entries = [
        running_since(1762, 5.0, position=0),
        entry(1763, position=1),
    ]
    plan = plan_tick(entries, board(open_=(1763,)), capacity=1, now=NOW)
    assert plan.occupied == 1
    assert plan.launch is None
    # At capacity is the queue working, not a stall — no escalation.
    assert plan.alert is None


def test_a_starting_entry_is_never_relaunched_even_if_something_requeues_it():
    """The launch-side half of the guard.

    A `waiting` row with a fresh `launched_at` means a drive went up moments
    ago whatever the queue state now says. Starting a second one is exactly
    the #1794 failure, so the walk refuses it rather than leaning on `coord
    drive`'s per-issue flock to catch it.
    """
    entries = [entry(1762, position=0, launched_at=NOW - 10.0, deferrals=0)]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)
    assert plan.launch is None
    assert [d.key for d in plan.deferrals] == [entry_key(REPO, 1762)]
    assert "second `coord drive` is refused" in plan.deferrals[0].reason
    assert plan.deferrals[0].updates["deferrals"] == 1


def test_the_window_never_starves_a_later_entry_that_is_genuinely_ready():
    """The cooldown defers ONE entry; it does not close the queue."""
    entries = [
        entry(1762, position=0, launched_at=NOW - 10.0),
        entry(1763, position=1),
    ]
    plan = plan_tick(entries, board(), capacity=2, now=NOW)
    assert plan.launch is not None and plan.launch.issue == 1763


def test_a_live_session_still_wins_over_the_startup_window():
    entries = [running_since(1762, 5.0)]
    plan = plan_tick(entries, board(sessions=(1762,)), capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "alive"


def test_a_merged_issue_still_wins_over_the_startup_window():
    entries = [running_since(1762, 5.0)]
    plan = plan_tick(entries, board(merged=(1762,)), capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "done"
    assert plan.reconciles[0].updates["state"] == STATE_DONE


def test_1660_held_is_unchanged_by_the_startup_window():
    """#1660's `held` keeps its own branch, inside the window and outside it."""
    for age in (5.0, DRIVE_STARTUP_GRACE_SECONDS + 60.0):
        plan = plan_tick(
            [running_since(1762, age)], board(active=(1762,)), capacity=1, now=NOW
        )
        assert plan.reconciles[0].outcome == "held", age
        assert plan.reconciles[0].occupies is True
        assert "state" not in plan.reconciles[0].updates
        assert plan.launch is None


def test_death_detection_still_reaches_blocked_at_max_attempts():
    """The window delays a death by at most one interval; it never hides one."""
    old = DRIVE_STARTUP_GRACE_SECONDS + 1
    first = plan_tick(
        [running_since(1762, old, attempts=0)], board(), capacity=1, now=NOW
    )
    assert first.reconciles[0].outcome == "retry"
    assert first.reconciles[0].updates["attempts"] == 1

    second = plan_tick(
        [running_since(1762, old, attempts=DEFAULT_MAX_ATTEMPTS - 1)],
        board(),
        capacity=1,
        now=NOW,
    )
    assert second.reconciles[0].outcome == "exhausted"
    assert [b.key for b in second.blocked] == [entry_key(REPO, 1762)]
    assert second.blocked[0].updates["state"] == STATE_BLOCKED
    assert second.blocked[0].updates["attempts"] == DEFAULT_MAX_ATTEMPTS


def test_a_row_with_no_launch_stamp_keeps_the_pre_1794_behaviour():
    """A pre-DQ-1 row, or one a human flipped to `running` by hand."""
    plan = plan_tick(
        [entry(1762, state=STATE_RUNNING, launched_at=None)],
        board(),
        capacity=1,
        now=NOW,
    )
    assert plan.reconciles[0].outcome == "retry"


def test_a_backwards_clock_jump_cannot_pin_an_entry_in_the_window():
    """A `launched_at` in the future must not make an entry un-retryable."""
    plan = plan_tick(
        [entry(1762, state=STATE_RUNNING, launched_at=NOW + 10_000.0)],
        board(),
        capacity=1,
        now=NOW,
    )
    assert plan.reconciles[0].outcome == "retry"


def test_omitting_the_clock_disables_the_window_entirely():
    """`now=None` is the pure-logic caller's opt-out, not a silent grace."""
    plan = plan_tick([running_since(1762, 5.0)], board(), capacity=1)
    assert plan.reconciles[0].outcome == "retry"


def test_the_grace_window_is_injectable():
    entries = [running_since(1762, 60.0)]
    assert (
        plan_tick(entries, board(), capacity=1, now=NOW, grace_seconds=30.0)
        .reconciles[0]
        .outcome
        == "retry"
    )
    assert (
        plan_tick(entries, board(), capacity=1, now=NOW, grace_seconds=120.0)
        .reconciles[0]
        .outcome
        == "starting"
    )


def test_the_default_window_clears_the_measured_startup_time():
    """~2 min measured on a loaded dellserver; the default must beat it."""
    assert DRIVE_STARTUP_GRACE_SECONDS >= 300.0


# ── plan_tick: the cross-host guard (#1870) ──────────────────────────────────
#
# 2026-08-06: two live `coord drive` sessions on the same issue at once. One
# was launched by hand on `elitebook` and was 47 minutes (2841s) into a
# healthy run — `work=done`, `test=running`. The other was a duplicate the
# TIMER's own tick launched on `dellserver` after concluding, from ITS local
# (and therefore blind) tmux read, that the elitebook session had "died
# without landing the work". #1794's grace window does not help here: the
# session was three orders of magnitude past any plausible grace and still
# invisible — the miss is not transient, it is structural, because liveness
# is always a local `tmux list-sessions` and the queue is fleet-global.


def test_a_drive_launched_on_another_host_is_unknown_not_dead():
    """THE regression for #1870 — the elitebook/dellserver duplicate launch."""
    entries = [
        running_since(
            1811,
            DRIVE_STARTUP_GRACE_SECONDS + 2841.0,
            position=0,
            attempts=0,
            launch_host="elitebook",
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW, local_host="dellserver"
    )

    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "unknown"
    assert reconcile.occupies is True
    # The three things the incident got wrong, asserted one by one — the same
    # shape as #1794's own regression test above.
    assert "state" not in reconcile.updates  # stays `running`
    assert "attempts" not in reconcile.updates  # no attempt spent
    assert plan.launch is None  # no duplicate drive
    assert plan.occupied == 1
    assert "elitebook" in reconcile.reason
    assert "dellserver" in reconcile.reason


def test_a_cross_host_entry_is_never_relaunched_even_with_free_capacity():
    """AC: no second drive for an entry with a live session on another host."""
    entries = [
        running_since(
            1811,
            DRIVE_STARTUP_GRACE_SECONDS + 100.0,
            position=0,
            launch_host="elitebook",
        ),
        entry(1812, position=1),
    ]
    plan = plan_tick(
        entries, board(open_=(1812,)), capacity=5, now=NOW, local_host="dellserver"
    )
    # Free capacity and a fully eligible successor — #1812 launches, #1811
    # does not get a second drive.
    assert plan.launch is not None and plan.launch.issue == 1812
    assert plan.occupied == 1


def test_a_same_host_entry_still_reconciles_normally():
    """The guard is scoped to a MISMATCH — this host's own launch is unaffected."""
    entries = [
        running_since(
            1811,
            DRIVE_STARTUP_GRACE_SECONDS + 1.0,
            attempts=0,
            launch_host="dellserver",
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW, local_host="dellserver"
    )
    assert plan.reconciles[0].outcome == "retry"
    assert plan.reconciles[0].updates["attempts"] == 1


def test_the_host_match_is_case_insensitive():
    entries = [
        running_since(
            1811,
            DRIVE_STARTUP_GRACE_SECONDS + 1.0,
            launch_host="DellServer",
        )
    ]
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW, local_host="dellserver"
    )
    assert plan.reconciles[0].outcome == "retry"


def test_an_entry_with_no_recorded_launch_host_keeps_the_pre_1870_behaviour():
    """AC: entries predating the column (or hand-edited) behave exactly as today."""
    entries = [
        running_since(1811, DRIVE_STARTUP_GRACE_SECONDS + 1.0, attempts=0)
    ]
    assert entries[0].launch_host == ""
    plan = plan_tick(
        entries, board(), capacity=1, now=NOW, local_host="dellserver"
    )
    assert plan.reconciles[0].outcome == "retry"


def test_omitting_local_host_disables_the_cross_host_check_entirely():
    """`local_host=None` is the pure-logic caller's opt-out, like `now=None`."""
    entries = [
        running_since(
            1811,
            DRIVE_STARTUP_GRACE_SECONDS + 1.0,
            launch_host="elitebook",
        )
    ]
    plan = plan_tick(entries, board(), capacity=1, now=NOW)
    assert plan.reconciles[0].outcome == "retry"


def test_a_live_session_still_wins_over_a_host_mismatch():
    """A real positive signal always outranks the cross-host guard."""
    entries = [running_since(1811, 5.0, launch_host="elitebook")]
    plan = plan_tick(
        entries,
        board(sessions=(1811,)),
        capacity=1,
        now=NOW,
        local_host="dellserver",
    )
    assert plan.reconciles[0].outcome == "alive"


def test_active_work_still_wins_over_a_host_mismatch():
    """#1660's `held` is a board-global fact; it must not be shadowed by #1870."""
    entries = [running_since(1811, 5.0, launch_host="elitebook")]
    plan = plan_tick(
        entries,
        board(active=(1811,)),
        capacity=1,
        now=NOW,
        local_host="dellserver",
    )
    assert plan.reconciles[0].outcome == "held"


def test_landed_still_wins_over_a_host_mismatch():
    entries = [running_since(1811, 5.0, launch_host="elitebook")]
    plan = plan_tick(
        entries,
        board(merged=(1811,)),
        capacity=1,
        now=NOW,
        local_host="dellserver",
    )
    assert plan.reconciles[0].outcome == "done"


def test_a_cross_host_entry_holds_its_slot_against_the_rest_of_the_queue():
    entries = [
        running_since(1811, 5.0, position=0, launch_host="elitebook"),
        entry(1812, position=1),
    ]
    plan = plan_tick(
        entries, board(open_=(1812,)), capacity=1, now=NOW, local_host="dellserver"
    )
    assert plan.occupied == 1
    assert plan.launch is None
    # At capacity is the queue working, not a stall — no escalation.
    assert plan.alert is None


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


def test_render_plan_narrates_a_starting_drive_and_the_full_slot():
    """#1794 was diagnosed from a journal, so the journal has to say it."""
    entries = [
        entry(1762, position=0, state=STATE_RUNNING, launched_at=NOW - 41.0),
        entry(1763, position=1),
    ]
    text = "\n".join(
        render_plan(plan_tick(entries, board(open_=(1763,)), capacity=1, now=NOW))
    )
    assert "reconcile claude-coordinator#1762: starting" in text
    assert "startup grace window (#1794)" in text
    assert "no launch — at capacity (1/1 occupied)" in text
    assert "retry" not in text


def test_render_plan_narrates_a_cross_host_entry_as_unknown():
    """#1870 was diagnosed from a journal too; the journal has to say it."""
    entries = [
        entry(
            1811,
            position=0,
            state=STATE_RUNNING,
            launched_at=NOW - (DRIVE_STARTUP_GRACE_SECONDS + 2841.0),
            launch_host="elitebook",
        ),
    ]
    text = "\n".join(
        render_plan(
            plan_tick(entries, board(), capacity=1, now=NOW, local_host="dellserver")
        )
    )
    assert "reconcile claude-coordinator#1811: unknown" in text
    assert "elitebook" in text
    assert "not this host" in text
    assert "no launch — at capacity (1/1 occupied)" in text
    assert "retry" not in text


# ── plan_tick: deploy gates (#1757) ──────────────────────────────────────────
#
# `merged != live`. These pin the decision half of the gate: what fires it,
# what does NOT fire it, and the fact that a fired gate outranks every other
# reason the walk might have had to launch something.


def held(issue: int, **kw) -> QueueEntry:
    """A `--hold-after` entry whose gate has already fired."""
    base = {
        "state": STATE_DONE,
        "hold_after": True,
        "hold_reason": "restart coord-serve",
        "hold_state": HOLD_FIRED,
    }
    base.update(kw)
    return entry(issue, **base)


def test_a_gate_fires_the_tick_its_entry_reaches_done():
    plan = plan_tick(
        [
            entry(
                1,
                state=STATE_RUNNING,
                hold_after=True,
                hold_reason="deploy",
                hold_state=HOLD_ARMED,
            ),
            entry(2),
        ],
        board(merged=(1,), open_=(2,)),
        capacity=1,
    )
    assert plan.launch is None
    assert plan.held is not None
    assert plan.held.outcome == "fired"
    assert dict(plan.writes())[entry_key(REPO, 1)]["hold_state"] == HOLD_FIRED


def test_a_fired_gate_blocks_a_fully_eligible_successor_with_free_capacity():
    """The whole feature in one assertion."""
    plan = plan_tick(
        [held(1), entry(2)],
        board(open_=(2,)),
        capacity=4,
    )
    assert plan.free_slots == 4
    assert plan.launch is None
    assert plan.deferrals == ()
    assert "restart coord-serve" in plan.alert.reason
    assert plan.alert.command == "coord drive-queue resume"


def test_an_armed_gate_on_an_unlanded_entry_holds_nothing():
    plan = plan_tick(
        [entry(1, hold_after=True, hold_state=HOLD_ARMED), entry(2)],
        board(open_=(1, 2)),
        capacity=1,
    )
    assert plan.held is None
    assert plan.launch is not None and plan.launch.issue == 1


def test_a_released_gate_holds_nothing():
    plan = plan_tick(
        [held(1, hold_state=HOLD_RELEASED), entry(2)],
        board(open_=(2,)),
        capacity=1,
    )
    assert plan.held is None
    assert plan.launch is not None and plan.launch.issue == 2


def test_a_hold_after_entry_that_dies_out_of_attempts_blocks_and_never_fires():
    """`blocked` already stops the queue — a second alert would just be noise."""
    plan = plan_tick(
        [
            entry(
                1,
                state=STATE_RUNNING,
                attempts=DEFAULT_MAX_ATTEMPTS - 1,
                hold_after=True,
                hold_state=HOLD_ARMED,
            )
        ],
        board(),
        capacity=1,
    )
    assert plan.held is None
    assert plan.holds == ()
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1)]
    assert "HELD" not in (plan.alert.reason if plan.alert else "")


def test_a_failing_probe_stays_held_and_increments_a_typed_attempt_count():
    key = entry_key(REPO, 1)
    plan = plan_tick(
        [held(1, resume_when="curl -sf x", hold_probes=2), entry(2)],
        board(open_=(2,)),
        capacity=1,
        probes={key: ProbeResult(key, False, "exit 7")},
    )
    assert plan.launch is None
    assert plan.held.probes == 3
    assert dict(plan.writes())[key]["hold_probes"] == 3
    assert "attempt 3 failed" in " ".join(plan.alert.details)


def test_a_passing_probe_releases_and_launches_in_the_same_tick():
    key = entry_key(REPO, 1)
    plan = plan_tick(
        [held(1, resume_when="curl -sf x", hold_probes=4), entry(2)],
        board(open_=(2,)),
        capacity=1,
        probes={key: ProbeResult(key, True, "exit 0")},
    )
    assert plan.held is None
    assert plan.launch is not None and plan.launch.issue == 2
    writes = dict(plan.writes())
    assert writes[key]["hold_state"] == HOLD_RELEASED
    assert writes[key]["hold_probes"] == 0
    assert plan.alert is None


def test_a_gate_with_no_probe_result_stays_held_and_writes_nothing():
    """Manual-resume-only, and a probe the shell could not run. Fail closed."""
    plan = plan_tick([held(1), entry(2)], board(open_=(2,)), capacity=1)
    assert plan.launch is None
    assert plan.writes() == []
    assert "release manually" in " ".join(plan.alert.details)


def test_only_an_already_fired_gate_is_offered_for_probing():
    from coord.drive_queue import pending_probe_targets

    entries = [
        entry(1, hold_after=True, hold_state=HOLD_ARMED, resume_when="a"),
        held(2, resume_when="b"),
        held(3),  # fired, but no probe declared
        held(4, hold_state=HOLD_RELEASED, resume_when="d"),
    ]
    assert [e.issue for e in pending_probe_targets(entries)] == [2]


def test_render_plan_says_why_nothing_launched():
    plan = plan_tick([held(1), entry(2)], board(open_=(2,)), capacity=1)
    text = "\n".join(render_plan(plan))
    assert "hold claude-coordinator#1: held" in text
    assert "no launch — HELD" in text
    assert "coord drive-queue resume" in text
