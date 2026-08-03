"""End-to-end tests for `coord doctor` (#1570 E) — the Click command, driven
the same way tests/test_cli_status_merge_queue.py drives `status`: mock
`coord.network.check_all` so the test is hermetic, then assert on the
command's actual output and exit code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import coord.network as network_mod
from coord.commands.status import doctor
from coord.network import ONLINE, OFFLINE, MachineStatus


def _run_doctor(config_path, monkeypatch, statuses, *, extra_args=None):
    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: statuses)
    runner = CliRunner()
    result = runner.invoke(
        doctor, ["--config", str(config_path), *(extra_args or [])],
        catch_exceptions=False,
    )
    return result


def _health(tool_versions: dict, machine=None) -> dict:
    """#1712: a realistic `/health` echoes back what the machine declares.
    A stub that always published `capabilities: []` now fires doctor's
    declared-but-unpublished CRIT in every test here, drowning out what each
    one is actually asserting — so pass the machine and echo it."""
    return {
        "machine": getattr(machine, "name", "x"),
        "capabilities": list(getattr(machine, "capabilities", None) or []),
        "repos": list(getattr(machine, "repos", None) or []),
        "tool_versions": tool_versions,
    }


def _ok_probe(capability: str | None = None) -> dict:
    return {
        "found": True, "version": "9.9.9", "min_version": None,
        "meets_floor": None, "capability": capability, "ok": True,
    }


def _missing_probe(capability: str | None = None) -> dict:
    return {
        "found": False, "version": None, "min_version": None,
        "meets_floor": None, "capability": capability, "ok": False,
    }


def test_doctor_exits_zero_when_everything_checks_out(
    valid_config_path, monkeypatch,
) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    machines = cfg.machines  # laptop [python], server [python, docker]

    statuses = [
        MachineStatus(
            machine=m, state=ONLINE, latency_ms=5.0,
            health=_health({
                "git": _ok_probe(), "gh": _ok_probe(),
                "python3": _ok_probe("python"),
            }, m),
        )
        for m in machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 0, result.output
    assert "✓ git" in result.output
    assert "✓ python3" in result.output
    # "docker" has no registered prereq — must not be reported at all.
    assert "docker" not in result.output


def test_doctor_flags_unreachable_machine(valid_config_path, monkeypatch) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(machine=cfg.machines[0], state=OFFLINE, reason="connection refused"),
        MachineStatus(
            machine=cfg.machines[1], state=ONLINE,
            health=_health({"git": _ok_probe(), "gh": _ok_probe()}, cfg.machines[1]),
        ),
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1
    assert "unreachable" in result.output
    assert "connection refused" in result.output


def test_doctor_flags_missing_baseline_tool(valid_config_path, monkeypatch) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({"git": _ok_probe(), "gh": _missing_probe()}, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1
    assert "✗ gh: not found" in result.output


def test_doctor_flags_claimed_capability_the_probe_contradicts(
    valid_config_path, monkeypatch,
) -> None:
    """`server` claims `docker` isn't registered (skipped) but claims
    nothing that maps to a failing probe in this fixture — use `python`
    instead: laptop claims `python` but its own probe says python3 is
    missing, which must surface as an unmet-capability line, not just a
    generic tool-missing line."""
    from coord.config import load

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({
                "git": _ok_probe(), "gh": _ok_probe(),
                "python3": _missing_probe("python"),
            }, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1
    assert "capability 'python' claimed but unmet" in result.output


def test_doctor_flags_agent_predating_tool_versions(
    valid_config_path, monkeypatch,
) -> None:
    """An agent that hasn't upgraded to #1570 B yet omits `tool_versions`
    from /health entirely — doctor must call this out distinctly rather
    than crash or silently pass it."""
    from coord.config import load

    cfg = load(valid_config_path)
    statuses = [
        MachineStatus(machine=m, state=ONLINE, health={"machine": m.name})
        for m in cfg.machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 1
    assert "predates #1570 B" in result.output


def test_doctor_machine_filter_narrows_to_one(valid_config_path, monkeypatch) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    laptop = next(m for m in cfg.machines if m.name == "laptop")
    statuses = [
        MachineStatus(
            machine=laptop, state=ONLINE,
            health=_health(
                {"git": _ok_probe(), "gh": _ok_probe(), "python3": _ok_probe("python")},
                laptop,
            ),
        ),
    ]
    result = _run_doctor(
        valid_config_path, monkeypatch, statuses, extra_args=["--machine", "laptop"],
    )
    assert result.exit_code == 0, result.output
    assert "laptop" in result.output
    assert "server" not in result.output


def test_doctor_unknown_machine_filter_errors(valid_config_path, monkeypatch) -> None:
    result = _run_doctor(
        valid_config_path, monkeypatch, [], extra_args=["--machine", "nope"],
    )
    assert result.exit_code == 2


# ── #1711: provider:opencode availability — declared vs. probed-and-met ────

OPENCODE_CONFIG = """\
repos:
  - name: api
    github: acme/api
    provider: opencode
machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: ["provider:opencode"]
    repos: [api]
providers:
  definitions:
    opencode:
      type: opencode
"""


@pytest.fixture
def opencode_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(OPENCODE_CONFIG)
    return p


def test_doctor_reports_provider_declared_and_probed_met(
    opencode_config_path, monkeypatch,
) -> None:
    """A machine that DECLARES `provider:opencode` AND whose probe found
    the binary reads green — same shape as any other capability."""
    from coord.config import load

    cfg = load(opencode_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({
                "git": _ok_probe(), "gh": _ok_probe(),
                "opencode": _ok_probe("provider:opencode"),
            }, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(opencode_config_path, monkeypatch, statuses)
    assert result.exit_code == 0, result.output
    assert "✓ opencode" in result.output


def test_doctor_flags_declared_provider_the_probe_contradicts(
    opencode_config_path, monkeypatch,
) -> None:
    """DECLARED (`provider:opencode` in capabilities) but PROBED-AND-UNMET
    (the opencode binary isn't actually on that machine) must surface as an
    unmet-capability line — the same "claimed but unmet" shape #1570 D
    already gives rust/gtk/browser, now covering provider availability too."""
    from coord.config import load

    cfg = load(opencode_config_path)
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({
                "git": _ok_probe(), "gh": _ok_probe(),
                "opencode": _missing_probe("provider:opencode"),
            }, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(opencode_config_path, monkeypatch, statuses)
    assert result.exit_code == 1
    assert "✗ opencode: not found" in result.output
    assert "capability 'provider:opencode' claimed but unmet" in result.output


def test_doctor_does_not_probe_undeclared_provider_capability(
    valid_config_path, monkeypatch,
) -> None:
    """A machine that never declared `provider:opencode` gets no opencode
    row at all — matches the existing "docker has no registered prereq /
    unclaimed capability isn't probed" posture for every other capability."""
    from coord.config import load

    cfg = load(valid_config_path)  # laptop/server declare only python/docker
    statuses = [
        MachineStatus(
            machine=m, state=ONLINE,
            health=_health({"git": _ok_probe(), "gh": _ok_probe()}, m),
        )
        for m in cfg.machines
    ]
    result = _run_doctor(valid_config_path, monkeypatch, statuses)
    assert result.exit_code == 0, result.output
    assert "opencode" not in result.output
