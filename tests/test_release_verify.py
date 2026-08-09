"""`coord release verify` and the `spawned_coord` lane behind it (#1834).

The centrepiece is
:func:`test_2026_08_04_daemon_spawns_two_releases_back_is_caught`, which
replays the exact drift the command was written for: a daemon host whose
agent venv, CLI venv, coord-serve process, unit files and PyPI index all read
0.4.105 while ``shutil.which("coord")`` inside the running ``coord-serve``
resolved to an editable checkout on 0.4.103. Four green readouts, one split
brain. Per #1544's standard — *a check that has never caught the bug it was
written for is not a check* — that test drives the real probe against a real
(temporary) console script and a real fake ``/proc`` entry, not a hand-shaped
result dict, so it fails if any link in the chain regresses:

    live PATH -> shutil.which -> the resolved binary -> its version
        -> the machine-scope CheckResult -> the fleet lane map
        -> `coord release verify`'s exit code

Everything else here pins the design constraints the issue spells out:
skew-between-lanes rather than staleness-within-one, editable-is-a-finding-on-
its-own, unreachable-is-never-OK, and read-only.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import release_verify as rv
from coord.config import HealthConfig, _DEFAULT_SPAWNED_COORD_UNITS
from coord.health.checks import spawned_coord
from coord.health.models import FleetSnapshot, HealthContext, Severity
from coord.health.registry import run_all

NOW = 1_800_000_000.0

RELEASED = "0.4.105"
STALE = "0.4.103"


# ──────────────────────────────────────────────────────────────────────────
# helpers: build the payload shapes the real transports produce
# ──────────────────────────────────────────────────────────────────────────


def _result(check_id: str, *, subject: str | None = None, severity: str = "ok",
            headroom: str = "", detail: str = "", error: str | None = None,
            **values) -> dict:
    """One row of an agent's `/health` -> `health.results` list."""
    row = {
        "key": f"{check_id}:{subject}" if subject else check_id,
        "check_id": check_id,
        "scope": "machine",
        "subject": subject,
        "severity": severity,
        "headroom": headroom,
        "detail": detail,
        "values": values,
    }
    if error:
        row["error"] = error
    return row


def _health(*results: dict) -> dict:
    """An agent `/health` body, shaped like coord/agent.py's."""
    return {"version": RELEASED, "health": {"schema": 1, "results": list(results)}}


def _agent_venv(version: str | None, *, editable: bool = False) -> dict:
    return _result("agent_venv", version=version, editable=editable)


def _cli_venv(version: str | None) -> dict:
    return _result("cli_venv", present=True, version=version, editable=False)


def _spawns(unit: str, version: str | None, *, editable: bool = False,
            severity: str = "ok", resolved: str = "/x/bin/coord") -> dict:
    return _result(
        "spawned_coord", subject=unit, severity=severity,
        unit=unit, pid=42, version=version, editable=editable,
        resolved=resolved, fallback=False,
    )


# ──────────────────────────────────────────────────────────────────────────
# the reproduction: 2026-08-04, end to end through the real probe
# ──────────────────────────────────────────────────────────────────────────


def _fake_coord_script(tmp_path: Path, *, version: str, editable: bool) -> Path:
    """A real `coord` console script whose interpreter reports *version*.

    Deliberately a genuine executable with a genuine shebang rather than a
    monkeypatched lookup: the thing under test is that ``shutil.which`` on a
    live PATH finds *this* file and that we can read a version out of it, and
    a stubbed resolver would assert nothing about either.

    *editable* controls whether the interpreter's ``coord.__file__`` lands
    under ``site-packages`` (a release) or in a bare checkout directory (an
    editable install) — the exact signal
    :func:`coord.health.checks.spawned_coord.is_editable` reads.
    """
    root = tmp_path / ("checkout" if editable else "release")
    if editable:
        module_file = root / "src" / "claude-coordinator" / "coord" / "__init__.py"
    else:
        module_file = (
            root / "lib" / "python3" / "site-packages" / "coord" / "__init__.py"
        )
    module_file.parent.mkdir(parents=True, exist_ok=True)
    module_file.write_text(f'__version__ = "{version}"\n')

    # A stand-in interpreter: `python -c "import coord;..."` is what the probe
    # runs, so a shim that answers with this version + module path is a
    # faithful substitute for a whole second venv, and is fast enough to run
    # in a unit test.
    interpreter = root / "bin" / "python3"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n%s\\n" "{version}" "{module_file}"\n'
    )
    interpreter.chmod(interpreter.stat().st_mode | stat.S_IEXEC)

    bindir = root / "bin"
    script = bindir / "coord"
    script.write_text(f"#!{interpreter}\n# console script stub\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _fake_proc(tmp_path: Path, pid: int, path_value: str) -> Path:
    """A `/proc/<pid>/environ` that holds *path_value*, NUL-separated."""
    proc_root = tmp_path / "proc"
    entry = proc_root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "environ").write_bytes(
        b"LANG=C\0PATH=" + path_value.encode() + b"\0HOME=/home/x\0"
    )
    return proc_root


def _ctx(home: Path) -> HealthContext:
    return HealthContext(
        thresholds=HealthConfig(),
        home=home,
        coord_dir=home / ".coord",
        now=NOW,
        allow_network=False,
    )


def test_2026_08_04_daemon_spawns_two_releases_back_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident, replayed through the real probe and the real command.

    dellserver's `coord-serve` unit began its `Environment=PATH=` with an
    editable checkout of claude-coordinator two releases behind. The daemon
    process itself was 0.4.105; every subprocess `coord_argv()` spawned was
    0.4.103. PyPI, `coord status` on all three agents,
    `~/.coord-venv/bin/coord version` and `~/.local/bin/coord version` all
    said 0.4.105.
    """
    stale = _fake_coord_script(tmp_path, version=STALE, editable=True)
    released = _fake_coord_script(tmp_path, version=RELEASED, editable=False)

    # The unit's PATH, verbatim in shape: the editable checkout FIRST, the
    # release entry point after it. This is the whole bug.
    service_path = f"{stale.parent}:{released.parent}:/usr/bin:/bin"
    proc_root = _fake_proc(tmp_path, 4242, service_path)

    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(
        spawned_coord, "running_unit_pids", lambda units: {"coord-serve": 4242}
    )
    # The reporting process (the agent on that host) is on the release.
    monkeypatch.setattr(spawned_coord, "OWN_VERSION", RELEASED)

    # ── 1. the machine-scope probe sees it ───────────────────────────────
    results = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert len(results) == 1
    row = results[0]
    assert row.subject == "coord-serve"
    assert row.severity is Severity.CRIT, row.headroom
    assert row.values["version"] == STALE
    assert row.values["resolved"] == str(stale)
    assert row.values["editable"] is True

    # ── 2. shutil.which really is what picked the stale one ──────────────
    assert spawned_coord.resolve_coord(service_path) == str(stale)

    # ── 3. `coord release verify` fails, naming the host AND the lane ────
    health = _health(
        _agent_venv(RELEASED),
        _cli_venv(RELEASED),
        _result("unit_drift", subject="coord-serve.service", severity="ok",
                matches=True),
        _spawns("coord-serve", STALE, editable=True, severity="crit",
                resolved=str(stale)),
    )
    report = rv.verify(
        machine_health={"dellserver": health},
        daemon_host={"coord_serve_version": RELEASED},
        expected=RELEASED,
    )
    assert report.severity == "crit"
    assert report.exit_code == rv.EXIT_CRIT

    rendered = rv.render(report)
    assert "dellserver" in rendered
    assert "coord-serve spawns" in rendered
    assert STALE in rendered
    # The four readouts that lied must still be shown as green lanes — the
    # report's value is the *relationship*, and hiding the agreeing lanes
    # would make the skew unexplainable.
    assert RELEASED in rendered
    assert "SKEW" in rendered


def test_the_pre_1834_lane_set_alone_would_have_passed(tmp_path: Path) -> None:
    """Control for the test above: without the `spawns` lane, 2026-08-04 is
    invisible. This is why the issue calls the enumeration the deliverable.

    If someone deletes the spawned_coord projection from `lanes_for_host`,
    the reproduction test above still has to fail — this test is what proves
    the reproduction is not passing for some incidental reason.
    """
    health = _health(
        _agent_venv(RELEASED),
        _cli_venv(RELEASED),
        _result("unit_drift", subject="coord-serve.service", severity="ok"),
    )
    report = rv.verify(
        machine_health={"dellserver": health},
        daemon_host={"coord_serve_version": RELEASED},
        expected=RELEASED,
    )
    assert report.ok, report.findings


# ──────────────────────────────────────────────────────────────────────────
# spawned_coord: the machine-scope probe's own contract
# ──────────────────────────────────────────────────────────────────────────


def test_no_running_coord_service_is_ok_not_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thin clients and plain workers run no coord user units at all. That is
    an absence, not a fault — same convention as cli_venv/tui_binary."""
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda units: {})
    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.subject is None
    assert "no coord service running" in result.headroom


def test_matching_spawned_version_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    released = _fake_coord_script(tmp_path, version=RELEASED, editable=False)
    proc_root = _fake_proc(tmp_path, 7, f"{released.parent}:/usr/bin")
    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda u: {"coord-agent": 7})
    monkeypatch.setattr(spawned_coord, "OWN_VERSION", RELEASED)

    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.values["version"] == RELEASED
    assert result.values["editable"] is False


def test_editable_is_crit_even_when_the_version_agrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1834, explicitly: "any editable install on a service PATH is a finding
    on its own, independent of its current version — it is a drift amplifier
    that silently tracks a checkout nothing keeps current."."""
    editable = _fake_coord_script(tmp_path, version=RELEASED, editable=True)
    proc_root = _fake_proc(tmp_path, 8, f"{editable.parent}:/usr/bin")
    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda u: {"coord-serve": 8})
    monkeypatch.setattr(spawned_coord, "OWN_VERSION", RELEASED)

    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert result.severity is Severity.CRIT
    assert result.values["version"] == RELEASED  # agrees...
    assert "EDITABLE" in result.headroom       # ...and is still a finding


def test_no_coord_on_the_service_path_is_ok_not_a_missing_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`coord_argv()` falls back to `sys.executable -m coord.cli` — the
    parent's own install, which cannot disagree with the parent. Reporting
    that as a gap would put a permanent UNKNOWN on every correct fleet."""
    proc_root = _fake_proc(tmp_path, 9, "/nonexistent-a:/nonexistent-b")
    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", proc_root)
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda u: {"coord-web": 9})

    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.values["fallback"] is True
    assert result.values["version"] is None


def test_unreadable_process_environ_is_unknown_never_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service running as another user is unverified, which is not the same
    as in sync."""
    monkeypatch.setattr(spawned_coord, "_PROC_ROOT", tmp_path / "empty-proc")
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda u: {"coord-serve": 1})

    (result,) = spawned_coord.probe_spawned_coord(_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert result.error


def test_process_path_reads_the_kernels_copy_not_the_unit_file(
    tmp_path: Path,
) -> None:
    """The reason this check exists alongside `unit_drift`: a drop-in, an
    EnvironmentFile, or `systemctl --user set-environment` changes the live
    PATH without touching any file `unit_drift` reads."""
    proc_root = _fake_proc(tmp_path, 55, "/injected/bin:/usr/bin")
    assert (
        spawned_coord.process_path(55, proc_root=proc_root)
        == "/injected/bin:/usr/bin"
    )
    assert spawned_coord.process_path(56, proc_root=proc_root) is None


def test_is_editable_is_unknown_not_false_without_a_module_path() -> None:
    """Guessing "not editable" from missing evidence would silence exactly
    the finding this exists for."""
    assert spawned_coord.is_editable(None) is None
    assert spawned_coord.is_editable("/x/lib/python3.11/site-packages/coord/__init__.py") is False
    assert spawned_coord.is_editable("/home/j/src/claude-coordinator/coord/__init__.py") is True


def test_probe_is_registered_in_the_machine_scope_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driven through `run_all` so an id typo or a wrong scope fails here
    rather than silently never running on any machine."""
    monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda units: {})
    from coord.health import checks  # noqa: F401

    report = run_all(_ctx(tmp_path), scopes=("machine",), only=["spawned_coord"])
    assert [r.check_id for r in report.results] == ["spawned_coord"]


def test_default_units_match_the_config_default() -> None:
    """The two lists are duplicated to keep config from importing the check
    registry; this is the pin that keeps them honest."""
    assert spawned_coord.DEFAULT_UNITS == _DEFAULT_SPAWNED_COORD_UNITS
    assert spawned_coord.configured_units(_ctx(Path("/nonexistent"))) == \
        tuple(_DEFAULT_SPAWNED_COORD_UNITS)


def test_empty_configured_units_disables_the_check(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.thresholds.spawned_coord_units = []
    assert spawned_coord.configured_units(ctx) == ()
    (result,) = spawned_coord.probe_spawned_coord(ctx)
    assert result.severity is Severity.OK


def test_running_unit_pids_survives_a_machine_with_no_systemd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS, containers, thin clients. Not a fault, not a crash."""
    def boom(*a, **k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(spawned_coord.subprocess, "run", boom)
    assert spawned_coord.running_unit_pids(("coord-serve",)) == {}


def test_running_unit_pids_ignores_inactive_and_pidless_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Proc:
        stdout = (
            "Id=coord-serve.service\nMainPID=1234\nActiveState=active\n"
            "\n"
            "Id=coord-web.service\nMainPID=0\nActiveState=inactive\n"
            "\n"
            "Id=coord-agent.service\nMainPID=0\nActiveState=active\n"
        )

    monkeypatch.setattr(spawned_coord.subprocess, "run", lambda *a, **k: Proc())
    assert spawned_coord.running_unit_pids(("coord-serve", "coord-web", "coord-agent")) == {
        "coord-serve": 1234
    }


# ──────────────────────────────────────────────────────────────────────────
# the fleet lane map
# ──────────────────────────────────────────────────────────────────────────


def _fleet_ctx(machines: dict, daemon_host: dict | None = None) -> HealthContext:
    ctx = HealthContext(
        thresholds=HealthConfig(),
        home=Path("/nonexistent-home"),
        coord_dir=Path("/nonexistent-home/.coord"),
        now=NOW,
        allow_network=False,
    )
    ctx.fleet = FleetSnapshot(machines=machines, daemon_host=daemon_host or {})
    return ctx


def _machine(*results: dict) -> dict:
    return {"state": "online", "checks": {"results": list(results)}}


def test_fleet_deploy_lanes_reads_every_unit_not_just_the_first(monkeypatch) -> None:
    """`spawned_coord` reports one row PER UNIT. A first-match read would see
    only `coord-agent` and structurally miss `coord-serve` — the one unit
    whose spawned version was the entire 2026-08-04 incident."""
    from coord.health import checks  # noqa: F401

    ctx = _fleet_ctx(
        {
            "dellserver": _machine(
                _agent_venv(RELEASED),
                _spawns("coord-agent", RELEASED),
                _spawns("coord-serve", STALE),
            )
        },
        {"coord_serve_version": RELEASED},
    )
    result = {r.check_id: r for r in run_all(ctx, scopes=("fleet",)).results}[
        "fleet_deploy_lanes"
    ]
    assert result.severity is Severity.CRIT
    assert "coord-serve spawns (dellserver)" in result.values["lanes"]
    assert result.values["lanes"]["coord-serve spawns (dellserver)"] == STALE
    assert "coord-serve spawns (dellserver)" in result.detail


def test_fleet_deploy_lanes_spawn_lane_never_manufactures_a_missing_lane() -> None:
    """A unit whose PATH has no `coord` on it is not a lane at all; admitting
    it as a null one would put a permanent UNKNOWN on every correct fleet."""
    from coord.health import checks  # noqa: F401

    fallback = _result("spawned_coord", subject="coord-web", severity="ok",
                       unit="coord-web", fallback=True, version=None)
    ctx = _fleet_ctx(
        {"dellserver": _machine(_agent_venv(RELEASED), _cli_venv(RELEASED), fallback)},
        {"coord_serve_version": RELEASED},
    )
    result = {r.check_id: r for r in run_all(ctx, scopes=("fleet",)).results}[
        "fleet_deploy_lanes"
    ]
    assert not any("spawns" in name for name in result.values["lanes"])
    # Every other lane agrees and none is missing, so the fallback unit must
    # leave the verdict at OK rather than dragging it to UNKNOWN.
    assert result.severity is Severity.OK


def test_fleet_deploy_lanes_errored_spawn_row_is_not_a_version() -> None:
    from coord.health import checks  # noqa: F401

    errored = _result("spawned_coord", subject="coord-serve", severity="unknown",
                      unit="coord-serve", version=None, error="environ unreadable")
    errored["error"] = "environ unreadable"
    ctx = _fleet_ctx(
        {"dellserver": _machine(_agent_venv(RELEASED), errored)},
        {"coord_serve_version": RELEASED},
    )
    result = {r.check_id: r for r in run_all(ctx, scopes=("fleet",)).results}[
        "fleet_deploy_lanes"
    ]
    assert not any("spawns" in name for name in result.values["lanes"])


# ──────────────────────────────────────────────────────────────────────────
# verify(): the judgement
# ──────────────────────────────────────────────────────────────────────────


def test_a_correctly_deployed_fleet_passes() -> None:
    report = rv.verify(
        machine_health={
            "dellserver": _health(_agent_venv(RELEASED), _spawns("coord-serve", RELEASED)),
            "elitebook": _health(_agent_venv(RELEASED), _cli_venv(RELEASED)),
        },
        daemon_host={"coord_serve_version": RELEASED},
        expected=RELEASED,
    )
    assert report.ok, rv.render(report)
    assert report.exit_code == rv.EXIT_OK


def test_skew_is_crit_with_no_expected_version_at_all() -> None:
    """The 2026-08-04 shape: nobody knew what to expect, but two lanes
    disagreeing was already conclusive. Staleness-within-one-lane logic
    cannot express this."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(_agent_venv(RELEASED), _spawns("coord-serve", STALE)),
        },
        daemon_host={"coord_serve_version": RELEASED},
    )
    assert report.severity == "crit"
    skew = [f for f in report.findings if f.lane == "(version skew)"]
    assert skew and STALE in skew[0].detail and RELEASED in skew[0].detail


def test_expected_version_names_the_offending_host_and_lane() -> None:
    report = rv.verify(
        machine_health={
            "dellserver": _health(_agent_venv(RELEASED)),
            "precision": _health(_agent_venv(STALE)),
        },
        expected=RELEASED,
    )
    assert report.severity == "crit"
    bad = [f for f in report.findings if f.severity == "crit"]
    assert bad
    assert any("precision" in f.host for f in bad)
    assert any(STALE in f.summary for f in bad)


def test_unreachable_host_is_unknown_never_ok() -> None:
    """"We could not ask" must not render as "verified" — that is the whole
    thesis of the issue."""
    report = rv.verify(
        machine_health={"dellserver": _health(_agent_venv(RELEASED))},
        unreachable={"precision": "connection refused"},
        expected=RELEASED,
    )
    assert not report.ok
    assert report.exit_code == rv.EXIT_WARN
    assert any(f.host == "precision" and f.severity == "unknown" for f in report.findings)


def test_a_lane_with_no_version_is_unknown_not_agreement() -> None:
    report = rv.verify(
        machine_health={"precision": _health(_agent_venv(None))},
        expected=RELEASED,
    )
    assert report.severity == "unknown"
    assert any("no version reported" in f.summary for f in report.findings)


def test_editable_agent_venv_is_crit_on_its_own() -> None:
    report = rv.verify(
        machine_health={
            "precision": _health(_agent_venv(RELEASED, editable=True)),
        },
        expected=RELEASED,
    )
    assert report.severity == "crit"
    assert any("EDITABLE" in f.summary for f in report.findings)


def test_unit_drift_and_tui_staleness_are_folded_in() -> None:
    """Lanes 3 and 4 of the issue's enumeration ride the same report rather
    than needing a second command."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result("unit_drift", subject="coord-serve.service", severity="crit",
                        headroom="PATH shadow risk", detail="reorder PATH="),
                _result("tui_binary", severity="warn", headroom="binary is 30.0h older"),
            )
        },
        expected=RELEASED,
    )
    lanes = {f.lane for f in report.findings}
    assert "unit coord-serve.service" in lanes
    assert "coord-tui" in lanes
    assert report.severity == "crit"


def test_unit_drift_against_an_unverified_reference_is_reported_not_dropped() -> None:
    """#1927: this command is the trust anchor #1835 gates on, so a match the
    machine could not vouch for has to reach the report. It rides as UNKNOWN
    — it must annotate the green, not page."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result(
                    "unit_drift",
                    subject="coord-serve.service",
                    severity="unknown",
                    headroom=(
                        "matches /home/john/src/claude-coordinator/deploy, but "
                        "that reference is an unverified working copy"
                    ),
                    detail="install a release wheel on this host",
                ),
            )
        },
        expected=RELEASED,
    )
    finding = next(f for f in report.findings if f.lane == "unit coord-serve.service")
    assert finding.severity == "unknown"
    assert "unverified working copy" in finding.summary
    assert report.severity == "unknown"  # annotated, not paged


def test_webapp_bundle_staleness_is_folded_in() -> None:
    """Lane 5 of the issue's enumeration — the webapp bundle — rides the same
    report too, on staleness-vs-source terms rather than a version (see
    coord/health/checks/fleet_deploy_lanes.py's module docstring for why a
    version comparison would be meaningless here)."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result("webapp_bundle", severity="warn",
                        headroom="bundle is 3.0h older than webapp/ source",
                        detail="check coord-web-dist-build.timer on: dellserver"),
            )
        },
        expected=RELEASED,
    )
    lanes = {f.lane for f in report.findings}
    assert "webapp bundle" in lanes
    assert report.severity == "warn"  # agent_venv alone is already clean here
    warn_findings = [f for f in report.findings if f.lane == "webapp bundle"]
    assert warn_findings[0].severity == "warn"
    assert "coord-web-dist-build.timer" in warn_findings[0].detail


def test_webapp_bundle_never_becomes_a_version_lane() -> None:
    """The bundle is SHA-versioned off a continuous publish timer (#1543),
    never pip-versioned — folding it into the version-skew map would
    manufacture permanent, meaningless skew against every other lane's
    semver string. It must never appear in report.lanes at all."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(
                _agent_venv(RELEASED),
                _result("webapp_bundle", severity="ok", headroom="up to date",
                        present=True, sha="abc123", dist_mtime=1.0),
            )
        },
        expected=RELEASED,
    )
    assert report.ok, rv.render(report)
    assert not any(lane.lane == "webapp bundle" for lane in report.lanes)
    assert "abc123" not in report.versions


def test_absent_cli_venv_is_not_a_lane() -> None:
    """Most machines never had one. An absent optional lane must not become a
    permanent UNKNOWN."""
    absent = _result("cli_venv", present=False, version=None)
    report = rv.verify(
        machine_health={"precision": _health(_agent_venv(RELEASED), absent)},
        expected=RELEASED,
    )
    assert report.ok, rv.render(report)
    assert not any(lane.lane == "~/.coord-cli-venv" for lane in report.lanes)


def test_an_empty_fleet_reading_is_unknown_not_a_pass() -> None:
    report = rv.verify(machine_health={"precision": None}, expected=RELEASED)
    assert report.severity == "unknown"
    assert not report.ok


def test_report_renders_every_lane_even_on_success() -> None:
    """The failure mode this command exists for is a readout that says "fine"
    while hiding the lane it never looked at, so the inspected lane set is
    part of the answer, not debug output."""
    report = rv.verify(
        machine_health={"dellserver": _health(_agent_venv(RELEASED),
                                              _spawns("coord-serve", RELEASED))},
        expected=RELEASED,
    )
    out = rv.render(report)
    assert "~/.coord-venv" in out
    assert "coord-serve spawns" in out
    assert "RELEASE VERIFY: OK" in out


def test_to_dict_is_json_serialisable_and_names_lanes() -> None:
    report = rv.verify(
        machine_health={"dellserver": _health(_agent_venv(STALE))},
        expected=RELEASED,
    )
    blob = json.loads(json.dumps(report.to_dict()))
    assert blob["severity"] == "crit"
    assert blob["exit_code"] == rv.EXIT_CRIT
    assert blob["lanes"][0]["host"] == "dellserver"


# ──────────────────────────────────────────────────────────────────────────
# transport: works from a thin client, and never writes
# ──────────────────────────────────────────────────────────────────────────


class _Machine:
    def __init__(self, name: str) -> None:
        self.name = name
        self.host = name


class _Config:
    def __init__(self, machines) -> None:
        self.machines = machines
        self.health = HealthConfig()


class _Status:
    def __init__(self, *, online: bool, health=None, reason: str = "") -> None:
        self.is_online = online
        self.health = health
        self.reason = reason
        self.state = "online" if online else "offline"


def test_gather_polls_every_machine_over_http_and_records_offline_ones() -> None:
    config = _Config([_Machine("dellserver"), _Machine("precision")])
    seen: list[str] = []

    def probe(machine, timeout=5.0):
        seen.append(machine.name)
        if machine.name == "precision":
            return _Status(online=False, reason="connection refused")
        return _Status(online=True, health=_health(_agent_venv(RELEASED)))

    health, unreachable, daemon, name = rv.gather(
        config, check_machine=probe, board_payload=lambda: {}
    )
    assert seen == ["dellserver", "precision"]
    assert set(health) == {"dellserver"}
    assert unreachable == {"precision": "connection refused"}
    assert daemon is None and name == "daemon"


def test_gather_survives_a_probe_that_raises() -> None:
    config = _Config([_Machine("dellserver")])

    def probe(machine, timeout=5.0):
        raise RuntimeError("tailscale down")

    health, unreachable, _daemon, _name = rv.gather(
        config, check_machine=probe, board_payload=lambda: {}
    )
    assert health == {}
    assert "tailscale down" in unreachable["dellserver"]


def test_gather_reads_coord_serve_version_out_of_the_board_payload() -> None:
    """The daemon's own version is process-local (#1806) — a thin client can
    only get it from `/board`'s published fleet_deploy_lanes row."""
    payload = {
        "fleet_health": {
            "fleet_checks": [
                {
                    "check_id": "fleet_deploy_lanes",
                    "values": {"lanes": {rv.DAEMON_SERVE_LANE: RELEASED}},
                }
            ]
        }
    }
    config = _Config([])
    _h, _u, daemon, _n = rv.gather(
        config, check_machine=lambda m, timeout=5.0: None, board_payload=lambda: payload
    )
    assert daemon == {"coord_serve_version": RELEASED}


def test_daemon_serve_lane_name_matches_what_the_fleet_check_publishes() -> None:
    """Pins the wire-format string across the two modules: a rename in
    fleet_deploy_lanes must break here loudly, not silently drop the lane."""
    from coord.health import checks  # noqa: F401

    ctx = _fleet_ctx({}, {"coord_serve_version": RELEASED})
    result = {r.check_id: r for r in run_all(ctx, scopes=("fleet",)).results}[
        "fleet_deploy_lanes"
    ]
    assert rv.DAEMON_SERVE_LANE in result.values["lanes"]


def test_board_fetch_falls_back_to_loopback_on_the_daemon_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the daemon host `resolve_board_service()` is None (host mode reads
    the DB directly). Without this fallback, running the command *on the
    daemon host* would silently drop the `coord-serve process` lane — exactly
    the lane #1834 exists to stop losing."""
    seen: dict = {}

    monkeypatch.setattr("coord.client.resolve_board_service", lambda *a, **k: None)
    monkeypatch.setattr("coord.serve_app.resolve_serve_token", lambda *a, **k: "tok")

    def fake_fetch(svc, *, timeout=None):
        seen["url"] = svc.url
        seen["token"] = svc.token
        seen["timeout"] = timeout
        return {}

    monkeypatch.setattr("coord.client.fetch_board_payload", fake_fetch)
    assert rv._default_board_fetch() == {}
    assert seen["url"].startswith("http://127.0.0.1:")
    assert seen["token"] == "tok"
    # NOT the per-host --timeout: /board is a multi-megabyte read and a 5s
    # budget makes a healthy daemon look unreachable (a recorded gotcha).
    assert seen["timeout"] == rv._BOARD_TIMEOUT >= 30.0


def test_a_board_that_cannot_be_read_is_no_data_not_a_crash() -> None:
    def boom():
        raise ConnectionError("board unreachable")

    config = _Config([])
    _h, _u, daemon, _n = rv.gather(
        config, check_machine=lambda m, timeout=5.0: None, board_payload=boom
    )
    assert daemon is None


def test_machine_filter_polls_only_that_machine() -> None:
    config = _Config([_Machine("dellserver"), _Machine("precision")])
    seen: list[str] = []

    def probe(machine, timeout=5.0):
        seen.append(machine.name)
        return _Status(online=True, health=_health(_agent_venv(RELEASED)))

    rv.gather(config, check_machine=probe, board_payload=lambda: {},
              machine_filter="precision")
    assert seen == ["precision"]


# ──────────────────────────────────────────────────────────────────────────
# the CLI surface
# ──────────────────────────────────────────────────────────────────────────


def test_cli_release_verify_exits_nonzero_and_names_the_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coord.cli import main

    monkeypatch.setattr(
        "coord.commands._common._load_config",
        lambda path: _Config([_Machine("dellserver")]),
    )
    monkeypatch.setattr(
        rv, "gather",
        lambda config, **kw: (
            {"dellserver": _health(_agent_venv(RELEASED),
                                   _spawns("coord-serve", STALE))},
            {},
            {"coord_serve_version": RELEASED},
            "daemon",
        ),
    )
    result = CliRunner().invoke(
        main, ["release", "verify", "--expected", "v" + RELEASED,
               "--config", str(tmp_path / "coordinator.yml")]
    )
    assert result.exit_code == rv.EXIT_CRIT, result.output
    assert "dellserver" in result.output
    assert "coord-serve spawns" in result.output
    assert STALE in result.output


def test_cli_release_verify_passes_on_a_clean_fleet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coord.cli import main

    monkeypatch.setattr(
        "coord.commands._common._load_config",
        lambda path: _Config([_Machine("dellserver")]),
    )
    monkeypatch.setattr(
        rv, "gather",
        lambda config, **kw: (
            {"dellserver": _health(_agent_venv(RELEASED),
                                   _spawns("coord-serve", RELEASED))},
            {}, {"coord_serve_version": RELEASED}, "daemon",
        ),
    )
    result = CliRunner().invoke(
        main, ["release", "verify", "--expected", RELEASED, "--no-pypi",
               "--config", str(tmp_path / "coordinator.yml")]
    )
    assert result.exit_code == 0, result.output
    assert "RELEASE VERIFY: OK" in result.output


def test_cli_release_verify_json_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from coord.cli import main

    monkeypatch.setattr(
        "coord.commands._common._load_config",
        lambda path: _Config([_Machine("dellserver")]),
    )
    monkeypatch.setattr(
        rv, "gather",
        lambda config, **kw: (
            {"dellserver": _health(_agent_venv(RELEASED))}, {}, None, "daemon",
        ),
    )
    result = CliRunner().invoke(
        main, ["release", "verify", "--json", "--no-exit-code", "--no-pypi",
               "--config", str(tmp_path / "coordinator.yml")]
    )
    assert result.exit_code == 0, result.output
    blob = json.loads(result.output)
    assert blob["lanes"][0]["lane"] == "~/.coord-venv"


def test_cli_release_preflight_is_still_reachable_both_ways() -> None:
    """The flat command is in every operator's muscle memory and in
    docs/AGENT_OPERATIONS.md; grouping must not break it."""
    from coord.cli import main

    for argv in (["release-preflight", "--help"], ["release", "preflight", "--help"]):
        result = CliRunner().invoke(main, argv)
        assert result.exit_code == 0, (argv, result.output)
        assert "1471" in result.output


def test_verify_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """`coord diagnose` is a documented trap for having write side effects;
    this command must be safe to run at any time, including mid-flight."""
    before = sorted(p.name for p in tmp_path.iterdir())
    rv.verify(
        machine_health={"dellserver": _health(_agent_venv(RELEASED))},
        expected=RELEASED,
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# ──────────────────────────────────────────────────────────────────────────
# #2052 fault 3 / #2035 item 4: uniform staleness must not read as health
# ──────────────────────────────────────────────────────────────────────────


def test_a_uniformly_stale_fleet_is_not_reported_clean() -> None:
    """The demonstration, not the hypothesis. After #2052's botched
    propagation reverted the fleet, every lane agreed on 0.4.104 while `main`
    was four releases ahead — and `coord release verify` said crit=0, because
    it compares the fleet against *itself*. Agreement is not currency, and a
    skew-only run must say so rather than render a clean bill of health."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(_agent_venv(STALE)),
            "elitebook": _health(_agent_venv(STALE)),
        },
    )
    assert not report.ok, rv.render(report)
    assert report.severity == "unknown"  # annotated, never paged
    finding = next(f for f in report.findings if f.lane == "(expected version)")
    assert "uniformly BEHIND" in finding.summary
    assert "--pypi" in finding.detail


def test_the_no_expected_finding_never_masks_real_skew() -> None:
    """Skew is already conclusive without an expected version — the #2035
    annotation must not downgrade or duplicate it."""
    report = rv.verify(
        machine_health={
            "dellserver": _health(_agent_venv(RELEASED)),
            "elitebook": _health(_agent_venv(STALE)),
        },
    )
    assert report.severity == "crit"
    assert not any(f.lane == "(expected version)" for f in report.findings)


def test_an_expected_version_suppresses_the_annotation() -> None:
    report = rv.verify(
        machine_health={"dellserver": _health(_agent_venv(RELEASED))},
        expected=RELEASED,
    )
    assert report.ok, rv.render(report)


def test_an_empty_lane_set_does_not_get_the_no_expected_annotation() -> None:
    """"No lanes at all" already has its own, better finding; adding "and we
    don't know what to expect" on top would be noise about nothing."""
    report = rv.verify(machine_health={})
    assert not any(f.lane == "(expected version)" for f in report.findings)


# ──────────────────────────────────────────────────────────────────────────
# #2052 fault 2: the daemon host is DERIVED, never guessed
# ──────────────────────────────────────────────────────────────────────────


def test_the_daemon_host_is_derived_from_a_running_coord_serve() -> None:
    """It is not a mystery: it is the machine with a live `coord-serve`, and
    every agent already publishes exactly that in its own /health."""
    assert rv.daemon_host_from_health({
        "precision": _health(_agent_venv(RELEASED), _spawns("coord-agent", RELEASED)),
        "dellserver": _health(_agent_venv(RELEASED),
                              _spawns("coord-agent", RELEASED),
                              _spawns("coord-serve", RELEASED)),
    }) == "dellserver"


def test_no_running_coord_serve_anywhere_is_none_not_a_guess() -> None:
    assert rv.daemon_host_from_health({
        "precision": _health(_agent_venv(RELEASED), _spawns("coord-agent", RELEASED)),
    }) is None
    assert rv.daemon_host_from_health({"precision": None}) is None


def test_two_hosts_claiming_coord_serve_is_none_not_a_coin_flip() -> None:
    """Two live daemons is a fault in its own right. A caller that has to
    order a roll around "the" daemon must refuse, not pick one."""
    assert rv.daemon_host_from_health({
        "a": _health(_spawns("coord-serve", RELEASED)),
        "b": _health(_spawns("coord-serve", RELEASED)),
    }) is None


def test_gather_labels_the_daemon_lane_with_the_real_machine_name(monkeypatch) -> None:
    """A lane labelled "daemon" cannot be matched to a host by anything
    downstream — which is how propagation ended up guessing at config order."""
    class _M:
        def __init__(self, name): self.name = name; self.host = name

    class _Status:
        is_online = True
        health = _health(_agent_venv(RELEASED), _spawns("coord-serve", RELEASED))

    class _Cfg:
        machines = [_M("dellserver")]

    _health_map, _unreachable, _facts, name = rv.gather(
        _Cfg(),
        check_machine=lambda machine, **kw: _Status(),
        board_payload=lambda: {},
    )
    assert name == "dellserver"
