"""The propagation decision half (#1835, PKG-7).

`coord/release_propagate.py` decides three things a merge-triggered release
lives or dies by, and all three are testable without a fleet:

1. **Is there a window?** Getting this wrong does not produce a wrong report
   — it produces destroyed work. `coord agent update` restarts the agent and
   the restart kills every in-flight headless worker, so a false "quiescent"
   is a queue of overnight drives silently thrown away.

2. **Is a fired deploy gate busy or is it an invitation?** #1757's
   `--hold-after` stops the queue *waiting for a deploy*. Propagation IS that
   deploy. Reading a fired hold as "busy" would deadlock the fleet forever:
   the queue waits for the deploy, the deploy waits for the queue.

3. **What order may lanes roll in?** A caller must never reach an endpoint
   its daemon predates — the documented 405. So the daemon host leads,
   always, and that is an invariant worth a test rather than a comment.

The journal is tested too, for the reason #1835 names: "a silent success is
indistinguishable from a silent no-op, which is precisely how 2026-08-04
stayed invisible."
"""

from __future__ import annotations

import json

import pytest

from coord import release_propagate as rp
from coord.drive_queue import HOLD_ARMED, HOLD_FIRED, STATE_RUNNING, STATE_WAITING


# ── quiescence ───────────────────────────────────────────────────────────


def test_an_empty_fleet_is_quiescent():
    q = rp.assess_quiescence(queue_entries=[], assignments=[])
    assert q.quiescent
    assert q.busy == ()
    assert "nothing in flight" in q.reason


def test_a_running_queue_entry_blocks_propagation():
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "claude-coordinator", "issue_number": 1835,
             "state": STATE_RUNNING},
        ],
        assignments=[],
    )
    assert not q.quiescent
    assert len(q.busy) == 1
    # Named down to the entry: a deferral nobody can explain is a deferral
    # nobody can distinguish from a wedged timer.
    assert "claude-coordinator#1835" in q.reason


def test_a_waiting_queue_entry_is_not_busy():
    """A queue with work *queued* but nothing launched is exactly the window
    propagation wants — roll now, before the next drive starts."""
    q = rp.assess_quiescence(
        queue_entries=[{"repo_name": "r", "issue_number": 7, "state": STATE_WAITING}],
        assignments=[],
    )
    assert q.quiescent


@pytest.mark.parametrize("status", ["RUNNING", "PENDING", "running", "pending"])
def test_a_live_assignment_blocks_propagation(status):
    q = rp.assess_quiescence(
        queue_entries=[],
        assignments=[{"machine_name": "dellserver", "issue_number": 42,
                      "status": status}],
    )
    assert not q.quiescent
    assert "dellserver:42" in q.reason


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "MERGED", "CANCELLED"])
def test_a_terminal_assignment_does_not_block(status):
    q = rp.assess_quiescence(
        queue_entries=[],
        assignments=[{"machine_name": "dellserver", "issue_number": 42,
                      "status": status}],
    )
    assert q.quiescent


def test_a_fired_deploy_gate_is_an_invitation_not_a_blocker():
    """#1757's gate stops the queue *waiting for a deploy*. Propagation is
    that deploy. Counting it as busy deadlocks the fleet: the queue waits for
    the deploy and the deploy waits for the queue."""
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "claude-coordinator", "issue_number": 1543,
             "state": "done", "hold_state": HOLD_FIRED},
        ],
        assignments=[],
    )
    assert q.quiescent
    assert q.fired_holds == ("claude-coordinator#1543",)
    assert "waiting on exactly this deploy" in q.reason


def test_an_armed_but_unfired_gate_is_not_collected():
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "r", "issue_number": 1, "state": STATE_WAITING,
             "hold_state": HOLD_ARMED},
        ],
        assignments=[],
    )
    assert q.quiescent
    assert q.fired_holds == ()


def test_a_running_entry_that_also_holds_a_gate_still_blocks():
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING,
             "hold_state": HOLD_FIRED},
        ],
        assignments=[],
    )
    assert not q.quiescent


def test_extra_busy_signals_are_honoured():
    """The seam for facts the board cannot see — e.g. `/board` itself being
    unreadable, which must defer rather than be read as 'idle'."""
    q = rp.assess_quiescence(
        extra_busy=[rp.Busy(kind="board unreadable", subject="/board",
                            detail="ConnectError")]
    )
    assert not q.quiescent
    assert "board unreadable" in q.reason


def test_queue_key_falls_back_across_row_spellings():
    """`/board` publishes sqlite columns; a rendered row carries `key`. Both
    must resolve — `coord drive-queue resume` needs the real key."""
    fired = rp.assess_quiescence(
        queue_entries=[{"key": "quadraui#302", "state": "done",
                        "hold_state": HOLD_FIRED}]
    ).fired_holds
    assert fired == ("quadraui#302",)


# ── holds_to_release ─────────────────────────────────────────────────────


def test_only_a_verified_roll_releases_the_deploy_gates():
    """Releasing a gate on an unverified roll restarts the overnight queue
    into the exact 'merged is not live' trap the gate exists to prevent."""
    q = rp.Quiescence(quiescent=True, fired_holds=("r#1",))
    assert rp.holds_to_release(q, verified=True) == ("r#1",)
    assert rp.holds_to_release(q, verified=False) == ()


# ── plan_lanes ───────────────────────────────────────────────────────────


def test_the_daemon_host_rolls_first():
    """The invariant: a caller must never reach an endpoint its daemon
    predates. Newer-daemon-than-caller is the skew the board protocol is
    built to tolerate; the reverse is a documented 405."""
    rolls = rp.plan_lanes(
        daemon_host="dellserver",
        hosts=["elitebook", "macmini", "dellserver"],
        lanes=[rp.LANE_PYTHON],
    )
    assert [r.host for r in rolls][0] == "dellserver"
    assert "405" in rolls[0].rationale


def test_every_python_lane_precedes_every_units_lane():
    """The units ship *inside* the wheel (coord/deploy/, #1927), so a host's
    unit lane can only roll after that host's venv swapped."""
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a", "b"])
    last_python = max(i for i, r in enumerate(rolls) if r.lane == rp.LANE_PYTHON)
    first_units = min(i for i, r in enumerate(rolls) if r.lane == rp.LANE_UNITS)
    assert last_python < first_units


def test_the_tui_lane_goes_last():
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a", "b"])
    assert rolls[-1].lane == rp.LANE_TUI


def test_the_order_field_is_dense_and_ascending():
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a", "b"])
    assert [r.order for r in rolls] == list(range(1, len(rolls) + 1))


def test_lane_filtering_narrows_without_reordering():
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a", "b"], lanes=[rp.LANE_UNITS])
    assert {r.lane for r in rolls} == {rp.LANE_UNITS}
    assert [r.host for r in rolls] == ["a", "b"]


def test_already_current_hosts_are_skipped_entirely():
    """A re-run after a partial failure resumes; it does not restart the
    hosts that already landed."""
    rolls = rp.plan_lanes(
        daemon_host="a", hosts=["a", "b"], lanes=[rp.LANE_PYTHON], skip_hosts=["a"]
    )
    assert [r.host for r in rolls] == ["b"]


def test_an_unknown_daemon_host_degrades_to_config_order():
    rolls = rp.plan_lanes(daemon_host=None, hosts=["a", "b"], lanes=[rp.LANE_PYTHON])
    assert [r.host for r in rolls] == ["a", "b"]


# ── version helpers ──────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("v0.4.111", "0.4.111"), ("0.4.111", "0.4.111"), ("", None), (None, None),
])
def test_normalize_version(raw, expected):
    assert rp.normalize_version(raw) == expected


def test_hosts_already_current_requires_every_lane_to_agree():
    current = rp.hosts_already_current(
        {"a": ["0.4.111", "0.4.111"], "b": ["0.4.111", "0.4.110"]}, "0.4.111"
    )
    assert current == ["a"]


def test_a_host_with_an_unreadable_lane_is_never_current():
    """#1834's rule: version=None means 'no data', which is emphatically not
    'agrees with everyone else'. Skipping such a host would let the lane
    nobody can see be the one that stays behind."""
    assert rp.hosts_already_current({"a": ["0.4.111", None]}, "0.4.111") == []


def test_a_host_with_no_lanes_at_all_is_never_current():
    assert rp.hosts_already_current({"a": []}, "0.4.111") == []


def test_no_target_means_nothing_is_current():
    assert rp.hosts_already_current({"a": ["0.4.111"]}, None) == []


# ── the journal ──────────────────────────────────────────────────────────


def test_a_record_round_trips(tmp_path):
    record = rp.PropagationRecord(
        started_at=1.0, target_version="0.4.111", status=rp.STATUS_VERIFIED
    )
    rp.append_record(tmp_path, record)
    records = rp.read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["target_version"] == "0.4.111"
    assert records[0]["status"] == rp.STATUS_VERIFIED


def test_records_append_in_order(tmp_path):
    for i in range(3):
        rp.append_record(tmp_path, rp.PropagationRecord(started_at=float(i)))
    assert [r["started_at"] for r in rp.read_records(tmp_path)] == [0.0, 1.0, 2.0]


def test_a_torn_final_line_does_not_destroy_the_history(tmp_path):
    """The history is most valuable in exactly the case where the process
    died mid-append."""
    rp.append_record(tmp_path, rp.PropagationRecord(started_at=1.0))
    with rp.journal_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write('{"started_at": 2.0, "stat')
    records = rp.read_records(tmp_path)
    assert len(records) == 1


def test_reading_a_journal_that_does_not_exist_yet_is_empty(tmp_path):
    assert rp.read_records(tmp_path) == []


def test_limit_returns_the_most_recent(tmp_path):
    for i in range(5):
        rp.append_record(tmp_path, rp.PropagationRecord(started_at=float(i)))
    assert [r["started_at"] for r in rp.read_records(tmp_path, limit=2)] == [3.0, 4.0]


def test_trim_bounds_the_journal(tmp_path):
    for i in range(10):
        rp.append_record(tmp_path, rp.PropagationRecord(started_at=float(i)))
    assert rp.trim_journal(tmp_path, keep=4) == 4
    records = rp.read_records(tmp_path)
    assert [r["started_at"] for r in records] == [6.0, 7.0, 8.0, 9.0]


def test_the_journal_is_valid_jsonl(tmp_path):
    rp.append_record(
        tmp_path,
        rp.PropagationRecord(started_at=1.0, lanes=[{"lane": "python", "host": "a",
                                                     "ok": True, "detail": "x"}]),
    )
    text = rp.journal_path(tmp_path).read_text(encoding="utf-8")
    assert text.endswith("\n")
    for line in text.splitlines():
        json.loads(line)


# ── rendering ────────────────────────────────────────────────────────────


def test_an_empty_history_says_the_timer_never_ran():
    """An empty history is itself the finding — the 2026-08-04 shape is a
    readout that cannot tell 'nothing to do' from 'nothing ran'."""
    out = rp.render_history([])
    assert "no propagation attempts recorded" in out
    assert "timer" in out


def test_no_op_runs_collapse_but_are_counted():
    records = [
        {"started_at": float(i), "status": rp.STATUS_DEFERRED,
         "quiescence": {"reason": "busy"}}
        for i in range(20)
    ]
    out = rp.render_history(records)
    assert "20 no-op attempt(s)" in out
    assert len(out.splitlines()) == 1


def test_verbose_shows_every_attempt():
    records = [
        {"started_at": float(i), "status": rp.STATUS_DEFERRED,
         "quiescence": {"reason": "busy"}}
        for i in range(3)
    ]
    assert len(rp.render_history(records, verbose=True).splitlines()) > 3


def test_a_real_roll_is_never_collapsed():
    records = [
        {"started_at": 1.0, "status": rp.STATUS_DEFERRED, "quiescence": {"reason": "busy"}},
        {"started_at": 2.0, "status": rp.STATUS_VERIFIED, "target_version": "0.4.111",
         "lanes": [{"lane": "python", "host": "dellserver", "ok": True, "detail": "now v0.4.111"}],
         "verification": {"severity": "ok", "findings": []}},
    ]
    out = rp.render_history(records)
    assert "0.4.111" in out
    assert "python@dellserver" in out
    assert "verify: ok" in out


def test_a_rollback_is_rendered():
    record = rp.PropagationRecord(
        started_at=1.0, target_version="0.4.111", status=rp.STATUS_ROLLED_BACK,
        rolled_back=["dellserver: rolling back"],
        verification={"severity": "crit",
                      "findings": [{"severity": "crit", "host": "dellserver",
                                    "lane": "~/.coord-venv", "summary": "skew"}]},
    )
    out = "\n".join(rp.render_record(record))
    assert "rolled back" in out
    assert "crit" in out


def test_a_dry_run_is_labelled_as_one():
    record = rp.PropagationRecord(started_at=1.0, target_version="0.4.111",
                                  dry_run=True, status=rp.STATUS_ROLLED)
    assert "[dry-run]" in "\n".join(rp.render_record(record))
