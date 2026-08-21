"""A host stuck behind during a #2240 deadlock-release cooldown has no
automatic path back to rolling — and, until this fix, no signal anywhere
either (#2490).

Incident, 2026-08-20: `precision` needed v0.5.192, kept cycling
busy→fail→idle→busy on the drive queue (the exact churn pattern that trips
`max_deferrals`), self-released its cordon, and sat on the broken version —
uncordoned, receiving normal dispatch, failing every attempt, genuinely idle
at several points — for the length of the 30-minute cooldown. Nothing
surfaced it: `coord status` showed a flatly ordinary `online • idle`, and
`coord release propagate` refused to even attempt cordoning it ("nothing was
cordoned this run"). An operator noticed only because a human happened to be
watching `coord status`.

Every test here fails against the pre-fix code: `CordonPlan` had no
`stuck_in_cooldown` field, `CordonOutcome` did not journal one, and neither
`coord release cordon` nor `coord status` read anything back out of the
journal for this case — a behind, idle host during an active cooldown
produced no cordon and no signal at all, anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import release_cordon as rc
from coord import release_propagate as rp
from coord.cli import main
from coord.commands import release as release_cmd


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir()
    return tmp_path


def _stub_state_dir(monkeypatch, tmp_path):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(release_cmd, "_state_dir", lambda: d)
    return d


# ══════════════════════════════════════════════════════════════════════════
# `plan_cordons`: the pure acceptance criterion the issue names directly —
# "construct a DeferralPressure that has just triggered a deadlock release
# (cooldown active), a host that is behind the target version AND idle, and
# assert that something ... flags it."
# ══════════════════════════════════════════════════════════════════════════


def test_a_behind_idle_host_is_flagged_during_the_cooldown() -> None:
    plan = rc.plan_cordons(
        target_version="0.5.192",
        host_versions={"precision": "0.5.191"},
        existing={},
        now=1200.0,
        pressure=rc.DeferralPressure(consecutive=0, last_release_at=1000.0),
        busy_reasons={},
    )
    assert plan.cooling_seconds > 0, "the cooldown must still be active for this test"
    assert plan.cordon == (), "the cooldown must still be suppressing new cordons"
    assert plan.stuck_in_cooldown == ("precision",)
    assert any("STUCK" in line and "precision" in line for line in plan.render())


def test_a_busy_behind_host_is_not_flagged_stuck() -> None:
    """A host that IS busy is not sitting idle for lack of a cordon — it has
    its own work in flight, which is the ordinary "cordon deferred" case,
    not the #2490 gap."""
    plan = rc.plan_cordons(
        target_version="0.5.192",
        host_versions={"precision": "0.5.191"},
        existing={},
        now=1200.0,
        pressure=rc.DeferralPressure(consecutive=0, last_release_at=1000.0),
        busy_reasons={"precision": "dispatched: issue #2481"},
    )
    assert plan.stuck_in_cooldown == ()


def test_a_current_host_is_never_flagged_stuck() -> None:
    plan = rc.plan_cordons(
        target_version="0.5.192",
        host_versions={"precision": "0.5.192"},
        existing={},
        now=1200.0,
        pressure=rc.DeferralPressure(consecutive=0, last_release_at=1000.0),
        busy_reasons={},
    )
    assert plan.stuck_in_cooldown == ()


def test_a_collateral_host_is_not_double_reported(monkeypatch=None) -> None:
    """A host spared because it's blocked behind a busy daemon host already
    has its own name for the reason (`collateral_spared`/`blocked_behind`) —
    it must not also show up as `stuck_in_cooldown`, which would give an
    operator two different explanations for the same machine."""
    plan = rc.plan_cordons(
        target_version="0.5.192",
        host_versions={"daemon": "0.5.191", "elitebook": "0.5.191"},
        existing={},
        now=1200.0,
        pressure=rc.DeferralPressure(consecutive=0, last_release_at=1000.0),
        busy_reasons={"daemon": "dispatched: issue #1"},
        daemon_host="daemon",
    )
    assert plan.collateral_spared == ("elitebook",)
    assert "elitebook" not in plan.stuck_in_cooldown
    # the daemon host itself IS busy, so it is not idle either
    assert "daemon" not in plan.stuck_in_cooldown


def test_outside_a_cooldown_nothing_is_flagged_stuck() -> None:
    """Ordinary drain: the host gets cordoned outright, so there is nothing
    to flag — `stuck_in_cooldown` is specifically the cooldown-suppressed
    case."""
    plan = rc.plan_cordons(
        target_version="0.5.192",
        host_versions={"precision": "0.5.191"},
        existing={},
        now=100.0,
        pressure=rc.DeferralPressure(),
        busy_reasons={},
    )
    assert plan.stuck_in_cooldown == ()
    assert [c.machine for c in plan.cordon] == ["precision"]


def test_to_dict_and_outcome_carry_the_field() -> None:
    plan = rc.plan_cordons(
        target_version="0.5.192",
        host_versions={"precision": "0.5.191"},
        existing={},
        now=1200.0,
        pressure=rc.DeferralPressure(consecutive=0, last_release_at=1000.0),
        busy_reasons={},
    )
    assert plan.to_dict()["stuck_in_cooldown"] == ["precision"]
    assert not plan.empty, "a stuck host is actionable — never silently 'empty'"

    outcome = rc.CordonOutcome()
    outcome.stuck_in_cooldown = list(plan.stuck_in_cooldown)
    assert outcome.to_dict()["stuck_in_cooldown"] == ["precision"]


# ══════════════════════════════════════════════════════════════════════════
# The journal round trip: `_apply_cordons` writes it, `_stuck_hosts_from_
# journal` reads it back — the same shape `coord status` and `coord release
# cordon` both consume.
# ══════════════════════════════════════════════════════════════════════════


def test_stuck_hosts_from_journal_reads_the_newest_record(
    tmp_home, monkeypatch, tmp_path
) -> None:
    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    rp.append_record(
        state_dir,
        rp.PropagationRecord(
            started_at=1.0, target_version="0.5.192",
            status=rp.STATUS_DEFERRED,
            cordons={"stuck_in_cooldown": ["precision"]},
        ),
    )
    hosts, target = release_cmd._stuck_hosts_from_journal()
    assert hosts == ["precision"]
    assert target == "0.5.192"


def test_stuck_hosts_from_journal_only_trusts_the_newest_record(
    tmp_home, monkeypatch, tmp_path
) -> None:
    """A host named in an OLDER record may since have gone busy, rolled, or
    had its cooldown lift — reporting it would be a false alarm."""
    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    rp.append_record(
        state_dir,
        rp.PropagationRecord(
            started_at=1.0, target_version="0.5.192",
            status=rp.STATUS_DEFERRED,
            cordons={"stuck_in_cooldown": ["precision"]},
        ),
    )
    rp.append_record(
        state_dir,
        rp.PropagationRecord(
            started_at=2.0, target_version="0.5.192",
            status=rp.STATUS_DEFERRED,
            cordons={"stuck_in_cooldown": []},
        ),
    )
    hosts, _target = release_cmd._stuck_hosts_from_journal()
    assert hosts == []


def test_no_journal_reads_as_nothing_stuck(tmp_home, monkeypatch, tmp_path) -> None:
    _stub_state_dir(monkeypatch, tmp_path)
    assert release_cmd._stuck_hosts_from_journal() == ([], None)


# ══════════════════════════════════════════════════════════════════════════
# `coord release cordon`: the surface an operator lands on once they've
# noticed the fleet is quiet. Pre-fix, this printed "no machines are
# cordoned — the fleet is free to take work" for exactly the incident above
# — technically true and completely misleading.
# ══════════════════════════════════════════════════════════════════════════


def test_the_cordon_listing_flags_a_stuck_host_even_with_none_cordoned(
    tmp_home, monkeypatch, tmp_path
) -> None:
    _stub_state_dir(monkeypatch, tmp_path)
    rp.append_record(
        Path(tmp_path / "state"),
        rp.PropagationRecord(
            started_at=1.0, target_version="0.5.192",
            status=rp.STATUS_DEFERRED,
            cordons={"stuck_in_cooldown": ["precision"]},
        ),
    )
    listed = CliRunner().invoke(main, ["release", "cordon"])
    assert listed.exit_code == 0, listed.output
    assert "STUCK" in listed.output
    assert "precision" in listed.output
    assert "v0.5.192" in listed.output
    assert "#2490" in listed.output
    assert "coord agent update --machine precision" in listed.output


def test_the_cordon_listing_is_quiet_when_nothing_is_stuck(
    tmp_home, monkeypatch, tmp_path
) -> None:
    _stub_state_dir(monkeypatch, tmp_path)
    listed = CliRunner().invoke(main, ["release", "cordon"])
    assert listed.exit_code == 0, listed.output
    assert "STUCK" not in listed.output
    assert "no machines are cordoned" in listed.output


# ══════════════════════════════════════════════════════════════════════════
# `coord status`'s own reader — same journal, same helper underneath.
# ══════════════════════════════════════════════════════════════════════════


def test_status_surface_reads_the_stuck_hosts(tmp_home, monkeypatch, tmp_path) -> None:
    from coord.commands.status import _stuck_in_cooldown_hosts

    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    rp.append_record(
        state_dir,
        rp.PropagationRecord(
            started_at=1.0, target_version="0.5.192",
            status=rp.STATUS_DEFERRED,
            cordons={"stuck_in_cooldown": ["precision"]},
        ),
    )
    assert _stuck_in_cooldown_hosts() == {"precision": "0.5.192"}


def test_status_surface_degrades_to_empty_with_no_journal(
    tmp_home, monkeypatch, tmp_path
) -> None:
    from coord.commands.status import _stuck_in_cooldown_hosts

    _stub_state_dir(monkeypatch, tmp_path)
    assert _stuck_in_cooldown_hosts() == {}
