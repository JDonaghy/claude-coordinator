"""Tests for coord pause / unpause CLI commands (machine routing-pause).

Regression test for the TUI --config injection bug: the coord-tui calls
`coord pause --config <path> <machine>` / `coord unpause --config <path>
<machine>` (injecting --config after the subcommand name for every non-flag
subcommand).  Until the @_CONFIG_OPTION decorator was added to `pause` and
`unpause`, Click rejected --config as an unknown option and the commands
silently failed from the TUI.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME to a temp dir so the pause-state file is isolated."""
    monkeypatch.setenv("HOME", str(tmp_path))
    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir()
    return tmp_path


def test_pause_accepts_config_option(tmp_home: Path, tmp_path: Path) -> None:
    """coord pause --config <path> <machine> must not fail with 'No such option'."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos: []\nmachines: []\n")

    runner = CliRunner()
    result = runner.invoke(main, ["pause", "--config", str(cfg), "testmachine"])
    assert result.exit_code == 0, f"exit {result.exit_code}: {result.output}"
    assert "paused" in result.output

    # The state file should reflect the pause.
    state_file = tmp_home / ".coord" / "paused_machines.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert "testmachine" in data["paused"]


def test_unpause_accepts_config_option(tmp_home: Path, tmp_path: Path) -> None:
    """coord unpause --config <path> <machine> must not fail with 'No such option'."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos: []\nmachines: []\n")

    # Pre-populate the paused state.
    state_file = tmp_home / ".coord" / "paused_machines.json"
    state_file.write_text(json.dumps({"paused": ["testmachine"]}))

    runner = CliRunner()
    result = runner.invoke(main, ["unpause", "--config", str(cfg), "testmachine"])
    assert result.exit_code == 0, f"exit {result.exit_code}: {result.output}"
    assert "resumed" in result.output

    data = json.loads(state_file.read_text())
    assert "testmachine" not in data["paused"]


def test_pause_unpause_roundtrip(tmp_home: Path, tmp_path: Path) -> None:
    """Pause then unpause a machine — state file ends up empty."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos: []\nmachines: []\n")

    runner = CliRunner()
    result = runner.invoke(main, ["pause", "--config", str(cfg), "m1"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["unpause", "--config", str(cfg), "m1"])
    assert result.exit_code == 0

    state_file = tmp_home / ".coord" / "paused_machines.json"
    data = json.loads(state_file.read_text())
    assert data["paused"] == []


def test_pause_already_paused_is_idempotent(tmp_home: Path, tmp_path: Path) -> None:
    """Pausing an already-paused machine returns exit 0 with 'already paused'."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos: []\nmachines: []\n")

    runner = CliRunner()
    runner.invoke(main, ["pause", "--config", str(cfg), "m1"])
    result = runner.invoke(main, ["pause", "--config", str(cfg), "m1"])
    assert result.exit_code == 0
    assert "already paused" in result.output


def test_unpause_not_paused_is_idempotent(tmp_home: Path, tmp_path: Path) -> None:
    """Unpausing a non-paused machine returns exit 0 with 'not paused'."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos: []\nmachines: []\n")

    runner = CliRunner()
    result = runner.invoke(main, ["unpause", "--config", str(cfg), "m1"])
    assert result.exit_code == 0
    assert "not paused" in result.output
