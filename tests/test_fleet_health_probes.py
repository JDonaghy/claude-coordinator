"""Unit tests for each fleet-scope health probe's own severity logic (#1630).

``tests/test_fleet_health_snapshot.py`` covers the *plumbing* — polling,
persisting, staleness, payload bounding, the advisory-only guard. This module
covers the other half: given a hand-seeded
:class:`~coord.health.models.FleetSnapshot`, does each probe classify severity
the way the milestone's acceptance table says it must?

Everything here drives the probes through ``run_all(ctx, scopes=("fleet",))``
rather than calling the functions directly, so a probe that silently drops out
of the registry (wrong scope, missing ``@check``, an id typo) fails these tests
instead of quietly never running on the daemon.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from coord.config import HealthConfig
from coord.health.fleet_snapshot import (
    FleetHealthRefresher,
    _default_tui_source_dir,
    _newest_rust_source_mtime,
)
from coord.health.models import FleetSnapshot, HealthContext, Severity
from coord.health.registry import run_all

NOW = 1_800_000_000.0


def _ctx(*, machines: dict | None = None, daemon_host: dict | None = None,
         fleet: bool = True, home: Path | None = None) -> HealthContext:
    ctx = HealthContext(
        thresholds=HealthConfig(),
        home=home or Path("/nonexistent-home"),
        coord_dir=(home or Path("/nonexistent-home")) / ".coord",
        now=NOW,
        allow_network=False,
    )
    if fleet:
        ctx.fleet = FleetSnapshot(
            machines=machines or {}, daemon_host=daemon_host or {}
        )
    return ctx


def _run(ctx: HealthContext) -> dict:
    """check_id -> CheckResult, for every fleet-scope probe in the registry."""
    # Import for the side effect of registering the fleet checks; the registry
    # is populated by module import, so a test module that never imports them
    # would see an empty fleet scope and pass vacuously.
    from coord.health import checks  # noqa: F401, PLC0415

    report = run_all(ctx, scopes=("fleet",))
    return {r.check_id: r for r in report.results}


def _agent(version: str | None, *, errored: bool = False) -> dict:
    """A machine entry shaped like the one the refresher builds."""
    result = {"check_id": "agent_venv", "values": {"version": version}}
    if errored:
        result["error"] = "pip show exploded"
    return {"state": "online", "checks": {"results": [result]}}


# ── every fleet probe is actually registered ─────────────────────────────────


def test_all_five_fleet_probes_run() -> None:
    results = _run(_ctx())
    assert set(results) == {
        "fleet_deploy_lanes",
        "fleet_tui_binary",
        "fleet_board_latency",
        "fleet_phantom_running",
        "fleet_toolchain_skew",
    }


def test_no_fleet_snapshot_means_unknown_never_ok() -> None:
    """`coord health` run by hand on an agent has no fleet view. Every fleet
    probe must read UNKNOWN there — an absent signal is not a passing one."""
    results = _run(_ctx(fleet=False))
    assert results, "fleet probes must still run and report, not be skipped"
    for check_id, r in results.items():
        assert r.severity is Severity.UNKNOWN, check_id
        assert "no fleet snapshot" in r.headroom, check_id


# ── fleet_deploy_lanes ───────────────────────────────────────────────────────


def test_deploy_lanes_all_agree_is_ok() -> None:
    r = _run(
        _ctx(
            machines={"elitebook": _agent("1.4.0"), "mini": _agent("1.4.0")},
            daemon_host={"coord_serve_version": "1.4.0", "cli_venv_version": "1.4.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.OK
    assert "1.4.0" in r.headroom
    assert set(r.values["lanes"]) == {
        "elitebook", "mini", "coord-serve (daemon host)", "~/.coord-cli-venv",
    }


def test_deploy_lanes_any_disagreement_is_crit() -> None:
    """One lane behind is the 2026-07-29 incident: the CLI venv three releases
    stale while everyone believed the fix was live."""
    r = _run(
        _ctx(
            machines={"elitebook": _agent("1.4.0"), "mini": _agent("1.4.0")},
            daemon_host={"coord_serve_version": "1.4.0", "cli_venv_version": "1.1.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.CRIT
    assert "2 versions" in r.headroom
    # The detail must name *which* lane is the odd one out, not just that skew
    # exists — "something is stale" is not an actionable page.
    assert "~/.coord-cli-venv" in r.detail
    assert "1.1.0" in r.detail


def test_deploy_lanes_agent_skew_is_crit_too() -> None:
    r = _run(
        _ctx(
            machines={"elitebook": _agent("1.4.0"), "mini": _agent("1.3.9")},
            daemon_host={"coord_serve_version": "1.4.0", "cli_venv_version": "1.4.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.CRIT
    assert "mini" in r.detail


def test_deploy_lanes_missing_lane_downgrades_agreement_to_unknown() -> None:
    """Agreement among the lanes that *did* answer is not fleet-wide agreement:
    a machine with no data must not read as "matches everyone else"."""
    r = _run(
        _ctx(
            machines={"elitebook": _agent("1.4.0"), "mini": _agent(None)},
            daemon_host={"coord_serve_version": "1.4.0", "cli_venv_version": "1.4.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.UNKNOWN
    assert "mini" in r.detail
    assert "1 lane(s) with no data" in r.headroom


def test_deploy_lanes_errored_agent_check_is_no_data_not_a_version() -> None:
    r = _run(
        _ctx(
            machines={"elitebook": _agent("1.4.0", errored=True)},
            daemon_host={"coord_serve_version": "1.4.0", "cli_venv_version": "1.4.0"},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.UNKNOWN
    assert r.values["lanes"]["elitebook"] is None


def test_deploy_lanes_no_lane_has_data_is_unknown() -> None:
    r = _run(
        _ctx(
            machines={"elitebook": _agent(None)},
            daemon_host={"coord_serve_version": None, "cli_venv_version": None},
        )
    )["fleet_deploy_lanes"]
    assert r.severity is Severity.UNKNOWN
    assert "no lane has a resolvable version" in r.headroom


# ── fleet_tui_binary ─────────────────────────────────────────────────────────


def test_tui_binary_newer_than_source_is_ok() -> None:
    r = _run(
        _ctx(
            daemon_host={
                "tui_binary_path": "/home/x/.local/bin/coord-tui",
                "tui_binary_mtime": NOW,
                "tui_source_mtime": NOW - 3600,
            }
        )
    )["fleet_tui_binary"]
    assert r.severity is Severity.OK
    assert "up to date" in r.headroom


def test_tui_binary_older_than_source_is_warn_with_the_staleness_in_hours() -> None:
    r = _run(
        _ctx(
            daemon_host={
                "tui_binary_path": "/home/x/.local/bin/coord-tui",
                "tui_binary_mtime": NOW - 9000,  # 2.5h before the newest source
                "tui_source_mtime": NOW,
            }
        )
    )["fleet_tui_binary"]
    assert r.severity is Severity.WARN
    assert "2.5h older" in r.headroom
    assert "rebuild" in r.detail


def test_tui_binary_exactly_equal_mtimes_is_ok_not_warn() -> None:
    """Boundary: `cp` preserving mtime must not read as stale forever."""
    r = _run(
        _ctx(
            daemon_host={
                "tui_binary_path": "/x/coord-tui",
                "tui_binary_mtime": NOW,
                "tui_source_mtime": NOW,
            }
        )
    )["fleet_tui_binary"]
    assert r.severity is Severity.OK


def test_tui_binary_missing_is_unknown_and_says_how_to_build_it() -> None:
    r = _run(
        _ctx(daemon_host={"tui_binary_path": "/home/x/.local/bin/coord-tui"})
    )["fleet_tui_binary"]
    assert r.severity is Severity.UNKNOWN
    assert "no binary at /home/x/.local/bin/coord-tui" in r.headroom
    assert "cargo build" in r.detail


def test_tui_binary_present_but_no_source_tree_is_ok_not_a_fabricated_verdict() -> None:
    r = _run(
        _ctx(daemon_host={"tui_binary_path": "/x/coord-tui", "tui_binary_mtime": NOW})
    )["fleet_tui_binary"]
    assert r.severity is Severity.OK
    assert "not found to compare" in r.headroom


# ── fleet_board_latency ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload_bytes", "expected"),
    [
        (1024, Severity.OK),
        (2 * 1024 * 1024 - 1, Severity.OK),
        (2 * 1024 * 1024, Severity.WARN),  # boundary: >= warns
        (4 * 1024 * 1024, Severity.WARN),
        (5 * 1024 * 1024, Severity.CRIT),  # boundary: >= crits
        (6 * 1024 * 1024, Severity.CRIT),  # the #1336 5.3MB payload's class
    ],
)
def test_board_payload_size_thresholds(payload_bytes, expected) -> None:
    r = _run(
        _ctx(daemon_host={"board_latency_ms": 5.0, "board_payload_bytes": payload_bytes})
    )["fleet_board_latency"]
    assert r.severity is expected


@pytest.mark.parametrize(
    ("latency_ms", "expected"),
    [
        (10.0, Severity.OK),
        (1499.0, Severity.OK),
        (1500.0, Severity.WARN),
        (3999.0, Severity.WARN),
        (4000.0, Severity.CRIT),
    ],
)
def test_board_latency_thresholds(latency_ms, expected) -> None:
    r = _run(
        _ctx(daemon_host={"board_latency_ms": latency_ms, "board_payload_bytes": 1024})
    )["fleet_board_latency"]
    assert r.severity is expected


def test_board_crit_payload_is_not_downgraded_by_a_merely_warn_latency() -> None:
    """Ordering trap: latency is judged after size, so a WARN-level latency
    must not overwrite a CRIT already set by the payload."""
    r = _run(
        _ctx(
            daemon_host={
                "board_latency_ms": 1600.0,  # WARN band
                "board_payload_bytes": 6 * 1024 * 1024,  # CRIT band
            }
        )
    )["fleet_board_latency"]
    assert r.severity is Severity.CRIT


def test_board_never_measured_is_unknown() -> None:
    r = _run(_ctx(daemon_host={}))["fleet_board_latency"]
    assert r.severity is Severity.UNKNOWN
    assert "no /board build measured yet" in r.headroom


def test_board_one_measurement_present_is_still_judged() -> None:
    """A daemon that recorded size but not latency (or vice versa) must be
    judged on what it has rather than falling back to "no data"."""
    r = _run(
        _ctx(daemon_host={"board_latency_ms": None, "board_payload_bytes": 6 * 1024 * 1024})
    )["fleet_board_latency"]
    assert r.severity is Severity.CRIT


# ── fleet_phantom_running ────────────────────────────────────────────────────


def test_phantom_zero_rows_is_ok() -> None:
    r = _run(_ctx(daemon_host={"phantom_running": []}))["fleet_phantom_running"]
    assert r.severity is Severity.OK
    assert "0 phantom" in r.headroom


def test_phantom_no_scan_yet_is_unknown_not_ok() -> None:
    """An empty list means "scanned, found none"; a missing key means "never
    scanned". Collapsing the second into the first is #1485's failure mode."""
    r = _run(_ctx(daemon_host={}))["fleet_phantom_running"]
    assert r.severity is Severity.UNKNOWN
    assert "no phantom-row scan yet" in r.headroom


def test_phantom_single_row_is_crit_and_named() -> None:
    r = _run(
        _ctx(
            daemon_host={
                "phantom_running": [
                    {"repo_name": "api", "issue_number": 42,
                     "machine": "mini", "assignment_id": "a1"}
                ]
            }
        )
    )["fleet_phantom_running"]
    assert r.severity is Severity.CRIT
    assert r.headroom == "1 phantom running row"  # singular
    assert "api#42@mini" in r.detail
    assert r.values["count"] == 1
    assert r.values["assignment_ids"] == ["a1"]


def test_phantom_many_rows_samples_five_and_says_there_are_more() -> None:
    rows = [
        {"repo_name": "api", "issue_number": n, "machine": "mini",
         "assignment_id": f"a{n}"}
        for n in range(8)
    ]
    r = _run(_ctx(daemon_host={"phantom_running": rows}))["fleet_phantom_running"]
    assert r.severity is Severity.CRIT
    assert r.headroom == "8 phantom running rows"  # plural
    assert r.detail.endswith(", ...")
    assert r.detail.count("api#") == 5
    # No silent cap: every id is still carried in `values` for a machine
    # consumer even though the human-facing detail samples five.
    assert len(r.values["assignment_ids"]) == 8


# ── daemon-host fact gathering: the defaults that make the lanes live ─────────


def test_tui_source_walk_finds_the_newest_rs_file(tmp_path: Path) -> None:
    src = tmp_path / "tui" / "src"
    (src / "widgets").mkdir(parents=True)
    old = src / "main.rs"
    old.write_text("fn main() {}")
    os.utime(old, (NOW - 10_000, NOW - 10_000))
    new = src / "widgets" / "board.rs"
    new.write_text("pub struct Board;")
    os.utime(new, (NOW, NOW))

    assert _newest_rust_source_mtime(src) == pytest.approx(NOW)


def test_tui_source_walk_skips_target_and_hidden_dirs(tmp_path: Path) -> None:
    """A `tui_source_dir` pointed at a crate root must not let a multi-GB
    `target/` dominate the mtime (or the walk's cost)."""
    src = tmp_path / "tui"
    src.mkdir()
    real = src / "lib.rs"
    real.write_text("")
    os.utime(real, (NOW - 10_000, NOW - 10_000))
    for junk_dir in ("target", ".git"):
        d = src / junk_dir / "deep"
        d.mkdir(parents=True)
        junk = d / "generated.rs"
        junk.write_text("")
        os.utime(junk, (NOW, NOW))

    assert _newest_rust_source_mtime(src) == pytest.approx(NOW - 10_000)


def test_tui_source_walk_on_a_missing_or_empty_dir_is_none(tmp_path: Path) -> None:
    assert _newest_rust_source_mtime(tmp_path / "nope") is None
    (tmp_path / "empty").mkdir()
    assert _newest_rust_source_mtime(tmp_path / "empty") is None


def test_default_tui_source_dir_comes_from_a_configured_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    checkout = tmp_path / "claude-coordinator"
    (checkout / "tui" / "src").mkdir(parents=True)
    fake_checkout = SimpleNamespace(name="coordinator", path=checkout)
    monkeypatch.setattr(
        "coord.health.context.local_checkouts", lambda cfg: (fake_checkout,)
    )
    assert _default_tui_source_dir(object()) == checkout / "tui" / "src"


def test_default_tui_source_dir_is_none_when_no_checkout_has_one(monkeypatch) -> None:
    monkeypatch.setattr("coord.health.context.local_checkouts", lambda cfg: ())
    assert _default_tui_source_dir(object()) is None


def test_default_tui_source_dir_survives_a_broken_config(monkeypatch) -> None:
    """A fact gatherer that raises would take the whole refresh tick down."""
    def _boom(cfg):
        raise RuntimeError("bad config")

    monkeypatch.setattr("coord.health.context.local_checkouts", _boom)
    assert _default_tui_source_dir(object()) is None


def test_daemon_host_facts_resolve_the_tui_lane_with_no_config_at_all(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: with no `health:` block, the tui lane must still resolve a
    concrete path (the README's install location) rather than reporting
    "not configured" forever."""
    home = tmp_path / "home"
    binary = home / ".local" / "bin" / "coord-tui"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    os.utime(binary, (NOW - 100, NOW - 100))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("coord.health.context.local_checkouts", lambda cfg: ())

    facts = FleetHealthRefresher._daemon_host_facts(
        SimpleNamespace(health=HealthConfig(), machines=(), repos=())
    )
    assert facts["tui_binary_path"] == str(binary)
    assert facts["tui_binary_mtime"] == pytest.approx(NOW - 100)

    # ...and that fact is enough for the probe to reach a real verdict.
    r = _run(_ctx(daemon_host=facts))["fleet_tui_binary"]
    assert r.severity is not Severity.UNKNOWN


def test_daemon_host_facts_honour_the_configured_overrides(
    tmp_path: Path, monkeypatch
) -> None:
    binary = tmp_path / "custom" / "coord-tui"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    os.utime(binary, (NOW - 5000, NOW - 5000))
    src = tmp_path / "elsewhere" / "src"
    src.mkdir(parents=True)
    rs = src / "main.rs"
    rs.write_text("")
    os.utime(rs, (NOW, NOW))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    cfg = SimpleNamespace(
        health=HealthConfig(
            tui_binary_path=str(binary),
            tui_source_dir=str(src),
            cli_venv_python=str(tmp_path / "no-such-venv" / "bin" / "python3"),
        ),
        machines=(),
        repos=(),
    )
    facts = FleetHealthRefresher._daemon_host_facts(cfg)
    assert facts["tui_binary_path"] == str(binary)
    assert facts["tui_binary_mtime"] == pytest.approx(NOW - 5000)
    assert facts["tui_source_mtime"] == pytest.approx(NOW)
    # An unreachable CLI venv is "no data", not a crash and not a version.
    assert facts["cli_venv_version"] is None

    r = _run(_ctx(daemon_host=facts))["fleet_tui_binary"]
    assert r.severity is Severity.WARN  # source is newer than the binary
