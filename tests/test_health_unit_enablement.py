"""Unit tests for the `unit_enablement` machine-scope health check (#2098).

`unit_drift` (tests: `tests/test_health_unit_drift.py`) already covers
"does the installed unit's content match the release". This module covers
the orthogonal question that actually cost a day: an installed unit whose
content is byte-perfect can still be `disabled`, and that state produces
no evidence until something needed it — a disabled timer and a deferring
timer both look like silence. Per #2096, the failing verdict is exercised
directly here, not just described.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import HealthConfig
from coord.health.checks import unit_enablement as ue
from coord.health.models import Checkout, HealthContext, Severity

NOW = 1_800_000_000.0


def make_ctx(tmp_path: Path, **kwargs) -> HealthContext:
    thresholds = kwargs.pop("thresholds", None) or HealthConfig()
    home = kwargs.pop("home", tmp_path)
    return HealthContext(
        thresholds=thresholds,
        home=home,
        coord_dir=kwargs.pop("coord_dir", home / ".coord"),
        now=kwargs.pop("now", NOW),
        checkouts=kwargs.pop("checkouts", ()),
        config=kwargs.pop("config", None),
        allow_network=kwargs.pop("allow_network", True),
    )


class _FakeProc:
    def __init__(self, stdout: str = "", stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(states: dict[str, str]):
    """A `subprocess.run`-shaped fake: `systemctl --user is-enabled <unit>`
    returns `states[unit]` on stdout, mirroring what real systemctl does —
    the state on stdout regardless of exit code."""

    def run(cmd, **kwargs):
        unit = cmd[-1]
        return _FakeProc(stdout=states.get(unit, "not-found") + "\n")

    return run


def _install(tmp_path: Path, *names: str) -> Path:
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (installed_dir / name).write_text("[Unit]\n")
    return installed_dir


# ── _is_enabled ──────────────────────────────────────────────────────────


def test_is_enabled_reads_stdout_regardless_of_returncode() -> None:
    state, error = ue._is_enabled(
        "coord-release-propagate.timer",
        runner=_fake_runner({"coord-release-propagate.timer": "disabled"}),
    )
    assert state == "disabled"
    assert error is None


def test_is_enabled_reports_missing_systemctl() -> None:
    def run(cmd, **kwargs):
        raise FileNotFoundError("systemctl")

    state, error = ue._is_enabled("coord-agent.service", runner=run)
    assert state is None
    assert "no systemd" in error


# ── probe_unit_enablement ────────────────────────────────────────────────


def test_no_manifest_unit_installed_is_ok(tmp_path) -> None:
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.OK
    assert "no manifest-listed unit installed" in results[0].headroom


def test_uninstalled_manifest_unit_is_silently_skipped(tmp_path, monkeypatch) -> None:
    """A worker box that never installed the daemon-only lanes is not a
    fault — same "don't guess topology" boundary as `unit_drift`."""
    _install(tmp_path, "coord-agent.service")
    monkeypatch.setattr(
        ue, "_is_enabled", lambda name, **kw: ("enabled", None)
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert [r.subject for r in results] == ["coord-agent.service"]


def test_installed_and_enabled_unit_is_ok(tmp_path, monkeypatch) -> None:
    _install(tmp_path, "coord-release-propagate.timer")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert len(results) == 1
    r = results[0]
    assert r.subject == "coord-release-propagate.timer"
    assert r.severity is Severity.OK
    assert r.headroom == "enabled"


def test_installed_but_disabled_unit_fails(tmp_path, monkeypatch) -> None:
    """The exact #2098 incident, reproduced: a unit that is `cp`'d onto a
    host and byte-identical to the release, but never `enable --now`'d.
    This is the failing verdict #2096 requires be exercised, not just
    described — assert it actually fires."""
    _install(tmp_path, "coord-release-propagate.timer")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("disabled", None))
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert len(results) == 1
    r = results[0]
    assert r.subject == "coord-release-propagate.timer"
    assert r.severity is Severity.WARN
    assert "disabled" in r.headroom
    assert "enable --now coord-release-propagate.timer" in r.detail


def test_installed_but_masked_unit_fails(tmp_path, monkeypatch) -> None:
    _install(tmp_path, "coord-db-backup.timer")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("masked", None))
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert results[0].severity is Severity.WARN


def test_is_enabled_error_is_unknown_not_ok_or_warn(tmp_path, monkeypatch) -> None:
    _install(tmp_path, "coord-serve.service")
    monkeypatch.setattr(
        ue, "_is_enabled", lambda name, **kw: (None, "systemctl not found (no systemd on this host)")
    )
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.UNKNOWN
    assert results[0].error


def test_multiple_installed_units_report_independently(tmp_path, monkeypatch) -> None:
    _install(tmp_path, "coord-serve.service", "coord-notify.timer", "coord-db-backup.timer")
    states = {
        "coord-serve.service": "enabled",
        "coord-notify.timer": "disabled",
        "coord-db-backup.timer": "enabled",
    }
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: (states[name], None))
    results = ue.probe_unit_enablement(make_ctx(tmp_path))
    by_subject = {r.subject: r for r in results}
    assert by_subject["coord-serve.service"].severity is Severity.OK
    assert by_subject["coord-notify.timer"].severity is Severity.WARN
    assert by_subject["coord-db-backup.timer"].severity is Severity.OK


def test_resolve_systemd_user_dir_honors_configured_path(tmp_path, monkeypatch) -> None:
    configured = tmp_path / "custom" / "systemd"
    configured.mkdir(parents=True)
    (configured / "coord-serve.service").write_text("[Unit]\n")
    monkeypatch.setattr(ue, "_is_enabled", lambda name, **kw: ("enabled", None))
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(systemd_user_dir=str(configured)))
    results = ue.probe_unit_enablement(ctx)
    assert [r.subject for r in results] == ["coord-serve.service"]
