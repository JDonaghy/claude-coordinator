"""#1632 acceptance: the whole tick, black box, through a fake transport.

Seed a board where an assignment exceeds its stratified baseline, assert
**exactly one** notification with the expected text, advance the clock and
assert **no second** one — plus the quiet-hours, urgent-drive and cold-start
criteria, all driven through :func:`coord.notifier.service.tick` rather
than through the pure predicate, so the ledger, the store and the transport
seam are exercised together.
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest

from coord.config import NotificationsConfig
from coord.models import QuietHours
from coord.notifier import service, store
from coord.notifier.baseline import build_baselines
from coord.notifier.predicate import HaltedDrive, PipelineSnapshot, WorkerProbe
from coord.notifier.transport import MemoryTransport

TZ = "America/Chicago"
HOUR = 3600.0


def at(hour: int, minute: int = 0, day: int = 14) -> float:
    return datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo(TZ)).timestamp()


class FakeConfig:
    """Just enough of a ``Config`` for the notifier: it reads one attribute."""

    def __init__(self, **kw):
        kw.setdefault("enabled", True)
        kw.setdefault("web_base_url", "http://dellserver:7434")
        self.notifications = NotificationsConfig(**kw)
        self.machines = []


def history(secs: float = 600.0, n: int = 10, repo="coord", type_="work"):
    """A completed-leg population big enough to learn a baseline from."""
    return [
        {
            "repo_name": repo,
            "type": type_,
            "issue_number": 1,
            "status": "done",
            "dispatched_at": 0.0,
            "finished_at": secs,
        }
        for _ in range(n)
    ]


def slow_snapshot(now: float, *, elapsed: float = 10 * HOUR, urgent: bool = False):
    return PipelineSnapshot(
        now=now,
        web_base_url="http://dellserver:7434",
        probes=[
            WorkerProbe(
                assignment_id="a1",
                repo="coord",
                issue=42,
                type="work",
                machine="dellserver",
                dispatched_at=now - elapsed,
                urgent=urgent,
            )
        ],
    )


def run(config, *, now, snapshot, transport, baselines=None, rows=None):
    return service.tick(
        config,
        now=now,
        transport=transport,
        snapshot=snapshot,
        baselines=baselines if baselines is not None else build_baselines(rows or history()),
    )


# ── the headline acceptance case ─────────────────────────────────────────


def test_exactly_one_notification_and_none_on_the_next_tick():
    transport = MemoryTransport()
    config = FakeConfig()
    t0 = at(15, 0)

    first = run(config, now=t0, snapshot=slow_snapshot(t0), transport=transport)
    assert len(first.delivered) == 1
    assert len(transport.sent) == 1

    message = transport.sent[0]
    assert "coord#42" in message.title
    assert "running far longer than comparable work" in message.title
    assert "p90 of 10 comparable" in message.body
    assert message.click_url == "http://dellserver:7434/pipeline/coord/42"

    # Advance the clock. The job is still slow — and must NOT re-notify.
    for offset in (HOUR, 2 * HOUR, 6 * HOUR):
        again = run(
            config,
            now=t0 + offset,
            snapshot=slow_snapshot(t0 + offset, elapsed=10 * HOUR + offset),
            transport=transport,
        )
        assert again.delivered == []
    assert len(transport.sent) == 1, "a genuinely slow job must notify once, not per tick"


def test_a_state_change_to_stopped_escalates_exactly_once():
    """Re-notify only on a state change (running-slow -> stopped), and carry
    the earlier notice's context so it reads as an escalation."""
    transport = MemoryTransport()
    config = FakeConfig()
    t0 = at(15, 0)
    run(config, now=t0, snapshot=slow_snapshot(t0), transport=transport)

    stuck = PipelineSnapshot(
        now=t0 + HOUR,
        probes=[
            WorkerProbe(
                assignment_id="a1", repo="coord", issue=42, machine="dellserver",
                dispatched_at=t0 - 10 * HOUR, stuck_message="out of ideas",
            )
        ],
    )
    escalated = run(config, now=t0 + HOUR, snapshot=stuck, transport=transport)
    assert len(escalated.delivered) == 1
    assert transport.sent[-1].title.startswith("ESCALATION:")
    assert "previously notified" in transport.sent[-1].body

    # ...and the escalation itself does not repeat either.
    third = run(config, now=t0 + 2 * HOUR, snapshot=stuck, transport=transport)
    assert third.delivered == []
    assert len(transport.sent) == 2


# ── cold start ────────────────────────────────────────────────────────────


def test_cold_stratum_produces_no_baseline_derived_notification():
    """Fewer than N samples in a stratum -> the p90 path must not fire.
    (The generous absolute ceiling still exists; this job is nowhere near
    it.)"""
    transport = MemoryTransport()
    result = run(
        FakeConfig(),
        now=at(15, 0),
        snapshot=slow_snapshot(at(15, 0), elapsed=2 * HOUR),
        transport=transport,
        rows=history(n=4),
    )
    assert result.delivered == []
    assert transport.sent == []


def test_cold_stratum_still_catches_a_catastrophically_wedged_leg():
    transport = MemoryTransport()
    result = run(
        FakeConfig(),
        now=at(15, 0),
        snapshot=slow_snapshot(at(15, 0), elapsed=20 * HOUR),
        transport=transport,
        rows=history(n=4),
    )
    assert len(result.delivered) == 1
    assert "no baseline yet" in transport.sent[0].body


# ── quiet hours, end to end ───────────────────────────────────────────────


def quiet_config(**kw):
    return FakeConfig(
        quiet_hours=QuietHours(start=dtime(22, 0), end=dtime(8, 0), tz=TZ), **kw
    )


def test_quiet_hours_hold_then_one_digest_at_0800():
    transport = MemoryTransport()
    config = quiet_config()

    night = at(23, 0)
    held_1 = run(config, now=night, snapshot=slow_snapshot(night), transport=transport)
    assert held_1.delivered == []
    assert len(held_1.deferred) == 1
    assert transport.sent == []

    small_hours = at(2, 0, day=15)
    halted = PipelineSnapshot(
        now=small_hours, halted=[HaltedDrive(repo="coord", issue=7, reason="FOREIGN")]
    )
    held_2 = run(config, now=small_hours, snapshot=halted, transport=transport)
    assert held_2.delivered == []
    assert transport.sent == [], "not even a halted drive pierces the window"

    morning = at(8, 0, day=15)
    flushed = run(
        config,
        now=morning,
        snapshot=PipelineSnapshot(now=morning),
        transport=transport,
    )
    assert flushed.digest is not None
    assert flushed.digest.detail["count"] == 2
    assert len(transport.sent) == 1, "one digest, not one message per held event"
    assert len(transport.sent[0].body.splitlines()) == 2


def test_a_persisting_condition_through_an_open_quiet_window_holds_once():
    """A held event must be ledgered at hold time, not only at delivery
    time (#1632 fix iteration 1).

    Without that, `select_deliverable`'s "fire once per subject/condition"
    dedupe — which only consults the ledger — treats the same persisting
    condition (a halted drive, a parked gate, a stalled worker: exactly the
    long-lived cases this feature targets) as fresh on every tick, and
    `state.deferred` fills with duplicates of the one subject/condition for
    as long as quiet hours and the condition both last, eventually evicting
    older, genuinely distinct events once MAX_DEFERRED is reached.
    """
    transport = MemoryTransport()
    config = quiet_config()

    # 18 ticks, 30 minutes apart, every one of them inside the 22:00-08:00
    # window, the same halted drive on every single one — the realistic
    # overnight-halt scenario, not a one-off hold.
    for step in range(18):
        moment = at(23, 0) + step * 1800.0
        snapshot = PipelineSnapshot(
            now=moment,
            halted=[HaltedDrive(repo="coord", issue=7, reason="FOREIGN")],
        )
        result = run(config, now=moment, snapshot=snapshot, transport=transport)
        assert result.delivered == []
        assert transport.sent == []

    state = store.load_state()
    assert len(state.deferred) == 1, (
        "one persisting condition must be held once across the whole quiet "
        "window, not re-appended on every tick"
    )
    assert state.overflow == 0
    assert len(state.ledger) == 1

    morning = at(8, 0, day=15)
    flushed = run(
        config,
        now=morning,
        snapshot=PipelineSnapshot(now=morning),
        transport=transport,
    )
    assert flushed.digest is not None
    assert flushed.digest.detail["count"] == 1
    assert len(transport.sent) == 1, "no duplicate immediate send alongside the digest"


def test_an_urgent_drive_delivers_at_2300():
    transport = MemoryTransport()
    night = at(23, 0)
    result = run(
        quiet_config(),
        now=night,
        snapshot=slow_snapshot(night, urgent=True),
        transport=transport,
    )
    assert len(result.delivered) == 1
    assert len(transport.sent) == 1
    assert result.deferred == []


def test_urgency_comes_from_the_store_and_expires():
    """`coord drive --urgent` writes the opt-out; it is scoped to one issue
    and carries its own expiry so a forgotten flag cannot make every future
    night loud."""
    now = at(23, 0)
    store.mark_urgent("coord", 42, expires_at=now + HOUR)
    state = store.load_state()
    assert store.urgent_keys(state, now=now) == {"coord#42"}
    assert store.urgent_keys(state, now=now + 2 * HOUR) == set()

    store.clear_urgent("coord", 42)
    assert store.urgent_keys(store.load_state(), now=now) == set()


# ── the master switch ─────────────────────────────────────────────────────


def test_disabled_notifier_does_nothing_at_all():
    transport = MemoryTransport()
    result = run(
        FakeConfig(enabled=False),
        now=at(15, 0),
        snapshot=slow_snapshot(at(15, 0)),
        transport=transport,
    )
    assert result.enabled is False
    assert transport.sent == []


def test_absent_notifications_block_is_disabled_by_default():
    assert NotificationsConfig().enabled is False


# ── persistence ───────────────────────────────────────────────────────────


def test_the_ledger_survives_a_restart():
    """A tick-local set would re-notify the same slow job on every daemon
    redeploy — the exact behaviour that trains an operator to mute."""
    config = FakeConfig()
    t0 = at(15, 0)
    first = MemoryTransport()
    run(config, now=t0, snapshot=slow_snapshot(t0), transport=first)
    assert len(first.sent) == 1

    # Fresh process: nothing in memory, everything read back off disk.
    reloaded = MemoryTransport()
    result = service.tick(
        config,
        now=t0 + HOUR,
        transport=reloaded,
        snapshot=slow_snapshot(t0 + HOUR, elapsed=11 * HOUR),
        baselines=build_baselines(history()),
        state=store.load_state(),
    )
    assert result.delivered == []
    assert reloaded.sent == []


def test_held_events_survive_a_restart():
    config = quiet_config()
    night = at(23, 0)
    run(config, now=night, snapshot=slow_snapshot(night), transport=MemoryTransport())
    assert len(store.load_state().deferred) == 1

    morning = at(8, 0, day=15)
    transport = MemoryTransport()
    result = service.tick(
        config,
        now=morning,
        transport=transport,
        snapshot=PipelineSnapshot(now=morning),
        baselines={},
        state=store.load_state(),
    )
    assert result.digest is not None
    assert len(transport.sent) == 1


@pytest.mark.parametrize("condition_secs", [10 * HOUR])
def test_tick_result_summary_is_human_readable(condition_secs):
    result = run(
        FakeConfig(),
        now=at(15, 0),
        snapshot=slow_snapshot(at(15, 0), elapsed=condition_secs),
        transport=MemoryTransport(),
    )
    assert "delivered" in result.summary()
