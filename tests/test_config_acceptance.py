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


# --- #1125: in-repo path routing -------------------------------------------

ROUTED_CONFIG = """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{ms}"
          mock: "*.out"
          capability: python
        - match: "tui/**"
          kind: tui-tuidriver
          run: "cargo test --test acceptance -- --format json"
          mock: "*.screen"
          capability: rust
"""


def _routed_cfg(tmp_path: Path):
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE.replace("coord-tui", "claude-coordinator") + ROUTED_CONFIG)
    return load(p)


def test_driver_for_routes_python_path_to_cli_pytest(tmp_path: Path) -> None:
    cfg = _routed_cfg(tmp_path)
    driver = cfg.acceptance.driver_for("claude-coordinator", "coord/acceptance.py")
    assert driver.kind == "cli-pytest"
    assert driver.match == "coord/**"
    assert driver.mock == "*.out"
    assert driver.capability == "python"


def test_driver_for_routes_rust_path_to_tui_tuidriver(tmp_path: Path) -> None:
    cfg = _routed_cfg(tmp_path)
    driver = cfg.acceptance.driver_for("claude-coordinator", "tui/src/app.rs")
    assert driver.kind == "tui-tuidriver"
    assert driver.match == "tui/**"
    assert driver.mock == "*.screen"
    assert driver.capability == "rust"


def test_driver_for_routes_first_match_wins(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "**"
          kind: cli-pytest
          run: "pytest ."
        - match: "tui/**"
          kind: tui-tuidriver
          run: "cargo test"
"""
    )
    cfg = load(p)
    driver = cfg.acceptance.driver_for("claude-coordinator", "tui/src/app.rs")
    # The catch-all "**" is listed first, so it wins even though "tui/**"
    # would also match — first-match, not most-specific-match.
    assert driver.kind == "cli-pytest"


def test_driver_for_routes_no_match_returns_none(tmp_path: Path) -> None:
    cfg = _routed_cfg(tmp_path)
    assert cfg.acceptance.driver_for("claude-coordinator", "docs/README.md") is None


def test_driver_for_routes_without_path_returns_none(tmp_path: Path) -> None:
    cfg = _routed_cfg(tmp_path)
    # No path given -> can't select a route; not a guess.
    assert cfg.acceptance.driver_for("claude-coordinator") is None
    assert cfg.acceptance.driver_for("claude-coordinator", None) is None


def test_driver_for_no_routes_falls_back_to_flat_form_with_path(tmp_path: Path) -> None:
    # Back-compat: an existing flat (non-routed) config ignores `path`
    # entirely and returns its one driver, exactly like before #1125.
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: "cargo test --test acceptance -- --format json"
"""
    )
    cfg = load(p)
    driver = cfg.acceptance.driver_for("coord-tui", "anything/at/all.py")
    assert driver.kind == "tui-tuidriver"


def test_acceptance_routes_empty_list_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes: []
"""
    )
    with pytest.raises(ConfigError, match="routes must be a non-empty list"):
        load(p)


def test_acceptance_route_missing_match_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - kind: cli-pytest
          run: "pytest ."
"""
    )
    with pytest.raises(ConfigError, match=r"routes\[0\]\.match is required"):
        load(p)


def test_acceptance_route_missing_kind_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "coord/**"
          run: "pytest ."
"""
    )
    with pytest.raises(ConfigError, match=r"routes\[0\]\.kind is required"):
        load(p)


def test_acceptance_route_missing_run_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - match: "coord/**"
          kind: cli-pytest
"""
    )
    with pytest.raises(ConfigError, match=r"routes\[0\]\.run is required"):
        load(p)


def test_acceptance_route_entry_not_a_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes:
        - "not-a-mapping"
"""
    )
    with pytest.raises(ConfigError, match=r"routes\[0\] must be a mapping"):
        load(p)


def test_acceptance_routes_not_a_list_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      routes: "not-a-list"
"""
    )
    with pytest.raises(ConfigError, match="routes must be a non-empty list"):
        load(p)


# ── has_driver (#1125 review finding 1) ──────────────────────────────────────
#
# Path-independent "does this repo participate in the oracle loop at all"
# predicate — Gate A / sealing / briefing-injection call sites must not
# silently flip from "yes" to "no" the moment a repo's driver becomes routed.


def test_has_driver_false_for_unconfigured_repo(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.acceptance.has_driver("coord-tui") is False


def test_has_driver_true_for_flat_driver(tmp_path: Path) -> None:
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
    assert cfg.acceptance.has_driver("coord-tui") is True


def test_has_driver_true_for_routed_driver_with_no_path(tmp_path: Path) -> None:
    # The whole point of #1125 review finding 1: driver_for(repo) with no
    # path returns None for a routed repo (by design — it can't pick a
    # route), but has_driver must still say True, since the repo plainly
    # DOES participate in the oracle loop.
    cfg = _routed_cfg(tmp_path)
    assert cfg.acceptance.driver_for("claude-coordinator") is None
    assert cfg.acceptance.has_driver("claude-coordinator") is True


# ── double-config footgun (#1125 review finding 5) ───────────────────────────


def test_acceptance_routes_and_flat_kind_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      kind: cli-pytest
      routes:
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{ms}"
"""
    )
    with pytest.raises(ConfigError, match="sets both 'routes' and flat field"):
        load(p)


def test_acceptance_routes_and_flat_run_raises(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE.replace("coord-tui", "claude-coordinator")
        + """\
acceptance:
  drivers:
    claude-coordinator:
      run: "pytest ."
      routes:
        - match: "coord/**"
          kind: cli-pytest
          run: "pytest tests/acceptance/{ms}"
"""
    )
    with pytest.raises(ConfigError, match="sets both 'routes' and flat field"):
        load(p)
