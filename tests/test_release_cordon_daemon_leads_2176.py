"""#2176: the release cordon must not take out hosts the daemon-leads
invariant already forbids from rolling.

`coord release propagate` cordons every host that is behind the target so it
drains (#2101). That is correct when a host's OWN draining is the thing being
waited on. It is not correct for a host that is idle, behind, and blocked
from rolling anyway because the daemon host itself is busy — the daemon-leads
invariant (`coord/release_propagate.py`'s module docstring) forbids ANY other
host's python lane from rolling ahead of a busy daemon host, so cordoning an
idle non-daemon host protects nothing: its drained state cannot be consumed
until the daemon host rolls first.

Evidence, 2026-08-13 00:18 UTC: dellserver (the daemon host) had a drive
running and was cordoned correctly. elitebook and precision had no busy
signal against them, were also cordoned, and stayed that way for 34+ minutes
— launch-blocked while unable to roll regardless of their own drain state,
because they could never roll ahead of dellserver.

Every test here fails against the pre-fix code, where `plan_cordons` had no
`daemon_host` parameter and cordoned every behind host unconditionally.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import machine_pause as mp
from coord import release_cordon as rc
from coord.cli import main
from coord.commands import release as release_cmd
from coord.drive_queue import STATE_RUNNING


# ══════════════════════════════════════════════════════════════════════════
# Pure `plan_cordons` tests — fast, and precise about which host is which.
# ══════════════════════════════════════════════════════════════════════════


def test_a_busy_behind_daemon_host_spares_idle_behind_hosts_from_cordon():
    """The exact incident shape: dellserver busy and behind, elitebook and
    precision idle and behind. Only dellserver may be cordoned."""
    plan = rc.plan_cordons(
        target_version="0.5.48",
        host_versions={
            "dellserver": "0.5.40", "elitebook": "0.5.40", "precision": "0.5.40",
        },
        now=0.0,
        busy_reasons={"dellserver": "drive-queue entry running: claude-coordinator#2164"},
        daemon_host="dellserver",
    )
    assert [c.machine for c in plan.cordon] == ["dellserver"]
    assert plan.collateral_spared == ("elitebook", "precision")
    assert plan.blocked_behind == "dellserver"


def test_a_quiescent_daemon_host_cordons_every_behind_host_as_before():
    """Once the daemon host is not itself busy, the existing drain guarantee
    is unchanged — every behind host is cordoned, same as pre-#2176."""
    plan = rc.plan_cordons(
        target_version="0.5.48",
        host_versions={
            "dellserver": "0.5.40", "elitebook": "0.5.40", "precision": "0.5.40",
        },
        now=0.0,
        busy_reasons={},
        daemon_host="dellserver",
    )
    assert sorted(c.machine for c in plan.cordon) == ["dellserver", "elitebook", "precision"]
    assert plan.collateral_spared == ()
    assert plan.blocked_behind is None


def test_a_daemon_host_already_on_target_cordons_every_behind_host():
    """A daemon host busy but already ON the target version is rollable (it
    needs no roll at all), so it does not block anyone — collateral sparing
    must not fire."""
    plan = rc.plan_cordons(
        target_version="0.5.48",
        host_versions={
            "dellserver": "0.5.48", "elitebook": "0.5.40", "precision": "0.5.40",
        },
        now=0.0,
        busy_reasons={"dellserver": "drive-queue entry running: claude-coordinator#2164"},
        daemon_host="dellserver",
    )
    assert sorted(c.machine for c in plan.cordon) == ["elitebook", "precision"]
    assert plan.collateral_spared == ()


def test_a_busy_behind_host_is_still_cordoned_regardless_of_daemon_state():
    """A host that is itself busy is never collateral — its own drain is
    still the thing being waited on, whatever the daemon host is doing."""
    plan = rc.plan_cordons(
        target_version="0.5.48",
        host_versions={
            "dellserver": "0.5.40", "elitebook": "0.5.40", "precision": "0.5.40",
        },
        now=0.0,
        busy_reasons={
            "dellserver": "drive-queue entry running: claude-coordinator#2164",
            "elitebook": "drive-queue entry running: acme/other#3",
        },
        daemon_host="dellserver",
    )
    assert sorted(c.machine for c in plan.cordon) == ["dellserver", "elitebook"]
    assert plan.collateral_spared == ("precision",)


def test_a_previously_cordoned_collateral_host_is_released_not_just_left():
    """A host cordoned by an EARLIER run, before the daemon host went busy,
    must be actively released — "spared" means dispatchable now, not merely
    "not renewed until it expires"."""
    existing = {
        "dellserver": rc.Cordon(machine="dellserver", expires_at=1e12),
        "elitebook": rc.Cordon(machine="elitebook", expires_at=1e12),
    }
    plan = rc.plan_cordons(
        target_version="0.5.48",
        host_versions={"dellserver": "0.5.40", "elitebook": "0.5.40"},
        existing=existing,
        now=0.0,
        busy_reasons={"dellserver": "drive-queue entry running: claude-coordinator#2164"},
        daemon_host="dellserver",
    )
    assert [c.machine for c in plan.cordon] == ["dellserver"]
    assert "elitebook" in plan.uncordon


def test_no_daemon_host_falls_back_to_cordoning_every_behind_host():
    """`daemon_host=None` (an unorderable fleet, or a caller that predates
    #2176) disables the exemption entirely — the safe default."""
    plan = rc.plan_cordons(
        target_version="0.5.48",
        host_versions={"dellserver": "0.5.40", "elitebook": "0.5.40"},
        now=0.0,
        busy_reasons={"dellserver": "drive-queue entry running: claude-coordinator#2164"},
        daemon_host=None,
    )
    assert sorted(c.machine for c in plan.cordon) == ["dellserver", "elitebook"]
    assert plan.collateral_spared == ()


# ══════════════════════════════════════════════════════════════════════════
# CLI black-box test — the acceptance criterion, end to end.
# ══════════════════════════════════════════════════════════════════════════

THREE_HOST_CONFIG = """\
repos:
  - name: api
    github: acme/api

machines:
  - name: dellserver
    host: dellserver.tailnet
    capabilities: [python]
    repos: [api]
  - name: elitebook
    host: elitebook.tailnet
    capabilities: [python]
    repos: [api]
  - name: precision
    host: precision.tailnet
    capabilities: [python]
    repos: [api]
"""


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the cordon store — it lives at $HOME/.coord/."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir()
    return tmp_path


@pytest.fixture()
def three_host_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(THREE_HOST_CONFIG)
    return p


def _stub_state_dir(monkeypatch, tmp_path):
    d = tmp_path / "propagation-state"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(release_cmd, "_state_dir", lambda: d)
    return d


def _stub_board(monkeypatch, *, drive_queue=()):
    monkeypatch.setattr(
        release_cmd,
        "_fetch_board",
        lambda: (
            {"drive_queue": list(drive_queue), "assignments": [], "issues": []},
            None,
        ),
    )


def _serve_health():
    """A ``/health`` body whose ``spawned_coord`` row names a live
    coord-serve — how the daemon host is DERIVED rather than guessed."""
    return {
        "version": "0.5.40",
        "health": {"schema": 1, "results": [
            {"check_id": "spawned_coord", "subject": "coord-serve",
             "severity": "ok",
             "values": {"unit": "coord-serve", "pid": 1, "version": "0.5.40"}},
        ]},
    }


def _stub_verify(monkeypatch, *, versions: dict[str, str], daemon: str):
    from coord import release_verify as rv

    lanes = [
        rv.Lane(host=host, lane="~/.coord-venv", version=version)
        for host, version in versions.items()
    ]
    machine_health = {daemon: _serve_health()}
    monkeypatch.setattr(
        rv, "gather", lambda *a, **k: (machine_health, {}, None, daemon)
    )
    monkeypatch.setattr(
        rv,
        "verify",
        lambda **kwargs: rv.VerifyReport(
            expected=kwargs.get("expected"), lanes=lanes, findings=[]
        ),
    )


def test_propagate_cordons_only_the_busy_daemon_host_and_spares_idle_behind_hosts(
    tmp_home, three_host_config_path, monkeypatch, tmp_path
):
    """#2176's exact regression, reproduced through the real CLI command: a
    drive is running on dellserver (the daemon host), which is also behind.
    elitebook and precision are behind too, but report nothing busy.

    Pre-fix, this cordoned all three and held elitebook/precision launch-
    blocked for the entire time dellserver stayed busy — capacity spent on
    hosts that could not have rolled regardless. Post-fix, only dellserver
    is cordoned; the other two stay dispatchable.
    """
    _stub_state_dir(monkeypatch, tmp_path)
    _stub_board(
        monkeypatch,
        drive_queue=[{"repo_name": "claude-coordinator", "issue_number": 2164,
                      "state": STATE_RUNNING, "machine": "dellserver",
                      "launch_host": "dellserver"}],
    )
    _stub_verify(
        monkeypatch,
        versions={"dellserver": "0.5.40", "elitebook": "0.5.40", "precision": "0.5.40"},
        daemon="dellserver",
    )

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(three_host_config_path),
         "--target", "0.5.48"],
    )
    assert result.exit_code == 0, result.output

    # This is the regression: pre-fix, all three of these lines were
    # "⊘ cordon <host>: draining for v0.5.48".
    assert "cordon dellserver" in result.output
    assert "cordon elitebook" not in result.output
    assert "cordon precision" not in result.output
    # Item 3 — the blast radius is named, not just absent.
    assert "elitebook, precision" in result.output
    assert "blocked behind daemon host dellserver" in result.output

    # And the cordon store itself agrees: elitebook/precision are actually
    # dispatchable, not merely un-mentioned in the printed plan.
    assert mp.cordoned_names() == {"dellserver"}


def test_once_the_daemon_host_is_idle_the_other_behind_hosts_cordon_as_today(
    tmp_home, three_host_config_path, monkeypatch, tmp_path
):
    """The existing drain guarantee, unchanged: with NOTHING busy anywhere,
    every behind host is cordoned exactly as it was before #2176.

    `--dry-run`, like the regression test above: it changes nothing in the
    cordon store, but the plan it prints is exactly what a real run would
    write, and using it here means this test does not need to stub the roll
    executors to keep a successful run from reaching real network I/O (a
    successful roll uncordons its own host immediately — see 4b below —
    which would otherwise make "was it cordoned at all" unobservable here).
    """
    _stub_state_dir(monkeypatch, tmp_path)
    _stub_board(monkeypatch, drive_queue=[])
    _stub_verify(
        monkeypatch,
        versions={"dellserver": "0.5.40", "elitebook": "0.5.40", "precision": "0.5.40"},
        daemon="dellserver",
    )

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(three_host_config_path),
         "--target", "0.5.48", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "cordon dellserver" in result.output
    assert "cordon elitebook" in result.output
    assert "cordon precision" in result.output
    assert "spared" not in result.output
