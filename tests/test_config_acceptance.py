"""Tests for the `acceptance:` block in coordinator.yml (#944, the oracle
loop runner + tui-tuidriver driver + sealing v1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import AcceptanceConfig, AcceptanceDriverConfig, ConfigError, load


BASE = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
"""


def test_acceptance_absent_defaults_to_empty(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.acceptance == AcceptanceConfig()
    assert cfg.acceptance.driver_for("coord-tui") is None


def test_acceptance_parses_driver(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test --test acceptance -- --format json"
      mock: "*.screen"
      capability: rust
"""
    )
    cfg = load(p)
    driver = cfg.acceptance.driver_for("coord-tui")
    assert driver == AcceptanceDriverConfig(
        kind="tui-tuidriver",
        run="cargo test --test acceptance -- --format json",
        mock="*.screen",
        capability="rust",
    )


def test_acceptance_driver_mock_and_capability_optional(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test --test acceptance"
"""
    )
    cfg = load(p)
    driver = cfg.acceptance.driver_for("coord-tui")
    assert driver.mock == ""
    assert driver.capability == ""


def test_acceptance_unconfigured_repo_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
"""
    )
    cfg = load(p)
    assert cfg.acceptance.driver_for("some-other-repo") is None


def test_acceptance_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "acceptance: [1, 2]\n")
    with pytest.raises(ConfigError, match="'acceptance' must be a mapping"):
        load(p)


def test_acceptance_drivers_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "acceptance:\n  drivers: [1, 2]\n")
    with pytest.raises(ConfigError, match="acceptance.drivers must be a mapping"):
        load(p)


def test_acceptance_driver_missing_kind_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      run: "cargo test"
"""
    )
    with pytest.raises(ConfigError, match="kind is required"):
        load(p)


def test_acceptance_driver_missing_run_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
"""
    )
    with pytest.raises(ConfigError, match="run is required"):
        load(p)


def test_acceptance_driver_entry_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui: "not-a-mapping"
"""
    )
    with pytest.raises(ConfigError, match="must be a mapping"):
        load(p)


def test_acceptance_driver_mock_non_string_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
      mock: [1, 2]
"""
    )
    with pytest.raises(ConfigError, match="mock must be a string"):
        load(p)


def test_acceptance_driver_capability_non_string_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test"
      capability: [1, 2]
"""
    )
    with pytest.raises(ConfigError, match="capability must be a string"):
        load(p)
