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
    HOLD_SCOPE_ENTRY,
    HOLD_SCOPE_FLEET,
    PARK_STALE_SECONDS,
    ProbeResult,
    STATE_BLOCKED,
    STATE_DONE,
    STATE_FAILED,
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
    ci_pending: tuple[int, ...] = (),
    ci_pending_live: tuple[int, ...] = (),
) -> BoardView:
    facts: dict[str, IssueFacts] = {}
    for issue in {*merged, *closed, *open_, *active, *ci_pending, *ci_pending_live}:
        facts[entry_key(REPO, issue)] = IssueFacts(
            known=True,
            issue_state=(
                "closed" if issue in closed else ("open" if issue in open_ else "")
            ),
            merged=issue in merged,
            active_work=issue in active,
            # #1891: the board's current read of this issue's merge gate —
            # nothing stronger than "CI checks have not reported yet".
            merge_ci_pending=issue in ci_pending or issue in ci_pending_live,
            # #2158: *ci_pending* is the un-refreshable reading (the raw
            # `merge_queue` row's frozen `error`, which only a live `coord
            # merge` rewrites); *ci_pending_live* is the self-refreshing one
            # (the `merge_plan` row's own reason, re-derived every board
            # build). Only the former ages out.
            merge_ci_pending_live=issue in ci_pending_live,
            merge_ci_pending_reason=(
                "CI running: test (3.12)"
                if issue in ci_pending or issue in ci_pending_live
                else ""
            ),
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


def test_build_board_view_reads_merge_ci_pending_from_the_live_plan_reason():
    """#1891: the live `merge_plan` section (board-render time) is the
    primary source — mirrors `drive_state._merge_entry`'s own resolution."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 1650,
                    "reason": "CI running: build, lint",
                },
                {
                    "repo_name": REPO, "issue_number": 1654,
                    "reason": "CI failed: build (failure)",
                },
            ],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 1650, "error": None},
                {"repo_name": REPO, "issue_number": 1654, "error": None},
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 1650)).merge_ci_pending
    assert not view.facts(entry_key(REPO, 1654)).merge_ci_pending


def test_build_board_view_falls_back_to_the_raw_queue_rows_persisted_error():
    """#1891: when the live plan's re-evaluation comes back with no reason
    (e.g. a `_gate_refresher` snapshot that lagged or gapped a real `coord
    merge` attempt's fresher read — see `CI_PENDING_PREFIX`'s docstring),
    `merge_ci_pending` still sees the checks-pending signal through the raw
    `merge_queue` row's own persisted `error` — exactly the fallback
    `drive_state._merge_entry` uses for `merge_reason`."""
    view = build_board_view(
        {
            "merge_plan": [
                {"repo_name": REPO, "issue_number": 1650, "reason": None},
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1650,
                    "error": "CI running: build",
                },
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 1650)).merge_ci_pending


def test_build_board_view_ci_pending_is_false_with_no_merge_sections_at_all():
    """A board payload predating #1891 (or a standalone fetch with neither
    section populated) degrades to `merge_ci_pending=False` everywhere,
    never raises."""
    view = build_board_view({"assignments": [], "issues": []}, [])
    assert not view.facts(entry_key(REPO, 1650)).merge_ci_pending


# ── #1892: the sibling trigger — a verdictless CI failure ──────────────────

def test_build_board_view_reads_merge_ci_pending_from_a_ci_infra_raw_row():
    """#1892: `_entry_gate_status` (board-render time) never computes the
    CI_INFRA_PREFIX classification — it needs an extra `gh api .../jobs`
    call the board *read* path must never make (`coord.gate_snapshot`'s
    Invariant 1). Only a LIVE `coord merge` attempt computes it and persists
    it onto the raw `merge_queue` row's `error`; the live `merge_plan`
    reason for the SAME entry still reads the generic "checks failed: ..."
    wording. `build_board_view` must prefer the raw row's more specific
    reading — mirroring `drive_state._merge_entry`'s identical recovery —
    or a verdictless failure would never park at all."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 1892,
                    "reason": "checks failed: e2e (cancelled)",
                },
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1892,
                    "error": (
                        "CI infra: e2e (cancelled) — no verdict about the "
                        "code (never assigned a runner, or died before "
                        "checkout)"
                    ),
                },
            ],
        },
        [],
    )
    facts = view.facts(entry_key(REPO, 1892))
    assert facts.merge_ci_pending
    assert facts.merge_ci_pending_reason.startswith("CI infra:")


def test_build_board_view_does_not_park_a_genuine_checks_failed_entry():
    """Regression: a plain 'checks failed' reason on BOTH the plan and the
    raw row — no #1892 classification anywhere — must not park."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 1893,
                    "reason": "checks failed: build (failure)",
                },
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1893,
                    "error": "checks failed: build (failure)",
                },
            ],
        },
        [],
    )
    assert not view.facts(entry_key(REPO, 1893)).merge_ci_pending


def test_build_board_view_live_ci_infra_plan_reason_also_parks():
    """If a future refactor DOES let the plan itself carry the #1892
    wording, `build_board_view` must still recognise it directly — the raw-
    row cross-check is a fallback, not the only path."""
    view = build_board_view(
        {
            "merge_plan": [
                {
                    "repo_name": REPO, "issue_number": 1894,
                    "reason": "CI infra: e2e (cancelled)",
                },
            ],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 1894, "error": None},
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 1894)).merge_ci_pending


# ── #2158: the frozen `error` string vs the live CI rollup ─────────────────
#
# The raw `merge_queue` row's `error` is written by a live `coord merge`
# attempt and by NOTHING else. For a `parked` entry — which by construction
# runs no merge — it is frozen at the attempt that parked it, so believing it
# over the board's own fresh reading makes the predicate that RELEASES the
# park refreshable only by the action the park WITHHOLDS.
#
# claude-coordinator#2138 (2026-08-12): CI run 31570947900 completed green at
# 06:48:51; the park was written at 06:49:32 quoting "CI running: …"; the
# entry then did not move for 7h25m, over a fully satisfied gate, until an
# unrelated merge happened to rewrite the board.


def _plan_row(issue: int, *, reason=None, ci_summary=None) -> dict:
    """One `/board` `merge_plan` row, shaped as `serve_app` ships it
    (`dataclasses.asdict` of a `PlannedMerge`, so `ci_summary` is a nested
    dict of `coord.ci_store.CiCheckSummary`)."""
    return {
        "repo_name": REPO,
        "issue_number": issue,
        "reason": reason,
        "ci_summary": ci_summary,
    }


def _rollup(passed: int = 0, failed: int = 0, running: int = 0) -> dict:
    return {
        "passed": passed,
        "failed": failed,
        "running": running,
        "failed_names": [],
        "first_failed_url": None,
    }


def test_build_board_view_drops_a_stale_ci_error_the_live_rollup_contradicts():
    """THE #2158 regression, at the fact level.

    The plan re-derived this entry clean (no reason of its own) AND its
    `ci_summary` — `summarize_counts` over the very checks that re-derivation
    consulted — says all 8 checks finished green. The raw row's "CI running:"
    is therefore a frozen write-path string that CI has already outrun, and
    must not hold the park.
    """
    view = build_board_view(
        {
            "merge_plan": [_plan_row(2138, reason=None, ci_summary=_rollup(passed=8))],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 2138,
                    "error": (
                        "CI running: no-gh-on-path, test (3.13), test (3.12)"
                    ),
                },
            ],
        },
        [],
    )
    assert not view.facts(entry_key(REPO, 2138)).merge_ci_pending


def test_build_board_view_keeps_the_park_while_the_rollup_shows_checks_in_flight():
    """The other half: checks genuinely still running is NOT evidence against
    the persisted reading — it agrees with it. Stays parked, no hot loop."""
    view = build_board_view(
        {
            "merge_plan": [
                _plan_row(2138, reason=None, ci_summary=_rollup(passed=5, running=3)),
            ],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 2138, "error": "CI running: test"},
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 2138)).merge_ci_pending


def test_build_board_view_keeps_the_park_when_the_plan_carries_no_rollup():
    """Fail closed, and leave #1891 exactly as it was: absence of a rollup
    (no PR yet, no `ci_store`, a gate snapshot that has not fetched this PR)
    is not evidence of anything. Only a POSITIVE all-green reading overrides
    the persisted string."""
    view = build_board_view(
        {
            "merge_plan": [_plan_row(2138, reason=None, ci_summary=None)],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 2138, "error": "CI running: test"},
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 2138)).merge_ci_pending


def test_build_board_view_keeps_a_ci_infra_park_while_the_rollup_shows_red():
    """#1892's classification lives ONLY on the raw row — the plan can never
    re-derive it. So a rollup that still shows a failed check is not evidence
    the verdictless failure has cleared, and the #2158 override must not fire
    on it. (An all-green rollup would; see the next test.)"""
    view = build_board_view(
        {
            "merge_plan": [
                _plan_row(1892, reason=None, ci_summary=_rollup(passed=7, failed=1)),
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1892,
                    "error": "CI infra: e2e (cancelled) — no verdict about the code",
                },
            ],
        },
        [],
    )
    assert view.facts(entry_key(REPO, 1892)).merge_ci_pending


def test_build_board_view_releases_a_ci_infra_park_once_the_rerun_lands_green():
    """The #1892 auto-rerun landing is exactly what un-parks that entry — and
    an all-green rollup is how the read path can see it happen, without a
    live `coord merge` to rewrite the raw row."""
    view = build_board_view(
        {
            "merge_plan": [_plan_row(1892, reason=None, ci_summary=_rollup(passed=8))],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1892,
                    "error": "CI infra: e2e (cancelled) — no verdict about the code",
                },
            ],
        },
        [],
    )
    assert not view.facts(entry_key(REPO, 1892)).merge_ci_pending


def test_build_board_view_flags_a_ci_infra_override_as_unrefreshable():
    """The realistic #1892-override pairing (review finding on #2158's first
    diff): `_entry_gate_status` re-derives a LIVE, non-infra "CI failed: ..."
    reason on the SAME `checks` read that produced a red `ci_summary` — a
    generic verdict is exactly what a still-failing CI-infra check looks like
    from the plan's side, since the plan can never compute the infra
    classification itself (#1892). The raw row still carries the frozen "CI
    infra:" string from the live merge attempt that parked this entry, so the
    #1892 override fires and `reason` ends up being that raw string — NOT the
    live plan one.

    `merge_ci_pending_live` must follow `reason`'s actual provenance, not
    `bool(plan_reason)`: a non-empty plan reason lost the override fight here,
    so this reading is exactly as unrefreshable as if the plan had been
    silent, and `plan_tick`'s `PARK_STALE_SECONDS` ceiling must still apply to
    it. Before the fix this asserted `merge_ci_pending_live=True` — the same
    "held with no ceiling" bug #2158 was written to close, just reached via a
    plan row that isn't empty."""
    view = build_board_view(
        {
            "merge_plan": [
                _plan_row(
                    1892,
                    reason="CI failed: test (3.12)",
                    ci_summary=_rollup(passed=7, failed=1),
                ),
            ],
            "merge_queue": [
                {
                    "repo_name": REPO, "issue_number": 1892,
                    "error": "CI infra: e2e (cancelled) — no verdict about the code",
                },
            ],
        },
        [],
    )
    facts = view.facts(entry_key(REPO, 1892))
    assert facts.merge_ci_pending
    assert facts.merge_ci_pending_reason == (
        "CI infra: e2e (cancelled) — no verdict about the code"
    )
    assert not facts.merge_ci_pending_live


def test_build_board_view_never_lets_a_rollup_overrule_a_live_plan_objection():
    """A non-empty plan reason is the live gate still objecting. It wins
    outright — the override only ever applies where the plan is silent."""
    view = build_board_view(
        {
            "merge_plan": [
                _plan_row(
                    2138,
                    reason="CI running: test (3.12)",
                    # Contradictory on purpose: a rollup that lagged the gate.
                    ci_summary=_rollup(passed=8),
                ),
            ],
            "merge_queue": [{"repo_name": REPO, "issue_number": 2138, "error": None}],
        },
        [],
    )
    facts = view.facts(entry_key(REPO, 2138))
    assert facts.merge_ci_pending
    assert facts.merge_ci_pending_live


def test_build_board_view_marks_a_raw_only_ci_reading_as_unrefreshable():
    """Provenance (#2158): a reading with no live plan reason behind it is
    flagged `merge_ci_pending_live=False`, which is what lets `plan_tick` age
    it out instead of trusting it forever."""
    view = build_board_view(
        {
            "merge_plan": [],
            "merge_queue": [
                {"repo_name": REPO, "issue_number": 2138, "error": "CI running: test"},
            ],
        },
        [],
    )
    facts = view.facts(entry_key(REPO, 2138))
    assert facts.merge_ci_pending
    assert not facts.merge_ci_pending_live


def test_build_board_view_survives_a_malformed_ci_rollup():
    """A rollup that is not a readable mapping of ints is not evidence — the
    park stands, and nothing raises."""
    for summary in ("green", 3, {"passed": "eight", "failed": 0, "running": 0}, []):
        view = build_board_view(
            {
                "merge_plan": [_plan_row(2138, reason=None, ci_summary=summary)],
                "merge_queue": [
                    {
                        "repo_name": REPO, "issue_number": 2138,
                        "error": "CI running: test",
                    },
                ],
            },
            [],
        )
        assert view.facts(entry_key(REPO, 2138)).merge_ci_pending, summary


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
    # Both entries are the SAME repo, so #1972's per-repo ceiling has to be
    # raised for the global ceiling to be the thing under test here — with the
    # default of 1 this queue deliberately defers (the test right below).
    plan = plan_tick(
        entries, board(sessions=(1650,)), capacity=2, max_parallel_per_repo=2
    )
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


# ── plan_tick: the per-repo ceiling (#1972) ─────────────────────────────────
#
# Per-repo serialisation, cross-repo parallelism. The hazard that forced
# serialisation is intra-repo (a merge stales the Test verdicts of the other
# queued branches in THAT repo), so repo is the axis along which extra
# parallelism is safe. Before this, `--max-parallel` was one global counter:
# capacity 3 with a 39-entry claude-coordinator queue would launch the two
# entries most likely to stale each other and never reach the quadraui entry
# that could have run for free.


def cross_repo_board(
    *,
    open_: tuple[str, ...] = (),
    sessions: tuple[str, ...] = (),
    active: tuple[str, ...] = (),
) -> BoardView:
    """A board keyed by fully-qualified ``repo#N``, for the multi-repo tests.

    The module-level ``board()`` helper hardcodes ``REPO``, which is exactly
    the single-repo assumption #1972 exists to break.
    """
    facts = {
        key: IssueFacts(
            known=True,
            issue_state="open",
            active_work=key in active,
        )
        for key in {*open_, *active, *sessions}
    }
    return BoardView(issues=facts, live_sessions=frozenset(sessions))


def other(issue: int, repo: str, position: int, **kw) -> QueueEntry:
    return QueueEntry(repo=repo, issue=issue, position=position, **kw)


def test_a_second_repo_rides_alongside_an_in_progress_repo():
    """#1972's headline scenario, asserted end to end.

    Capacity 3. Position 0 is a claude-coordinator drive in progress;
    positions 1..38 are claude-coordinator and blocked BY DESIGN; position 39
    is quadraui. The tick must launch the quadraui entry — the walk already
    skips deferred entries, so all this needs is for the same-repo entries to
    actually defer.
    """
    entries = [entry(1650, position=0, state=STATE_RUNNING)]
    entries += [entry(1700 + i, position=i + 1) for i in range(38)]
    entries.append(other(302, "quadraui", position=39))
    board_view = cross_repo_board(
        sessions=(entry_key(REPO, 1650),),
        active=(entry_key(REPO, 1650),),
        open_=tuple(entry_key(REPO, 1700 + i) for i in range(38))
        + ("quadraui#302",),
    )

    plan = plan_tick(entries, board_view, capacity=3)

    assert plan.launch is not None
    assert plan.launch.key == "quadraui#302"
    assert plan.occupied == 1
    assert plan.repo_occupied == {REPO: 1}


def test_same_repo_entries_defer_rather_than_block():
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1, deferrals=2),
        other(302, "quadraui", position=2),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650),),
            open_=(entry_key(REPO, 1654), "quadraui#302"),
        ),
        capacity=3,
    )
    assert plan.launch is not None and plan.launch.key == "quadraui#302"
    assert plan.blocked == ()  # a defer, never a block
    deferral = next(d for d in plan.deferrals if d.key == entry_key(REPO, 1654))
    assert deferral.repo_limited is True
    # Position untouched, no attempt consumed — only the deferral counter and
    # the reason an operator reads in `coord drive-queue list` move.
    assert set(deferral.updates) == {"deferrals", "last_reason"}
    assert deferral.updates["deferrals"] == 3
    assert "at its limit (1/1)" in deferral.reason


def test_the_per_repo_ceiling_is_configurable():
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
    ]
    board_view = cross_repo_board(
        sessions=(entry_key(REPO, 1650),), open_=(entry_key(REPO, 1654),)
    )
    assert plan_tick(entries, board_view, capacity=3).launch is None
    raised = plan_tick(entries, board_view, capacity=3, max_parallel_per_repo=2)
    assert raised.launch is not None and raised.launch.issue == 1654
    # 0 disables the ceiling entirely — one global counter, as before #1972.
    off = plan_tick(entries, board_view, capacity=3, max_parallel_per_repo=0)
    assert off.launch is not None and off.launch.issue == 1654


def test_the_global_ceiling_still_wins_over_a_repo_with_headroom():
    """Both apply, GLOBAL first — a full fleet launches nothing, any repo."""
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        other(302, "quadraui", position=1),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650),), open_=("quadraui#302",)
        ),
        capacity=1,
    )
    assert plan.launch is None
    assert plan.deferrals == ()  # the walk never ran; at capacity is not a defer


def test_a_repo_limited_queue_is_saturated_not_stalled():
    """No queue-level alert: this is the queue working, like at-capacity."""
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650),), open_=(entry_key(REPO, 1654),)
        ),
        capacity=3,
    )
    assert plan.launch is None
    assert plan.alert is None
    text = "\n".join(render_plan(plan))
    assert "per-repo: claude-coordinator 1/1" in text
    assert "every waiting entry's repo is at its per-repo limit" in text


def test_a_genuinely_stuck_entry_still_alerts_alongside_a_repo_limit_defer():
    """Mixed tick: something really is stuck, so the alert names all of it."""
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1),
        other(302, "quadraui", position=2, after=("quadraui#1",)),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650),), open_=(entry_key(REPO, 1654),)
        ),
        capacity=3,
    )
    assert plan.launch is None
    assert plan.alert is not None
    assert len(plan.alert.details) == 2  # both deferrals explained


def test_the_launch_takes_its_own_repos_slot_in_the_report_only_tail():
    """`--dry-run` must not call the next same-repo entry eligible."""
    entries = [
        other(302, "quadraui", position=0),
        other(303, "quadraui", position=1),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(open_=("quadraui#302", "quadraui#303")),
        capacity=3,
    )
    assert plan.launch is not None and plan.launch.issue == 302
    assert [d.key for d in plan.deferrals] == ["quadraui#303"]
    assert plan.deferrals[0].counted is False  # never competed for the slot
    assert plan.deferrals[0].updates == {}
    assert plan.writes() == []
    assert "at its limit (1/1)" in plan.deferrals[0].reason


def test_an_unsatisfiable_prereq_still_blocks_inside_a_full_repo():
    """Capacity is not an excuse to sit on a permanently broken entry."""
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        entry(1654, position=1, after=("ghost#1",)),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(sessions=(entry_key(REPO, 1650),)),
        capacity=3,
    )
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1654)]
    assert plan.alert is not None  # blocked is a stall, and stalls escalate


def test_the_capacity_line_shows_the_per_repo_breakdown():
    entries = [
        entry(1650, position=0, state=STATE_RUNNING),
        other(302, "quadraui", position=1, state=STATE_RUNNING),
        entry(1654, position=2),
    ]
    plan = plan_tick(
        entries,
        cross_repo_board(
            sessions=(entry_key(REPO, 1650), "quadraui#302"),
            open_=(entry_key(REPO, 1654),),
        ),
        capacity=4,
    )
    text = "\n".join(render_plan(plan, dry_run=True))
    assert "2/4 occupied" in text
    assert "per-repo: claude-coordinator 1/1, quadraui 1/1" in text
    # #1660's caveat, restated where it now bites one repo instead of the queue.
    assert "counted from board state" in text


def test_a_plan_without_a_per_repo_ceiling_renders_the_original_line():
    entries = [entry(1650, position=0)]
    plan = plan_tick(entries, board(), capacity=1, max_parallel_per_repo=0)
    assert "per-repo" not in "\n".join(render_plan(plan))


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


# ── plan_tick: the drive's own exit reason wins over "died" (#1845/#1844) ────
#
# `_reconcile_running`'s death branch ("no session, no active work, nothing
# landed") also matches a drive that exited DELIBERATELY — a clean
# `exit_code=1` after diagnosing its own blocker (a merge-queue race in
# #1845, an oracle refusal in #1844) — and used to overwrite that already-
# recorded diagnosis with a synthesised "drive session died" every time. The
# shell reads the drive's own `drive_exited` audit summary and hands it in as
# `exit_reasons`; these tests pin what `_reconcile_running` does with it.


def test_a_dead_drives_own_exit_reason_replaces_the_synthesised_death():
    """#1845: a drive that exited on its own diagnosis must have THAT
    diagnosis carried forward as `last_reason`, not "drive session died"."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    own_reason = (
        "drive exited for api#1650 (exit_code=1): merge attempted 3 times "
        "without landing."
    )
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "retry"  # state transition is unchanged
    assert own_reason in reconcile.reason
    assert own_reason in reconcile.updates["last_reason"]
    assert "drive session died" not in reconcile.reason


def test_an_exhausted_drives_own_exit_reason_replaces_the_synthesised_death():
    """Same fix, at the `exhausted` branch — no regression on retry exhausting
    to `blocked` (the path that recovered #1845's overnight incidents)."""
    entries = [
        entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    own_reason = (
        "drive exited for api#1650 (exit_code=1): a permanent refusal, not "
        "a crash"
    )
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): own_reason},
    )
    assert plan.reconciles[0].outcome == "exhausted"
    assert own_reason in plan.blocked[0].reason
    assert own_reason in plan.blocked[0].updates["last_reason"]
    assert "drive session died" not in plan.blocked[0].reason
    # The reason changed; the outcome — still exhausts, still blocks — did not.
    assert plan.blocked[0].updates["state"] == STATE_BLOCKED
    assert plan.blocked[0].updates["attempts"] == DEFAULT_MAX_ATTEMPTS


def test_no_exit_reason_falls_back_to_the_synthesised_death_wording():
    """No regression: a genuine crash (no `drive_exited` row, or the shell's
    audit fetch came back empty) keeps the pre-#1845 wording exactly."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1, exit_reasons={})
    assert (
        "drive session died without landing the work"
        in plan.reconciles[0].reason
    )


# ═══════════════════════════════════════════════════════════════════════════
# #1891: `parked` — a CI verdict that has not arrived must not consume merge
# budget. Same "no session, no active work, nothing landed" evidence as
# `retry`/`exhausted`, but the board's OWN current read of the issue names
# nothing stronger than "CI checks have not reported yet"
# (`IssueFacts.merge_ci_pending`) — so this goes straight to `STATE_PARKED`
# instead: no attempt spent, no `blocked`, no escalation, and — unlike
# `blocked` — no operator command needed to release it. See
# `coord.drive_queue.STATE_PARKED`'s docstring.
# ═══════════════════════════════════════════════════════════════════════════


def test_a_dead_drive_still_ci_pending_parks_without_spending_an_attempt():
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(ci_pending=(1650,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "parked"
    assert reconcile.updates["state"] == "parked"
    assert "attempts" not in reconcile.updates
    assert plan.blocked == ()
    assert plan.launch is None  # nothing to launch — it is parked, not waiting


def test_a_parked_entry_never_reaches_blocked_even_deep_into_the_attempt_budget():
    """The whole point: an entry that would have exhausted retries (attempts
    already at max_attempts - 1) still parks, not blocks, when the board
    shows nothing but CI silence — attempts genuinely never move."""
    entries = [
        entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    plan = plan_tick(entries, board(ci_pending=(1650,)), capacity=1)
    assert plan.reconciles[0].outcome == "parked"
    assert plan.blocked == ()
    assert plan.alert is None


def test_a_parked_entry_resumes_to_waiting_and_launches_once_ci_reports():
    """Acceptance: a parked entry resumes automatically on a later tick once
    checks report, with no operator command — modelled here as the SAME
    entry, now persisted as `parked`, ticked again against a board that no
    longer shows `merge_ci_pending` for it."""
    entries = [entry(1650, position=3, state="parked", attempts=0)]
    plan = plan_tick(entries, board(), capacity=1)  # ci_pending cleared
    resumed = [r for r in plan.reconciles if r.key == entry_key(REPO, 1650)]
    assert [r.outcome for r in resumed] == ["resumed"]
    assert resumed[0].updates["state"] == STATE_WAITING
    assert "attempts" not in resumed[0].updates
    # …and it falls straight into this SAME tick's launch selection — no
    # human, no separate tick required.
    assert plan.launch is not None and plan.launch.issue == 1650


def test_a_still_parked_entry_is_not_relaunched_while_ci_is_still_pending():
    entries = [entry(1650, position=3, state="parked", attempts=0)]
    plan = plan_tick(entries, board(ci_pending=(1650,)), capacity=1)
    assert plan.reconciles == ()  # still gated — nothing to report or write
    assert plan.launch is None


# ── #2158: a park that cannot refresh itself must age out ──────────────────


def test_a_park_on_an_unrefreshable_reading_ages_out_to_waiting():
    """THE #2158 regression, at the decision level.

    `merge_ci_pending` here is `merge_ci_pending_live=False` — it came only
    from the raw `merge_queue` row's persisted `error`, which no read path
    rewrites. Nothing on this tick's lane can ever refresh it (the board has
    no `merge_plan` section — the daemon-host tick — or the plan carried no
    rollup), so past `PARK_STALE_SECONDS` the tick stops believing it rather
    than holding the entry forever.
    """
    entries = [
        entry(
            2138, position=3, state="parked", attempts=0,
            last_reason="CI running: test (3.12) — parking without spending an attempt",
            reason_at=NOW - PARK_STALE_SECONDS - 60,
        )
    ]
    plan = plan_tick(entries, board(ci_pending=(2138,)), capacity=1, now=NOW)
    resumed = [r for r in plan.reconciles if r.key == entry_key(REPO, 2138)]
    assert [r.outcome for r in resumed] == ["resumed"]
    assert resumed[0].updates["state"] == STATE_WAITING
    assert "attempts" not in resumed[0].updates  # still free, per #1891
    assert "#2158" in resumed[0].reason
    # It does NOT claim CI reported — nothing here knows that.
    assert "have reported" not in resumed[0].reason
    # …and falls into this same tick's launch selection.
    assert plan.launch is not None and plan.launch.issue == 2138


def test_a_park_on_an_unrefreshable_reading_is_held_until_the_ceiling():
    """No hot loop: the ceiling is a backstop, not a second CI timeout. A
    park younger than it stays exactly where it is."""
    entries = [
        entry(
            2138, position=3, state="parked", attempts=0,
            reason_at=NOW - PARK_STALE_SECONDS + 60,
        )
    ]
    plan = plan_tick(entries, board(ci_pending=(2138,)), capacity=1, now=NOW)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_park_on_a_live_plan_reason_never_ages_out():
    """A reading the board re-derives on every build is not stale, however
    old the park is — it will go false by itself the moment CI reports, and
    resuming over a live objection is the hot loop #1891 exists to avoid."""
    entries = [
        entry(
            2138, position=3, state="parked", attempts=0,
            reason_at=NOW - 30 * 3600,  # 30 hours, far past the ceiling
        )
    ]
    plan = plan_tick(entries, board(ci_pending_live=(2138,)), capacity=1, now=NOW)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_park_with_no_capture_time_stays_parked():
    """Fail closed on an unmeasurable age: a row predating #2133's `reason_at`
    (or one whose `last_reason` is still '') must degrade to today's
    behaviour, not to a park that expires by accident."""
    entries = [entry(2138, position=3, state="parked", attempts=0, reason_at=None)]
    plan = plan_tick(entries, board(ci_pending=(2138,)), capacity=1, now=NOW)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_park_stamped_in_the_future_stays_parked():
    """A clock that jumped backwards must not expire a park it cannot age."""
    entries = [
        entry(2138, position=3, state="parked", attempts=0, reason_at=NOW + 10_000)
    ]
    plan = plan_tick(entries, board(ci_pending=(2138,)), capacity=1, now=NOW)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_pure_logic_tick_with_no_clock_never_expires_a_park():
    """`plan_tick` still reads no clock of its own — a caller that passes none
    gets the pre-#2158 behaviour, not an entry aged against `None`."""
    entries = [entry(2138, position=3, state="parked", attempts=0, reason_at=1.0)]
    plan = plan_tick(entries, board(ci_pending=(2138,)), capacity=1)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_an_aged_gate_a_park_is_still_gated_on_the_human():
    """#2063 stays fail-closed THROUGH the #2158 expiry: a Gate-A park waits
    on a human, not on CI, so ageing the CI reading out must not release it.
    """
    from coord import gate_a

    marker = f"parked ... {gate_a.park_marker('api', 37)}"
    entries = [
        entry(
            2138, position=3, state="parked", attempts=0,
            last_reason=marker,
            reason_at=NOW - PARK_STALE_SECONDS - 60,
        )
    ]
    plan = plan_tick(
        entries,
        board(ci_pending=(2138,)),
        capacity=1,
        now=NOW,
        gate_a_pending={entry_key(REPO, 2138): True},
    )
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_parked_entry_that_lands_while_parked_reconciles_to_done():
    entries = [entry(1650, position=3, state="parked", attempts=0)]
    plan = plan_tick(entries, board(merged=(1650,), ci_pending=(1650,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE
    assert plan.launch is None


# ── #2055: `blocked`/`failed` re-checked against the board too ─────────────
#
# #1891's landed re-check above was `parked`-only. `blocked`/`failed` got no
# such check, so an entry that merges by hand while blocked showed as
# `blocked` forever — the board never asked again. These extend the SAME
# `landed` branch to `blocked`/`failed`, without granting them the `parked`
# branch's CI-pending resume (that would resurrect a gave-up entry for
# dispatch, which is explicitly not the fix). See #1956 for the live
# instance this was spotted from.
# ═══════════════════════════════════════════════════════════════════════════


def test_a_blocked_entry_that_lands_reconciles_to_done():
    entries = [entry(1650, position=3, state=STATE_BLOCKED, attempts=2)]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE
    assert plan.launch is None


def test_a_blocked_entry_that_closed_without_merging_also_reconciles_to_done():
    entries = [entry(1650, position=3, state=STATE_BLOCKED, attempts=2)]
    plan = plan_tick(entries, board(closed=(1650,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE


def test_a_still_blocked_entry_is_left_untouched_not_resumed_to_waiting():
    """The whole point of scoping this to the `landed` branch only: a
    blocked entry whose issue has NOT landed must stay blocked — it must
    NOT fall into the `parked` branch's CI-pending resume, which would
    relaunch a gave-up entry outside its attempt budget."""
    entries = [entry(1650, position=3, state=STATE_BLOCKED, attempts=2)]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles == ()  # nothing to report or write
    assert plan.launch is None


def test_a_failed_entry_that_lands_reconciles_to_done():
    entries = [entry(1650, position=3, state=STATE_FAILED, attempts=2)]
    plan = plan_tick(entries, board(merged=(1650,)), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "done"
    assert reconcile.updates["state"] == STATE_DONE


def test_a_still_failed_entry_is_left_untouched():
    entries = [entry(1650, position=3, state=STATE_FAILED, attempts=2)]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles == ()
    assert plan.launch is None


def test_a_genuinely_dead_drive_without_ci_pending_still_retries_normally():
    """No regression: without `merge_ci_pending`, a dead drive takes the
    EXACT pre-#1891 path — this is byte-for-byte
    `test_a_dead_drive_is_requeued_at_the_same_position_with_an_attempt_spent`
    with an explicit (empty) `board()`, pinning that the new branch is
    opt-in, not a default."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(entries, board(), capacity=1)
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "retry"
    assert reconcile.updates["state"] == STATE_WAITING
    assert reconcile.updates["attempts"] == 1


# ── plan_tick: a permanent dispatch refusal blocks straight away (#1844) ────
#
# The defect #1845 did NOT fix: `_reconcile_running`'s death branch treats a
# drive refused by a deterministic pre-dispatch guard (`enforce_oracle_
# readiness`, `enforce_epic_dispatch_guard`) exactly like a genuine crash —
# it retries the identical guaranteed-to-fail dispatch, burns an attempt, and
# only reaches `blocked` after `max_attempts` is exhausted. This is the exact
# #1817 overnight shape: two identical, fully actionable refusals were
# retried and only THEN blocked, discarding the guard's own remedy along the
# way (`exit_reasons` alone, #1845's fix, only changes the WORDING of that
# outcome — see the tests above). `exit_refused` is what changes the
# DECISION: straight to `blocked`, attempts untouched, on the FIRST tick.


REFUSAL = (
    "drive exited for claude-coordinator#1817 (exit_code=5): dispatch failed: "
    "Issue #1817 is part of oracle-opted-in milestone ms-51 (Gate A "
    "satisfied) but has no acceptance slice yet — run `coord acceptance "
    "author claude-coordinator <tracking_issue> --issue 1817` first."
)


def test_a_permanent_refusal_blocks_immediately_with_attempts_unspent():
    """The acceptance criterion, asserted the way the issue insists on: on
    the attempt counter, not just the final state."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): REFUSAL},
        exit_refused={entry_key(REPO, 1650): True},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "refused"
    assert reconcile.occupies is False
    # NOT `retry`: no requeue, no attempt spent — check both the reconcile
    # and the paired Blocked never touch `attempts` at all (a bare `0` would
    # also satisfy "attempts == 0" but wrongly imply a write happened).
    assert "attempts" not in reconcile.updates
    assert [b.key for b in plan.blocked] == [entry_key(REPO, 1650)]
    blocked = plan.blocked[0]
    assert "attempts" not in blocked.updates
    assert blocked.updates["state"] == STATE_BLOCKED
    # The guard's own message — remedy included — survives verbatim into
    # both the reconcile log line and what `coord drive-queue list`/`status`
    # will show as `last_reason`.
    assert REFUSAL in reconcile.reason
    assert REFUSAL in blocked.reason
    assert REFUSAL in blocked.updates["last_reason"]
    assert "coord acceptance author" in blocked.updates["last_reason"]
    assert "drive session died" not in blocked.reason


def test_a_permanent_refusal_blocks_even_on_a_fresh_entrys_first_tick():
    """Not just "no MORE attempts spent" — no attempt at all, ever, for a
    refusal observed on attempt 0. `entry.attempts` (the pre-tick value)
    must be what an operator sees after re-adding this exact entry."""
    entries = [entry(1817, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1817): REFUSAL},
        exit_refused={entry_key(REPO, 1817): True},
    )
    assert plan.reconciles[0].outcome == "refused"
    assert plan.blocked[0].updates.get("attempts") is None
    assert plan.launch is None


def test_exit_reason_without_the_refused_flag_still_retries_normally():
    """`exit_reasons` alone (a genuine death that happened to narrate why,
    #1845) must NOT trip the new `refused` branch — only `exit_refused`
    does. No regression on the #1845 behaviour pinned above."""
    entries = [entry(1650, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1650): REFUSAL},
        exit_refused={},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "retry"
    assert reconcile.updates["attempts"] == 1


def test_a_refused_entry_deep_into_its_attempt_budget_still_spends_none():
    """`exit_refused` short-circuits regardless of how many attempts this
    entry has already burned on genuine deaths — the LAST attempt is not
    "closer to exhausted", it is still a refusal, still costs nothing."""
    entries = [
        entry(1817, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)
    ]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1817): REFUSAL},
        exit_refused={entry_key(REPO, 1817): True},
    )
    assert plan.reconciles[0].outcome == "refused"  # not "exhausted"
    assert "attempts" not in plan.blocked[0].updates


def test_a_genuine_death_still_retries_and_exhausts_with_no_refused_flag():
    """Regression bar: the retry mechanism this issue explicitly leaves
    alone. Three genuine deaths recovered on attempt 2 the same overnight
    run #1844 is named for — this must keep working exactly as #1794/#1845
    left it when `exit_refused` says nothing (the default, `None`)."""
    entries = [entry(1650, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)]
    plan = plan_tick(entries, board(), capacity=1)
    assert plan.reconciles[0].outcome == "exhausted"
    assert plan.blocked[0].updates["attempts"] == DEFAULT_MAX_ATTEMPTS


# ── plan_tick: a dead-end exit blocks the entry (#2019) ──────────────────────
#
# The second PERMANENT cause, sharing #1844's branch. Before #2019 this shape
# never reached the queue at all — the drive did not exit, it counted `no
# state change` for 140 minutes while holding the tmux session, the queue slot
# and (since #1972) the whole repo's capacity lane. Now it exits within one
# poll, and the tick's job is to make that visible in `coord drive-queue
# list`/`status` with the SPECIFIC reason, without spending an attempt on a
# relaunch that would reproduce the identical dead end.

DEAD_END_REASON = (
    "drive exited for claude-coordinator#1956 (exit_code=6): DEAD END "
    "[review_terminal_no_verdict] — this row is terminal and unactionable; "
    "exiting instead of polling (#2019).\n   review c9b489b2333e reached "
    "status=done carrying NO verdict.\n   Recover: coord report-result "
    "--assignment c9b489b2333e --status done --verdict "
    "<approve|request-changes> --verdict-source recovered ..."
)


def test_a_dead_end_exit_blocks_immediately_with_attempts_unspent():
    """#2019 acceptance: "the queue entry is left `blocked` with a reason
    naming the specific dead end and the recovery command"."""
    entries = [entry(1956, position=3, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1956): DEAD_END_REASON},
        exit_dead_end={entry_key(REPO, 1956): True},
    )
    reconcile = plan.reconciles[0]
    assert reconcile.outcome == "dead_end"
    assert reconcile.occupies is False
    # No relaunch, no attempt spent — a dead end is not a flaky death.
    assert "attempts" not in reconcile.updates
    assert plan.launch is None
    blocked = plan.blocked[0]
    assert blocked.key == entry_key(REPO, 1956)
    assert "attempts" not in blocked.updates
    assert blocked.updates["state"] == STATE_BLOCKED
    # The specific dead end AND its recovery command reach `last_reason`,
    # which is what `coord drive-queue list`/`status` render. "no state
    # change in 140.558m" is what this replaces.
    last_reason = blocked.updates["last_reason"]
    assert "review_terminal_no_verdict" in last_reason
    assert "coord report-result --assignment c9b489b2333e" in last_reason
    assert "drive session died" not in last_reason


def test_a_dead_end_reason_is_not_reported_as_a_pre_dispatch_refusal():
    """Two permanent causes, one branch — but never one wording. #1844's
    "refused by a pre-dispatch guard" would send an operator hunting for a
    guard that never fired."""
    entries = [entry(1956, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1956): DEAD_END_REASON},
        exit_dead_end={entry_key(REPO, 1956): True},
    )
    reason = plan.blocked[0].reason
    assert "pre-dispatch guard" not in reason
    assert "terminal and unactionable" in reason
    assert "#2019" in reason


def test_a_dead_end_entry_deep_into_its_attempt_budget_still_spends_none():
    """Same short-circuit property #1844 asserts: how many attempts a genuine
    death already burned is irrelevant — a dead end still costs nothing and
    reports as `dead_end`, never `exhausted`."""
    entries = [entry(1956, state=STATE_RUNNING, attempts=DEFAULT_MAX_ATTEMPTS - 1)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1956): DEAD_END_REASON},
        exit_dead_end={entry_key(REPO, 1956): True},
    )
    assert plan.reconciles[0].outcome == "dead_end"
    assert "attempts" not in plan.blocked[0].updates


def test_exit_reason_without_the_dead_end_flag_still_retries_normally():
    """The #1845 regression bar, restated for the new flag: narrating a
    reason must not by itself block anything."""
    entries = [entry(1956, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1956): DEAD_END_REASON},
        exit_dead_end={},
    )
    assert plan.reconciles[0].outcome == "retry"
    assert plan.reconciles[0].updates["attempts"] == 1


def test_refused_still_wins_and_still_reads_as_refused():
    """#1844's path must be untouched by #2019 sharing its branch. The two
    exit codes are mutually exclusive by construction, but if both flags ever
    arrive for one key the pre-dispatch wording is what shows."""
    entries = [entry(1817, state=STATE_RUNNING, attempts=0)]
    plan = plan_tick(
        entries, board(), capacity=1,
        exit_reasons={entry_key(REPO, 1817): REFUSAL},
        exit_refused={entry_key(REPO, 1817): True},
        exit_dead_end={entry_key(REPO, 1817): True},
    )
    assert plan.reconciles[0].outcome == "refused"
    assert "pre-dispatch guard" in plan.blocked[0].reason


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
        entries,
        board(open_=(1812,)),
        capacity=5,
        now=NOW,
        local_host="dellserver",
        # Same repo on both rows: the #1870 guard is what this test is about,
        # so #1972's per-repo ceiling is raised out of the way (with the
        # default of 1, #1811's cross-host slot would legitimately defer
        # #1812 and the assertion below would be testing the wrong feature).
        max_parallel_per_repo=5,
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


# ── plan_tick: deploy gates (#1757, scoped by #2186) ─────────────────────────
#
# `merged != live`. These pin the decision half of the gate: what fires it,
# what does NOT fire it, and — since #2186 — HOW FAR a fired gate reaches.
# The default scope (`entry`, unset in `held()` below) holds only entries
# that name the gated key in their own `after=`; `HOLD_SCOPE_FLEET` is the
# pre-#2186 whole-queue stop, kept for an explicit `--scope=fleet`.


def held(issue: int, **kw) -> QueueEntry:
    """A `--hold-after` entry whose gate has already fired (default scope)."""
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
            entry(2, after=(entry_key(REPO, 1),)),
        ],
        board(merged=(1,), open_=(2,)),
        capacity=1,
    )
    assert plan.launch is None
    assert plan.held is not None
    assert plan.held.outcome == "fired"
    assert plan.held.scope == HOLD_SCOPE_ENTRY
    assert dict(plan.writes())[entry_key(REPO, 1)]["hold_state"] == HOLD_FIRED


def test_2186_a_fired_entry_scoped_gate_does_not_block_an_unrelated_successor():
    """THE #2186 fix, in one assertion: entry-scoped is the default.

    Black-box acceptance scenario from the issue: a fired gate on entry A
    (position 0) and a fully eligible, UNRELATED entry B (position 1) — B
    launches in the same tick, even though A's gate is still closed.
    """
    plan = plan_tick(
        [held(1), entry(2)],
        board(open_=(2,)),
        capacity=4,
    )
    assert plan.free_slots == 4
    assert plan.launch is not None and plan.launch.issue == 2
    # The gate is still on record as closed — it just never stopped the tick.
    assert plan.held is not None
    assert plan.held.outcome == "held"
    assert not plan.held.stops_fleet


def test_a_fired_entry_scoped_gate_still_holds_its_own_dependent():
    """The other half of #2186: scoping the hold must not mean removing it."""
    plan = plan_tick(
        [held(1), entry(2, after=(entry_key(REPO, 1),))],
        board(),
        capacity=4,
    )
    assert plan.launch is None
    assert [d.key for d in plan.deferrals] == [entry_key(REPO, 2)]
    reason = plan.deferrals[0].reason
    assert "deploy gate" in reason
    assert "restart coord-serve" in reason
    # #2186 acceptance: the reason is written to the row every tick (via the
    # ordinary deferral path), not frozen the way a queue-wide short-circuit
    # would leave it — this is what keeps `coord drive-queue list` honest.
    assert dict(plan.writes())[entry_key(REPO, 2)]["last_reason"] == reason


def test_a_fired_fleet_scoped_gate_still_blocks_a_fully_eligible_successor():
    """The pre-#2186 behaviour, preserved for an explicit `--scope=fleet`."""
    plan = plan_tick(
        [held(1, hold_scope=HOLD_SCOPE_FLEET), entry(2)],
        board(open_=(2,)),
        capacity=4,
    )
    assert plan.free_slots == 4
    assert plan.launch is None
    assert plan.deferrals == ()
    assert "restart coord-serve" in plan.alert.reason
    assert plan.alert.command == "coord drive-queue resume"
    assert plan.held.stops_fleet


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
        [
            held(1, resume_when="curl -sf x", hold_probes=2),
            entry(2, after=(key,)),
        ],
        board(open_=(2,)),
        capacity=1,
        probes={key: ProbeResult(key, False, "exit 7")},
    )
    assert plan.launch is None
    assert plan.held.probes == 3
    assert dict(plan.writes())[key]["hold_probes"] == 3
    # #2186: the dependent's own deferral carries the probe detail now — the
    # fleet-wide `QUEUE HELD` alert this used to come from only fires for an
    # explicit `--scope=fleet` gate.
    alert_text = " ".join(plan.alert.details) if plan.alert else ""
    assert "attempt 3 failed" in alert_text
    assert dict(plan.writes())[entry_key(REPO, 2)]["last_reason"] == (
        plan.deferrals[0].reason
    )


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


def test_a_fleet_scoped_gate_with_no_probe_result_stays_held_and_writes_nothing():
    """Manual-resume-only, and a probe the shell could not run. Fail closed."""
    plan = plan_tick(
        [held(1, hold_scope=HOLD_SCOPE_FLEET), entry(2)],
        board(open_=(2,)),
        capacity=1,
    )
    assert plan.launch is None
    assert plan.writes() == []
    assert "release manually" in " ".join(plan.alert.details)


def test_an_entry_scoped_gate_with_no_probe_result_defers_its_dependent_live():
    """Same fail-closed rule, but scoped: only the dependent is affected, and
    its reason is written fresh every tick rather than frozen."""
    plan = plan_tick(
        [held(1), entry(2, after=(entry_key(REPO, 1),))],
        board(),
        capacity=4,
    )
    assert plan.launch is None
    assert len(plan.deferrals) == 1
    d = plan.deferrals[0]
    assert d.key == entry_key(REPO, 2)
    assert "restart coord-serve" in d.reason
    assert dict(plan.writes())[entry_key(REPO, 2)]["last_reason"] == d.reason


def test_only_an_already_fired_gate_is_offered_for_probing():
    from coord.drive_queue import pending_probe_targets

    entries = [
        entry(1, hold_after=True, hold_state=HOLD_ARMED, resume_when="a"),
        held(2, resume_when="b"),
        held(3),  # fired, but no probe declared
        held(4, hold_state=HOLD_RELEASED, resume_when="d"),
    ]
    assert [e.issue for e in pending_probe_targets(entries)] == [2]


def test_render_plan_says_why_a_fleet_scoped_hold_stopped_everything():
    plan = plan_tick(
        [held(1, hold_scope=HOLD_SCOPE_FLEET), entry(2)],
        board(open_=(2,)),
        capacity=1,
    )
    text = "\n".join(render_plan(plan))
    assert "hold claude-coordinator#1: held" in text
    assert "[scope=fleet]" in text
    assert "no launch — HELD" in text
    assert "fleet-wide" in text
    assert "coord drive-queue resume" in text


def test_render_plan_narrates_an_entry_scoped_hold_as_a_defer_not_a_queue_stop():
    plan = plan_tick(
        [held(1), entry(2, after=(entry_key(REPO, 1),))],
        board(),
        capacity=1,
    )
    text = "\n".join(render_plan(plan))
    assert "hold claude-coordinator#1: held" in text
    assert "[scope=fleet]" not in text
    assert "no launch — HELD" not in text
    assert (
        f"defer {entry_key(REPO, 2)}: waiting on {entry_key(REPO, 1)}'s deploy gate"
        in text
    )
