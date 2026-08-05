"""Tests for #1862: per-machine quiet hours.

Covers the three things the issue calls out as "actually hard":
  1. Timezone is required and honoured (a pinned non-UTC tz fires at the
     right UTC instant) — `test_quiet_hours_covers_pins_tz_to_utc_instant`.
  2. Wrapping (23:00 -> 08:00) and non-wrapping (13:00 -> 14:00) windows,
     with boundary tests at start, end, and +/-1 minute.
  3. `coord unpause` during a quiet window grants a real override rather
     than silently re-pausing on the next read.

Config-parse-time validation (missing/invalid tz, malformed HH:MM,
start == end) lives in tests/test_config.py alongside the rest of
`coord.config`'s machine-parsing tests.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from pathlib import Path

import pytest

from coord import machine_pause
from coord.models import Assignment, Board, Machine, QuietHours

# 23:00 -> 08:00 UTC: the wrapping shape from the issue's own example.
WRAPPING = QuietHours(start=time(23, 0), end=time(8, 0), tz="UTC")
# 13:00 -> 14:00 UTC: the non-wrapping shape.
NON_WRAPPING = QuietHours(start=time(13, 0), end=time(14, 0), tz="UTC")


def _machine(name: str, quiet_hours: QuietHours | None = None) -> Machine:
    return Machine(name=name, host=f"{name}.tail", repos=["api"], quiet_hours=quiet_hours)


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir()
    return tmp_path


# ── QuietHours.covers() — boundary tests ────────────────────────────────────


def test_covers_wrapping_window_at_start() -> None:
    assert WRAPPING.covers(datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc)) is True


def test_covers_wrapping_window_one_minute_before_start() -> None:
    assert WRAPPING.covers(datetime(2026, 1, 15, 22, 59, tzinfo=timezone.utc)) is False


def test_covers_wrapping_window_at_end() -> None:
    """The machine wakes up exactly at `end`, not a minute after."""
    assert WRAPPING.covers(datetime(2026, 1, 16, 8, 0, tzinfo=timezone.utc)) is False


def test_covers_wrapping_window_one_minute_before_end() -> None:
    assert WRAPPING.covers(datetime(2026, 1, 16, 7, 59, tzinfo=timezone.utc)) is True


def test_covers_wrapping_window_through_midnight() -> None:
    assert WRAPPING.covers(datetime(2026, 1, 16, 0, 0, tzinfo=timezone.utc)) is True


def test_covers_wrapping_window_outside() -> None:
    assert WRAPPING.covers(datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)) is False


def test_covers_non_wrapping_window_at_start() -> None:
    assert NON_WRAPPING.covers(datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)) is True


def test_covers_non_wrapping_window_one_minute_before_start() -> None:
    assert NON_WRAPPING.covers(datetime(2026, 1, 15, 12, 59, tzinfo=timezone.utc)) is False


def test_covers_non_wrapping_window_at_end() -> None:
    assert NON_WRAPPING.covers(datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)) is False


def test_covers_non_wrapping_window_one_minute_before_end() -> None:
    assert NON_WRAPPING.covers(datetime(2026, 1, 15, 13, 59, tzinfo=timezone.utc)) is True


def test_covers_non_wrapping_window_outside() -> None:
    assert NON_WRAPPING.covers(datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)) is False


def test_covers_naive_datetime_treated_as_utc() -> None:
    """A caller that forgets `tzinfo` must not silently misbehave — treat it
    as UTC (matching `datetime.now(timezone.utc)`'s own convention) rather
    than raising or comparing against a wall-clock-dependent local zone."""
    assert WRAPPING.covers(datetime(2026, 1, 15, 23, 30)) is True


# ── tz is honoured — pinned to a known non-UTC UTC-instant (#1862 hard case) ─


def test_covers_pins_non_utc_tz_to_the_right_utc_instant() -> None:
    """America/Chicago is UTC-6 in mid-January (CST, no DST). A window of
    local 23:00 -> 08:00 Chicago must cover local 23:00 Jan 15, which is
    05:00 UTC Jan 16 — NOT 23:00 UTC Jan 15 (what a naive/UTC-defaulting
    implementation would wrongly use, firing 6 hours early)."""
    qh = QuietHours(start=time(23, 0), end=time(8, 0), tz="America/Chicago")

    # Exactly local 23:00 Chicago == 05:00 UTC the next day.
    assert qh.covers(datetime(2026, 1, 16, 5, 0, tzinfo=timezone.utc)) is True
    # One minute earlier in UTC == one minute before local start: not covered.
    assert qh.covers(datetime(2026, 1, 16, 4, 59, tzinfo=timezone.utc)) is False
    # The literal failure mode this field prevents: 23:00 UTC on Jan 15 is
    # only 17:00 Chicago time — well outside the window.
    assert qh.covers(datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc)) is False

    # Exactly local 08:00 Chicago == 14:00 UTC: window just ended.
    assert qh.covers(datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)) is False
    assert qh.covers(datetime(2026, 1, 16, 13, 59, tzinfo=timezone.utc)) is True


# ── QuietHours.window_end_instant() ─────────────────────────────────────────


def test_window_end_instant_non_wrapping_is_same_day() -> None:
    now = datetime(2026, 1, 15, 13, 30, tzinfo=timezone.utc)
    assert NON_WRAPPING.window_end_instant(now) == datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)


def test_window_end_instant_wrapping_evening_half_is_tomorrow() -> None:
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    assert WRAPPING.window_end_instant(now) == datetime(2026, 1, 16, 8, 0, tzinfo=timezone.utc)


def test_window_end_instant_wrapping_morning_half_is_today() -> None:
    now = datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc)
    assert WRAPPING.window_end_instant(now) == datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)


# ── coord.machine_pause: paused_set()/local_paused_set() folds in quiet hours


def test_local_paused_set_includes_quiet_covered_machine(tmp_home: Path) -> None:
    machines = [_machine("elitebook", WRAPPING), _machine("server")]
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    assert machine_pause.local_paused_set(machines, now=now) == {"elitebook"}


def test_local_paused_set_excludes_machine_outside_its_window(tmp_home: Path) -> None:
    machines = [_machine("elitebook", WRAPPING)]
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert machine_pause.local_paused_set(machines, now=now) == set()


def test_local_paused_set_with_no_machines_arg_is_explicit_only(tmp_home: Path) -> None:
    """The pre-#1862 contract: omit `machines` and quiet hours never apply —
    every caller not yet threading `config.machines` through stays
    byte-identical to before this feature."""
    machine_pause.local_pause("m1")
    assert machine_pause.local_paused_set() == {"m1"}


def test_machine_with_no_quiet_hours_block_is_unaffected(tmp_home: Path) -> None:
    machines = [_machine("plain")]
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    assert machine_pause.local_paused_set(machines, now=now) == set()


def test_paused_set_routes_machines_through_to_local_computation(tmp_home: Path) -> None:
    """`paused_set()` (the public, board-service-aware entry point every
    real caller uses) must fold in quiet hours too, not just the
    `local_paused_set()` primitive underneath it."""
    machines = [_machine("elitebook", WRAPPING)]
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    assert machine_pause.paused_set(machines, now=now) == {"elitebook"}


# ── "coord unpause must not become a lie" (#1862 acceptance criterion) ─────


def test_unpause_during_quiet_window_grants_an_override(tmp_home: Path) -> None:
    machines = [_machine("elitebook", WRAPPING)]
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)

    outcome = machine_pause.local_unpause_effective("elitebook", machines, now=now)
    assert outcome.changed is True
    assert outcome.kind == "quiet_override"
    assert outcome.quiet_until == "08:00"
    assert outcome.tz == "UTC"

    # Says what it did AND actually did it: the very next read, same
    # window, no longer shows it paused (the #1563 failure class: success
    # reported, nothing changed).
    assert "elitebook" not in machine_pause.local_paused_set(machines, now=now)


def test_unpause_override_lasts_only_until_the_window_it_was_granted_in(tmp_home: Path) -> None:
    machines = [_machine("elitebook", WRAPPING)]
    granted_at = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    machine_pause.local_unpause_effective("elitebook", machines, now=granted_at)

    # One minute before the window naturally ends: still overridden.
    almost_end = datetime(2026, 1, 16, 7, 59, tzinfo=timezone.utc)
    assert "elitebook" not in machine_pause.local_paused_set(machines, now=almost_end)

    # The NEXT night's window is a fresh window — the override must not
    # outlive the one it was granted for, or `coord unpause` would have
    # silently disabled quiet hours forever instead of "for the rest of
    # tonight".
    next_night = datetime(2026, 1, 16, 23, 30, tzinfo=timezone.utc)
    assert "elitebook" in machine_pause.local_paused_set(machines, now=next_night)


def test_unpause_outside_any_quiet_window_is_a_true_no_op(tmp_home: Path) -> None:
    machines = [_machine("elitebook", WRAPPING)]
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)  # well outside the window

    outcome = machine_pause.local_unpause_effective("elitebook", machines, now=now)
    assert outcome.changed is False
    assert outcome.kind == "not_paused"


def test_unpause_prefers_explicit_resume_over_quiet_override(tmp_home: Path) -> None:
    """A machine that is BOTH hand-paused and inside its quiet window: lift
    the hand pause (the more specific, more recent operator action) rather
    than manufacturing a quiet-hours override on top of it."""
    machine_pause.local_pause("elitebook")
    machines = [_machine("elitebook", WRAPPING)]
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)

    outcome = machine_pause.local_unpause_effective("elitebook", machines, now=now)
    assert outcome.changed is True
    assert outcome.kind == "resumed"


def test_unpause_of_unrelated_machine_ignores_others_quiet_hours(tmp_home: Path) -> None:
    machines = [_machine("elitebook", WRAPPING), _machine("server")]
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    outcome = machine_pause.local_unpause_effective("server", machines, now=now)
    assert outcome.changed is False
    assert outcome.kind == "not_paused"


# ── Pause-state display distinguishes quiet-paused from hand-paused ─────────


def test_describe_pause_state_hand_pause() -> None:
    m = _machine("laptop")
    state = machine_pause.describe_pause_state(m, {"laptop"})
    assert state is not None
    assert state.kind == "hand"


def test_describe_pause_state_quiet() -> None:
    m = _machine("elitebook", WRAPPING)
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    state = machine_pause.describe_pause_state(m, {"elitebook"}, now=now)
    assert state is not None
    assert state.kind == "quiet"
    assert "08:00" in state.detail


def test_describe_pause_state_quiet_overridden() -> None:
    """Covered by its window, but NOT in the effective paused set (the
    override suppressed it) — a distinct, third state from either a plain
    hand pause or an active quiet pause."""
    m = _machine("elitebook", WRAPPING)
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)
    state = machine_pause.describe_pause_state(m, set(), now=now)
    assert state is not None
    assert state.kind == "quiet_overridden"


def test_describe_pause_state_none_when_normal() -> None:
    m = _machine("elitebook", WRAPPING)
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert machine_pause.describe_pause_state(m, set(), now=now) is None


# ── In-flight assignments are untouched when a window opens ────────────────


def test_quiet_hours_never_touches_in_flight_assignments(tmp_home: Path) -> None:
    """#1862 acceptance: quiet hours governs ROUTING only — a task still
    running when the window opens finishes normally. The pause computation
    takes only `machines`/`now`; it has no path to `Board`/`Assignment` at
    all, so a running assignment on a now-quiet machine is provably
    untouched by it."""
    board = Board()
    running = Assignment(
        machine_name="elitebook", repo_name="api", issue_number=1,
        issue_title="in flight", status="running",
    )
    board.active.append(running)

    machines = [_machine("elitebook", WRAPPING)]
    now = datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)  # window just opened

    paused = machine_pause.local_paused_set(machines, now=now)
    assert "elitebook" in paused  # excluded from NEW dispatch...

    # ...but the in-flight assignment is the exact same object, unmodified.
    assert board.active == [running]
    assert running.status == "running"
