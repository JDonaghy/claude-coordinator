"""Unit tests for the `unit_drift` machine-scope health check (#1831).

Mirrors `tests/test_health_deploy_lane_facts.py`'s structure — see that
module's docstring for the "measure locally, judge centrally" pattern this
check follows. Covers both halves of #1831's acceptance criteria: content
drift (installed != deploy/) and PATH shadow risk (an editable checkout
ahead of the release entry point).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coord.config import HealthConfig
from coord.health.checks import unit_drift as ud
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


UNIT_TEXT = (
    "[Service]\n"
    "Type=simple\n"
    "Environment=PATH=%h/.cargo/bin:%h/.local/bin:/usr/bin:/bin\n"
    "ExecStart=%h/.coord-venv/bin/coord serve\n"
)


def _make_deploy_dir(tmp_path: Path, units: dict[str, str]) -> Path:
    deploy_dir = tmp_path / "checkout" / "deploy"
    deploy_dir.mkdir(parents=True)
    for name, text in units.items():
        (deploy_dir / name).write_text(text)
    return deploy_dir


# ── resolve_deploy_dir / resolve_systemd_user_dir ──────────────────────────


def test_resolve_deploy_dir_is_none_with_no_checkout(tmp_path) -> None:
    assert ud.resolve_deploy_dir(make_ctx(tmp_path)) is None


def test_resolve_deploy_dir_finds_first_checkout_with_one(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    deploy = checkout / "deploy"
    deploy.mkdir(parents=True)
    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    assert ud.resolve_deploy_dir(ctx) == deploy


def test_resolve_deploy_dir_prefers_configured_path(tmp_path) -> None:
    configured = tmp_path / "elsewhere" / "deploy"
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(deploy_dir=str(configured)))
    assert ud.resolve_deploy_dir(ctx) == configured


def test_resolve_systemd_user_dir_default(tmp_path) -> None:
    ctx = make_ctx(tmp_path)
    assert ud.resolve_systemd_user_dir(ctx) == tmp_path / ".config" / "systemd" / "user"


def test_resolve_systemd_user_dir_prefers_configured_path(tmp_path) -> None:
    configured = tmp_path / "custom" / "systemd"
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(systemd_user_dir=str(configured)))
    assert ud.resolve_systemd_user_dir(ctx) == configured


# ── probe_unit_drift ─────────────────────────────────────────────────────


def test_no_deploy_dir_is_ok_not_unknown(tmp_path) -> None:
    results = ud.probe_unit_drift(make_ctx(tmp_path))
    assert len(results) == 1
    assert results[0].severity is Severity.OK
    assert "no deploy/ checkout" in results[0].headroom


def test_unit_not_installed_is_ok(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    _make_deploy_dir(tmp_path, {"coord-serve.service": UNIT_TEXT})
    (checkout / "deploy").mkdir(parents=True, exist_ok=True)
    (checkout / "deploy" / "coord-serve.service").write_text(UNIT_TEXT)
    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.subject == "coord-serve.service"
    assert r.severity is Severity.OK
    assert r.headroom == "not installed on this machine"
    assert r.values["installed"] is False


def test_matching_unit_is_ok_and_silent(tmp_path) -> None:
    """The acceptance-criteria "matching unit -> silent" half."""
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "coord-serve.service").write_text(UNIT_TEXT)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    (installed_dir / "coord-serve.service").write_text(UNIT_TEXT)

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.severity is Severity.OK
    assert r.headroom == "matches deploy/"
    assert r.values["matches"] is True


def test_stale_unit_is_warn_and_reports_mtime_and_diff(tmp_path) -> None:
    """The acceptance-criteria "stale unit -> reported" half."""
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "coord-serve.service").write_text(
        UNIT_TEXT + "ExtraLineInDeploy=1\n"
    )
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    installed = installed_dir / "coord-serve.service"
    installed.write_text(UNIT_TEXT)
    stale_mtime = NOW - (21 * 24 * 3600)  # three weeks stale, matches #1831
    os.utime(installed, (stale_mtime, stale_mtime))

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.severity is Severity.WARN
    assert "stale" in r.headroom
    assert "line" in r.headroom
    assert r.values["installed_mtime"] == pytest.approx(stale_mtime)
    assert r.values["diff_lines"] >= 1
    assert "cp" in r.detail and "restart" in r.detail


def test_unreadable_installed_unit_is_unknown(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "coord-serve.service").write_text(UNIT_TEXT)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    installed = installed_dir / "coord-serve.service"
    installed.mkdir()  # a directory, not a file -> read_text() raises

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    assert results[0].severity is Severity.UNKNOWN
    assert results[0].error


def test_multiple_units_each_get_their_own_result(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "coord-serve.service").write_text(UNIT_TEXT)
    (checkout / "deploy" / "coord-agent.service").write_text(UNIT_TEXT)
    (checkout / "deploy" / "coord-notify.timer").write_text("[Timer]\n")

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    subjects = sorted(r.subject for r in results)
    assert subjects == [
        "coord-agent.service",
        "coord-notify.timer",
        "coord-serve.service",
    ]
    assert all(r.severity is Severity.OK for r in results)


# ── find_path_shadow ─────────────────────────────────────────────────────


def test_no_path_line_has_no_shadow() -> None:
    assert ud.find_path_shadow("[Service]\nExecStart=/bin/true\n") is None


def test_release_first_has_no_shadow() -> None:
    text = (
        "Environment=PATH=%h/.cargo/bin:%h/.local/bin:"
        "%h/src/claude-coordinator/.venv/bin:/usr/bin\n"
    )
    assert ud.find_path_shadow(text) is None


def test_editable_venv_before_local_bin_is_a_shadow() -> None:
    """The exact #1831 dellserver shape: the repo venv ahead of ~/.local/bin."""
    text = "Environment=PATH=%h/src/claude-coordinator/.venv/bin:%h/.local/bin:/usr/bin\n"
    assert ud.find_path_shadow(text) == "%h/src/claude-coordinator/.venv/bin"


def test_editable_venv_before_coord_venv_bin_is_a_shadow() -> None:
    text = "Environment=PATH=%h/src/claude-coordinator/.venv/bin:%h/.coord-venv/bin:/usr/bin\n"
    assert ud.find_path_shadow(text) == "%h/src/claude-coordinator/.venv/bin"


def test_coord_venv_and_coord_cli_venv_are_not_mistaken_for_a_dev_venv() -> None:
    """`.coord-venv`/`.coord-cli-venv` are the SANCTIONED venvs — a probe
    that flagged them as `.venv/bin` would make every stock install CRIT."""
    text = "Environment=PATH=%h/.coord-cli-venv/bin:%h/.coord-venv/bin:%h/.local/bin\n"
    assert ud.find_path_shadow(text) is None


def test_only_the_last_environment_path_line_is_read() -> None:
    text = (
        "Environment=PATH=%h/.local/bin:/usr/bin\n"
        "Environment=PATH=%h/src/claude-coordinator/.venv/bin:%h/.local/bin\n"
    )
    assert ud.find_path_shadow(text) == "%h/src/claude-coordinator/.venv/bin"


def test_shadow_risk_wins_over_content_match(tmp_path) -> None:
    """Even a unit whose content is byte-identical to deploy/ must CRIT if
    deploy/ itself regresses the PATH ordering — #1831's exact shape: the
    v0.4.105 cut of coord-serve.service was internally consistent and still
    wrong."""
    text = "Environment=PATH=%h/src/claude-coordinator/.venv/bin:%h/.local/bin:/usr/bin\n"
    checkout = tmp_path / "src" / "claude-coordinator"
    (checkout / "deploy").mkdir(parents=True)
    (checkout / "deploy" / "coord-serve.service").write_text(text)
    installed_dir = tmp_path / ".config" / "systemd" / "user"
    installed_dir.mkdir(parents=True)
    (installed_dir / "coord-serve.service").write_text(text)

    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),))
    results = ud.probe_unit_drift(ctx)
    assert len(results) == 1
    r = results[0]
    assert r.severity is Severity.CRIT
    assert r.values["matches"] is True
    assert r.values["shadow_entry"] == "%h/src/claude-coordinator/.venv/bin"
