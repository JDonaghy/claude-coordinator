"""Unit tests for coord/block_log.py — #2235's Phase-0 stall log.

The load-bearing claim of #2235 is that the queue's *stated* block reason and
the *actual* cause disagree most of the time (five of seven, on the morning
that motivated the plan), and that Phase 1's scope must be decided from two
weeks of measured evidence rather than from that one sample. This file pins
the recorder that produces the evidence:

* it records a transition INTO and OUT OF ``blocked``/``parked``, and nothing
  else — a blocked entry that stays blocked is not an event;
* the stated reason is stored VERBATIM, and ``true_cause`` is empty on the way
  in, because at that instant nobody knows it;
* the release itself supplies the cause, classified from the marker the queue's
  own reconcile branch wrote (#2230 / #1891 / #2158 / #2063 / #2055);
* ``human_acted`` is true for exactly the releases a person caused — an
  operator ``remove``, and the Gate-A sign-off that #2063 polls for;
* the writer never raises, because it runs inside a tick that is mid-way
  through applying real merge/attempt state.

The CLI-level suite (tests/test_cli_drive_queue.py) drives the same machinery
end to end through ``coord drive-queue tick`` / ``block-log``.
"""

from __future__ import annotations

import json

from coord.block_log import (
    EVENT_ENTER,
    EVENT_RESOLVE,
    MAX_LOG_BYTES,
    block_log_path,
    enter_event,
    episodes,
    operator_resolution_event,
    plan_events,
    read_events,
    record,
    summarize,
)
from coord.drive_queue import (
    STATE_BLOCKED,
    STATE_DONE,
    STATE_PARKED,
    STATE_WAITING,
    Blocked,
    QueueEntry,
    Reconcile,
    TickPlan,
)

REPO = "claude-coordinator"
NOW = 1_800_000_000.0


def entry(issue: int, **kw) -> QueueEntry:
    base: dict = {"repo": REPO, "issue": issue, "position": issue}
    base.update(kw)
    return QueueEntry(**base)


# ── enter events ─────────────────────────────────────────────────────────────


def test_an_entry_that_reaches_blocked_is_recorded_with_the_reason_the_queue_stated():
    """#2235's whole premise: capture what the queue SAID, verbatim.

    The reason here is the shape of the real ``claude-coordinator#2143`` row —
    "CI red, 2/2 attempts" — which was wrong 23 minutes later. It has to land
    in the log unedited, because the comparison against what the release
    reveals is the measurement.
    """
    stated = "checks_failed: build red (attempt 2/2)"
    plan = TickPlan(
        reconciles=(
            Reconcile("claude-coordinator#2143", "exhausted", stated,
                      updates={"attempts": 2, "last_reason": stated}),
        ),
        blocked=(
            Blocked("claude-coordinator#2143", stated,
                    updates={"state": STATE_BLOCKED, "last_reason": stated}),
        ),
    )
    events = plan_events([entry(2143, attempts=1)], plan, host="dellserver", now=NOW)

    assert len(events) == 1
    (event,) = events
    assert event["event"] == EVENT_ENTER
    assert event["state"] == STATE_BLOCKED
    assert event["from_state"] == STATE_WAITING
    assert event["stated_reason"] == stated
    assert event["host"] == "dellserver"
    assert event["ts"] == NOW


def test_an_enter_record_never_guesses_at_the_true_cause():
    """The one thing a Phase-0 `enter` must NOT do.

    Nothing at block time knows why the entry really stopped — that is the
    entire finding. A record that filled `true_cause` in here would be
    recording the stated reason twice under two names, and the dataset Phase 1
    is meant to be sized from would agree with itself by construction.
    """
    plan = TickPlan(
        blocked=(
            Blocked(f"{REPO}#309", "stale test verdict",
                    updates={"state": STATE_BLOCKED, "last_reason": "stale test verdict"}),
        ),
    )
    (event,) = plan_events([entry(309)], plan, now=NOW)
    assert event["true_cause"] == ""
    assert event["human_acted"] is None


def test_a_parked_entry_is_recorded_just_like_a_blocked_one():
    """`parked` is half of what #2235 asks to instrument, not a lesser state.

    coord-portal#71 and claude-coordinator#2138 both burnt a night parked, so a
    log that counted only `blocked` would under-report the thing it exists to
    measure.
    """
    reason = "CI checks have not reported yet (#1891)"
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#2138", "parked", reason,
                      updates={"state": STATE_PARKED, "last_reason": reason}),
        ),
    )
    (event,) = plan_events([entry(2138, state="running")], plan, now=NOW)
    assert event["event"] == EVENT_ENTER
    assert event["state"] == STATE_PARKED
    assert event["from_state"] == "running"


def test_an_entry_that_was_already_blocked_and_stays_blocked_records_nothing():
    """This log counts transitions, not tick-seconds.

    #2230's `oscillating` outcome rewrites `last_reason` on EVERY tick while
    the ceiling is hit. Recording that would bury the seven real stalls of a
    night under hundreds of rows saying the same thing, which is the exact
    "trains you to stop reading its output" failure #2235 warns about for
    Phase 3's issue filing.
    """
    reason = "gate reads clear but has already been auto-resumed 2 time(s) (#2230)"
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#309", "oscillating", reason,
                      updates={"last_reason": reason}),
        ),
    )
    assert plan_events([entry(309, state=STATE_BLOCKED)], plan, now=NOW) == []


def test_a_normal_launch_produces_no_record_at_all():
    """A healthy tick must be silent here, or the file is noise by week two."""
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#100", "alive", "session alive", occupies=True),
        ),
    )
    assert plan_events([entry(100, state="running")], plan, now=NOW) == []


# ── resolve events ───────────────────────────────────────────────────────────


def test_a_2230_auto_resume_is_recorded_as_a_stale_stated_reason_and_no_human():
    """The #2143 shape, mechanised: the gate cleared after the queue gave up.

    This is the single most important classification in the log — it is the
    category #2235 predicts the mechanism fixes have already eliminated, and
    the only way to check that prediction is to count it.
    """
    release = (
        f"{REPO}#2143's merge gate reads clear now (no gate objection) — resuming "
        "from blocked without an operator remove+add, attempt budget reset "
        "(resume 1/2) (#2230)"
    )
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#2143", "resumed", release,
                      updates={"state": STATE_WAITING, "attempts": 0,
                               "resumes": 1, "last_reason": release}),
        ),
    )
    (event,) = plan_events(
        [entry(2143, state=STATE_BLOCKED, last_reason="checks_failed")], plan, now=NOW
    )
    assert event["event"] == EVENT_RESOLVE
    assert event["resolution"] == "auto_resumed"
    assert event["human_acted"] is False
    assert event["true_cause"].startswith("gate-cleared-after-giveup")
    # The reason it was blocked UNDER is preserved on the resolve record too,
    # so a reader who greps only for resolutions still sees the disagreement.
    assert event["stated_reason"] == "checks_failed"


def test_a_gate_a_release_is_the_one_auto_resume_that_counts_as_human_action():
    """#2063 polls for a HUMAN's signature; the tick just notices it arrived.

    Counting this as an automatic release would be the most flattering
    possible lie for the "interventions per night" metric — the human did all
    the work and the queue takes the credit. Gate A exists precisely to
    require a person (#2063), and coord-portal#70 in the evidence table is
    logged as needing one, correctly.
    """
    release = f"Gate A sign-off recorded for {REPO}#70 — resuming from parked (#2063)"
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#70", "resumed", release,
                      updates={"state": STATE_WAITING, "last_reason": release}),
        ),
    )
    (event,) = plan_events([entry(70, state=STATE_PARKED)], plan, now=NOW)
    assert event["human_acted"] is True
    assert event["true_cause"].startswith("gate-a-signed")


def test_a_2055_landing_is_recorded_as_the_stated_reason_having_outlived_itself():
    """"The queue gave up" and "the work is done" are independent facts."""
    reason = "done — issue already merged while blocked (#2055)"
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#1956", "done", reason,
                      updates={"state": STATE_DONE, "last_reason": reason,
                               "session_name": None}),
        ),
    )
    (event,) = plan_events([entry(1956, state=STATE_BLOCKED)], plan, now=NOW)
    assert event["resolution"] == "landed"
    assert event["human_acted"] is False
    assert event["true_cause"].startswith("already-landed")


def test_a_2158_park_expiry_is_not_reported_as_ci_having_reported():
    """#2158's own docstring insists on this distinction; so does the log.

    The ceiling means "this reading has no writer left and has aged out", NOT
    "CI came back green". Conflating them would make an instrumentation
    artefact look like a healthy release in the two-week tally.
    """
    release = (
        f"park reason for {REPO}#2138 has not been refreshable for 45m (CI) — "
        "re-evaluating from waiting without spending an attempt (#2158)"
    )
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#2138", "resumed", release,
                      updates={"state": STATE_WAITING, "last_reason": release}),
        ),
    )
    (event,) = plan_events([entry(2138, state=STATE_PARKED)], plan, now=NOW)
    assert event["true_cause"].startswith("unrefreshable-reading")
    assert "ci-reported" not in event["true_cause"]


def test_an_operator_remove_is_always_recorded_as_a_human_intervention():
    """`remove && add` IS the documented one-key fix, so it IS the metric.

    #2235's success metric is interventions per night, and this is what an
    intervention looks like from inside the process.
    """
    event = operator_resolution_event(
        entry(2195, state=STATE_BLOCKED, last_reason="advisory — 0 commits"),
        resolution="operator_removed",
        host="dellserver",
        now=NOW,
    )
    assert event["event"] == EVENT_RESOLVE
    assert event["human_acted"] is True
    assert event["source"] == "operator"
    assert event["stated_reason"] == "advisory — 0 commits"
    assert event["true_cause"].startswith("operator-intervened")


def test_an_operator_resolution_does_not_pretend_to_know_what_was_fixed():
    """The honest non-answer beats a plausible one.

    Whatever the human did happened outside this process. Guessing here would
    poison exactly the column Phase 1 is supposed to size itself from.
    """
    event = operator_resolution_event(
        entry(2195, state=STATE_BLOCKED, last_reason="advisory — 0 commits"),
        resolution="operator_removed",
        now=NOW,
    )
    assert "NOT recorded" in event["true_cause"]


def test_a_launch_failure_blocks_outside_the_plan_and_is_still_recorded():
    """stick-demo#1's row: "dispatch failed", invisible to `plan.writes()`.

    The `coord drive --tmux` subprocess exits non-zero AFTER the plan has been
    applied, so this transition can only ever be reported by its own branch. A
    log that missed it would omit a whole category of overnight stall.
    """
    event = enter_event(
        entry(1, state=STATE_WAITING, attempts=1),
        state=STATE_BLOCKED,
        reason="launch failed (exit 1): agent repo list frozen",
        attempts=2,
        host="dellserver",
        now=NOW,
    )
    assert event["event"] == EVENT_ENTER
    assert event["outcome"] == "launch_failed"
    # The branch recomputes `attempts` because the row has already moved on;
    # the record must show the post-write value, not the stale snapshot.
    assert event["attempts"] == 2


# ── persistence ──────────────────────────────────────────────────────────────


def test_record_round_trips_through_the_file(tmp_path):
    path = tmp_path / "log.jsonl"
    assert record([{"event": EVENT_ENTER, "ts": NOW, "key": "a#1"}], path=path) == 1
    assert record([{"event": EVENT_RESOLVE, "ts": NOW + 5, "key": "a#1"}], path=path) == 1
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == EVENT_ENTER


def test_record_never_raises_when_the_log_cannot_be_written(tmp_path):
    """The tick is mid-way through applying merge and attempt state.

    #2235's own prohibition list ends with "never mutate board state"; the
    corollary for instrumentation is that it must never be able to *fail* one
    either. A path whose parent is a regular file is the cheapest way to make
    every filesystem call blow up.
    """
    blocker = tmp_path / "notadir"
    blocker.write_text("x")
    assert record([{"event": EVENT_ENTER, "key": "a#1"}], path=blocker / "log.jsonl") == 0


def test_record_drops_an_unserialisable_event_rather_than_the_whole_batch(tmp_path):
    path = tmp_path / "log.jsonl"
    written = record(
        [{"key": "a#1", "bad": object()}, {"key": "a#2", "ts": NOW}], path=path
    )
    assert written == 1
    assert json.loads(path.read_text().strip())["key"] == "a#2"


def test_read_events_skips_a_torn_final_line(tmp_path):
    """A power loss mid-append must not make two weeks unreadable."""
    path = tmp_path / "log.jsonl"
    path.write_text(
        json.dumps({"event": EVENT_ENTER, "ts": NOW, "key": "a#1"}) + "\n{\"event\": \"en"
    )
    events = read_events(path=path)
    assert [e["key"] for e in events] == ["a#1"]


def test_read_events_on_a_missing_file_is_empty_not_an_error(tmp_path):
    assert read_events(path=tmp_path / "nope.jsonl") == []


def test_the_log_rotates_once_it_passes_its_ceiling(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text("x" * (MAX_LOG_BYTES + 1))
    record([{"key": "a#1", "ts": NOW}], path=path)
    assert path.with_suffix(".jsonl.1").exists()
    assert json.loads(path.read_text().strip())["key"] == "a#1"


def test_the_log_path_is_resolved_at_call_time(monkeypatch, tmp_path):
    """Same rationale as `drive_queue_lock_path`: a constant freezes $HOME."""
    monkeypatch.setenv("COORD_BLOCK_LOG", str(tmp_path / "custom.jsonl"))
    assert block_log_path() == tmp_path / "custom.jsonl"


# ── episodes + summary ───────────────────────────────────────────────────────


def test_an_enter_and_its_resolve_pair_into_one_episode_with_a_duration():
    events = [
        {"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#2143",
         "state": STATE_BLOCKED, "stated_reason": "checks_failed"},
        {"event": EVENT_RESOLVE, "ts": NOW + 1380, "key": f"{REPO}#2143",
         "resolution": "auto_resumed", "true_cause": "gate-cleared-after-giveup — x",
         "human_acted": False},
    ]
    (episode,) = episodes(events)
    assert episode["resolved"] is True
    assert episode["stalled_seconds"] == 1380
    assert episode["stated_reason"] == "checks_failed"
    assert episode["true_cause"].startswith("gate-cleared")


def test_an_unresolved_enter_is_still_an_episode():
    """"Blocked and still blocked" is the outcome the plan cares most about.

    Dropping it would flatter every number in the summary — the overnight
    stall that nobody rescued is the whole problem statement.
    """
    (episode,) = episodes(
        [{"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#309",
          "state": STATE_BLOCKED, "stated_reason": "stale test verdict"}]
    )
    assert episode["resolved"] is False
    assert episode["stalled_seconds"] is None


def test_a_resolve_with_no_recorded_enter_is_dropped():
    """The log started mid-stall; an episode with no stated reason answers
    nothing, and synthesising one would invent evidence."""
    assert episodes(
        [{"event": EVENT_RESOLVE, "ts": NOW, "key": f"{REPO}#309",
          "resolution": "auto_resumed"}]
    ) == []


def test_a_second_enter_closes_the_previous_episode_rather_than_losing_it():
    """A release recorded on another host leaves a dangling `enter`."""
    got = episodes([
        {"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#309",
         "state": STATE_BLOCKED, "stated_reason": "first"},
        {"event": EVENT_ENTER, "ts": NOW + 60, "key": f"{REPO}#309",
         "state": STATE_BLOCKED, "stated_reason": "second"},
    ])
    assert [e["stated_reason"] for e in got] == ["first", "second"]
    assert all(e["resolved"] is False for e in got)


def test_the_summary_reports_human_and_open_counts_together():
    """#2235's metric only means anything as a pair.

    A queue that needs zero interventions because it leaves everything blocked
    forever is the failure mode, not the goal, so `human_acted` is never
    reported without `open` beside it.
    """
    stats = summarize([
        {"key": f"{REPO}#1", "state": STATE_BLOCKED, "resolved": True,
         "human_acted": True, "true_cause": "operator-intervened — x"},
        {"key": f"{REPO}#2", "state": STATE_BLOCKED, "resolved": True,
         "human_acted": False, "true_cause": "gate-cleared-after-giveup — x"},
        {"key": f"{REPO}#3", "state": STATE_PARKED, "resolved": False,
         "human_acted": None, "true_cause": ""},
    ])
    assert stats == {
        "episodes": 3,
        "by_state": {STATE_BLOCKED: 2, STATE_PARKED: 1},
        "by_cause": {
            "operator-intervened": 1,
            "gate-cleared-after-giveup": 1,
            "(unresolved)": 1,
        },
        "human_acted": 1,
        "auto_released": 1,
        "open": 1,
        "repeat_causes": {},
    }


def test_the_same_repo_stalling_twice_on_the_same_reason_shows_up_as_a_repeat():
    """#2235's tripwire: "a repeat is a bug report, not a success".

    Without this visible from day one, a rescue that quietly patches the same
    defect every night looks like the metric improving.
    """
    stats = summarize([
        {"key": f"{REPO}#1", "state": STATE_BLOCKED, "resolved": True,
         "human_acted": True, "stated_reason": "stale test verdict"},
        {"key": f"{REPO}#2", "state": STATE_BLOCKED, "resolved": True,
         "human_acted": True, "stated_reason": "stale test verdict"},
    ])
    assert stats["repeat_causes"] == {f"{REPO}: stale test verdict": 2}
