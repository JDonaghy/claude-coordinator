"""Unit tests for the per-machine ``cli_venv``/``tui_binary`` checks (#1806).

These two machine-scope checks replace the daemon-host-only ``os.stat``
gathering :mod:`coord.health.fleet_snapshot` used to do for the CLI-venv
version and the tui/ binary-vs-source comparison — both facts about
whichever machine the operator actually put them on, not about the daemon
host. See ``coord/health/checks/deploy_lane_facts.py``'s module docstring
for the full story, and ``tests/test_fleet_health_probes.py`` for the
fleet-scope aggregation this feeds.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from coord.config import HealthConfig
from coord.health.checks import agent_install, deploy_lane_facts as dlf
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


def _pip_show(monkeypatch, stdout: str, returncode: int = 0) -> None:
    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(agent_install.subprocess, "run", _run)


PYPI_SHOW = (
    "Name: claude-coordinator\n"
    "Version: 0.4.91\n"
    "Location: /home/x/.coord-cli-venv/lib/python3.12/site-packages\n"
)
EDITABLE_SHOW = (
    "Name: claude-coordinator\n"
    "Version: 0.4.92\n"
    "Location: /home/x/.coord-cli-venv/lib/python3.12/site-packages\n"
    "Editable project location: /home/x/src/claude-coordinator\n"
)


# ── cli_venv ─────────────────────────────────────────────────────────────────


def test_cli_venv_absent_is_ok_not_unknown(tmp_path) -> None:
    """The common case — most machines never had this venv created — must
    not read as a fault."""
    result = dlf.probe_cli_venv(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "not present on this machine"
    assert result.values == {
        "python": str(tmp_path / ".coord-cli-venv" / "bin" / "python3"),
        "present": False,
        "version": None,
    }


def test_cli_venv_present_pypi_install_is_ok(tmp_path, monkeypatch) -> None:
    python = tmp_path / ".coord-cli-venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.touch()
    _pip_show(monkeypatch, PYPI_SHOW)
    result = dlf.probe_cli_venv(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "pypi 0.4.91"
    assert result.values["present"] is True
    assert result.values["version"] == "0.4.91"
    assert result.values["editable"] is False


def test_cli_venv_present_editable_is_still_ok_but_flagged(tmp_path, monkeypatch) -> None:
    """Unlike the agent's own ``~/.coord-venv`` (CRIT if editable), this
    check only reports the fact — the fleet lane check judges skew, not
    install hygiene, for this lane."""
    python = tmp_path / ".coord-cli-venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.touch()
    _pip_show(monkeypatch, EDITABLE_SHOW)
    result = dlf.probe_cli_venv(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.values["version"] == "0.4.92"
    assert result.values["editable"] is True


def test_cli_venv_present_but_not_a_real_install_is_unknown(tmp_path, monkeypatch) -> None:
    python = tmp_path / ".coord-cli-venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.touch()
    _pip_show(monkeypatch, "", returncode=1)
    result = dlf.probe_cli_venv(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert result.values["version"] is None


def test_cli_venv_pip_failure_is_unknown(tmp_path, monkeypatch) -> None:
    python = tmp_path / ".coord-cli-venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.touch()

    def _run(cmd, **kwargs):
        raise OSError("no such interpreter")

    monkeypatch.setattr(agent_install.subprocess, "run", _run)
    result = dlf.probe_cli_venv(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert "no such interpreter" in (result.error or "")


def test_resolve_cli_venv_python_prefers_the_configured_path(tmp_path) -> None:
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(cli_venv_python="~/custom/bin/python")
    )
    assert dlf.resolve_cli_venv_python(ctx) == tmp_path / "custom" / "bin" / "python"


# ── tui_binary ───────────────────────────────────────────────────────────────


def test_tui_binary_absent_is_ok_not_unknown(tmp_path) -> None:
    result = dlf.probe_tui_binary(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "not present on this machine"
    assert result.values["present"] is False


def test_tui_binary_present_no_source_tree_is_ok(tmp_path) -> None:
    binary = tmp_path / ".local" / "bin" / "coord-tui"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    result = dlf.probe_tui_binary(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert "not found to compare" in result.headroom
    assert result.values["present"] is True
    assert "source_mtime" not in result.values


def test_tui_binary_newer_than_source_is_ok(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    src = checkout / "tui" / "src"
    src.mkdir(parents=True)
    old = src / "main.rs"
    old.write_text("")
    os.utime(old, (NOW - 3600, NOW - 3600))

    binary = tmp_path / ".local" / "bin" / "coord-tui"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    os.utime(binary, (NOW, NOW))

    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),)
    )
    result = dlf.probe_tui_binary(ctx)
    assert result.severity is Severity.OK
    assert result.headroom == "up to date with tui/ source"
    assert result.values["source_mtime"] == pytest.approx(NOW - 3600)


def test_tui_binary_older_than_source_is_warn(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    src = checkout / "tui" / "src"
    src.mkdir(parents=True)
    new = src / "main.rs"
    new.write_text("")
    os.utime(new, (NOW, NOW))

    binary = tmp_path / ".local" / "bin" / "coord-tui"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    os.utime(binary, (NOW - 9000, NOW - 9000))  # 2.5h before the source

    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),)
    )
    result = dlf.probe_tui_binary(ctx)
    assert result.severity is Severity.WARN
    assert "2.5h older" in result.headroom
    assert "rebuild" in result.detail


def test_tui_binary_exactly_equal_mtimes_is_ok_not_warn(tmp_path) -> None:
    checkout = tmp_path / "src" / "claude-coordinator"
    src = checkout / "tui" / "src"
    src.mkdir(parents=True)
    rs = src / "main.rs"
    rs.write_text("")
    os.utime(rs, (NOW, NOW))

    binary = tmp_path / ".local" / "bin" / "coord-tui"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    os.utime(binary, (NOW, NOW))

    ctx = make_ctx(
        tmp_path, checkouts=(Checkout(name="coordinator", path=checkout),)
    )
    assert dlf.probe_tui_binary(ctx).severity is Severity.OK


def test_resolve_tui_binary_path_prefers_the_configured_path(tmp_path) -> None:
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(tui_binary_path="~/custom/coord-tui")
    )
    assert dlf.resolve_tui_binary_path(ctx) == tmp_path / "custom" / "coord-tui"


def test_resolve_tui_source_dir_prefers_the_configured_path(tmp_path) -> None:
    configured = tmp_path / "elsewhere" / "src"
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(tui_source_dir=str(configured))
    )
    assert dlf.resolve_tui_source_dir(ctx) == configured


def test_resolve_tui_source_dir_falls_back_to_the_first_checkout_with_one(
    tmp_path,
) -> None:
    checkout_a = tmp_path / "a"
    checkout_a.mkdir()
    checkout_b = tmp_path / "b"
    (checkout_b / "tui" / "src").mkdir(parents=True)
    ctx = make_ctx(
        tmp_path,
        checkouts=(
            Checkout(name="a", path=checkout_a),
            Checkout(name="b", path=checkout_b),
        ),
    )
    assert dlf.resolve_tui_source_dir(ctx) == checkout_b / "tui" / "src"


def test_resolve_tui_source_dir_is_none_when_no_checkout_has_one(tmp_path) -> None:
    ctx = make_ctx(tmp_path, checkouts=())
    assert dlf.resolve_tui_source_dir(ctx) is None


# ── _newest_rust_source_mtime (moved from coord.health.fleet_snapshot) ───────


def test_newest_rust_source_walk_finds_the_newest_rs_file(tmp_path: Path) -> None:
    src = tmp_path / "tui" / "src"
    (src / "widgets").mkdir(parents=True)
    old = src / "main.rs"
    old.write_text("fn main() {}")
    os.utime(old, (NOW - 10_000, NOW - 10_000))
    new = src / "widgets" / "board.rs"
    new.write_text("pub struct Board;")
    os.utime(new, (NOW, NOW))

    assert dlf._newest_rust_source_mtime(src) == pytest.approx(NOW)


def test_newest_rust_source_walk_skips_target_and_hidden_dirs(tmp_path: Path) -> None:
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

    assert dlf._newest_rust_source_mtime(src) == pytest.approx(NOW - 10_000)


def test_newest_rust_source_walk_on_a_missing_or_empty_dir_is_none(
    tmp_path: Path,
) -> None:
    assert dlf._newest_rust_source_mtime(tmp_path / "nope") is None
    (tmp_path / "empty").mkdir()
    assert dlf._newest_rust_source_mtime(tmp_path / "empty") is None
