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

import httpx
import pytest
from click.testing import CliRunner

from coord import client as coord_client
from coord import machine_pause
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


# ── #1563: daemon-aware routing ─────────────────────────────────────────────
#
# `coord pause` used to store the pause set in a host-local JSON file that
# only the process that wrote it would ever read — harmless for `coord plan`/
# `coord assign` (client dispatches those in-process) but silently useless
# for the autonomous dispatcher, which runs *inside the daemon* and reads
# its own copy. These tests cover the unit-level fix in
# `coord.machine_pause`: when a board service is configured (thin client),
# `paused_set()`/`pause()`/`unpause()` route over HTTP instead of touching
# the local file at all. The full cross-process round trip (through a real
# daemon app) is covered separately in
# tests/test_serve.py::test_pause_on_thin_client_reaches_daemon_and_blocks_dispatch.


def _remote(monkeypatch, url: str = "http://daemon:7435") -> None:
    """Make `coord.board_service.resolve()` (and therefore machine_pause's
    daemon-aware functions) act as a thin client pointed at *url*."""
    monkeypatch.setattr(
        coord_client, "resolve_board_service",
        lambda *a, **k: coord_client.ServiceConfig(url=url),
    )


def test_paused_set_asks_the_daemon_when_remote(monkeypatch) -> None:
    _remote(monkeypatch)
    monkeypatch.setattr(
        coord_client, "fetch_paused_machines",
        lambda svc, **k: {"dellserver", "elitebook"},
    )
    assert machine_pause.paused_set() == {"dellserver", "elitebook"}


def test_paused_set_fails_soft_when_daemon_unreachable(monkeypatch) -> None:
    """The read side degrades to 'nothing is paused' on a transport error —
    consistent with this module's pre-existing local-file fail-soft
    contract (a network blip must not wedge the dispatcher)."""
    _remote(monkeypatch)

    def _raise(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(coord_client, "fetch_paused_machines", _raise)
    assert machine_pause.paused_set() == set()


def test_pause_routes_to_the_daemon_and_never_touches_the_local_file(
    monkeypatch, tmp_home: Path
) -> None:
    """The literal #1563 bug: verify `pause()` on a thin client goes over
    HTTP instead of writing the operator's own local copy."""
    _remote(monkeypatch)
    posted = {}

    def _fake_post_pause(svc, machine, action, **_k):
        posted["url"] = svc.url
        posted["machine"] = machine
        posted["action"] = action
        return {"paused": [machine], "changed": True}

    monkeypatch.setattr(coord_client, "post_pause", _fake_post_pause)

    assert machine_pause.pause("dellserver") is True
    assert posted == {
        "url": "http://daemon:7435", "machine": "dellserver", "action": "pause",
    }
    state_file = tmp_home / ".coord" / "paused_machines.json"
    assert not state_file.exists()


def test_unpause_routes_to_the_daemon(monkeypatch) -> None:
    _remote(monkeypatch)
    monkeypatch.setattr(
        coord_client, "post_pause",
        lambda svc, machine, action, **k: {"paused": [], "changed": True},
    )
    assert machine_pause.unpause("dellserver") is True


def test_pause_fails_loudly_when_daemon_unreachable(monkeypatch) -> None:
    """#1563: 'there is no configuration in which a thin-client pause fails
    loudly' — fixed by letting the transport error propagate instead of
    swallowing it like the read side does."""
    _remote(monkeypatch)

    def _raise(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(coord_client, "post_pause", _raise)
    with pytest.raises(httpx.ConnectError):
        machine_pause.pause("dellserver")


def test_pause_cli_fails_loudly_when_daemon_unreachable(
    monkeypatch, tmp_path: Path
) -> None:
    """`coord pause` on a thin client that can't reach the daemon must
    report an error and exit non-zero — never print 'paused: X'."""
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos: []\nmachines: []\n")
    _remote(monkeypatch)

    def _raise(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(coord_client, "post_pause", _raise)

    runner = CliRunner()
    result = runner.invoke(main, ["pause", "--config", str(cfg), "dellserver"])
    assert result.exit_code == 1
    assert "paused: dellserver" not in result.output
    assert "error" in result.output.lower()


def test_unpause_cli_fails_loudly_when_daemon_unreachable(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text("repos: []\nmachines: []\n")
    _remote(monkeypatch)

    def _raise(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(coord_client, "post_pause", _raise)

    runner = CliRunner()
    result = runner.invoke(main, ["unpause", "--config", str(cfg), "dellserver"])
    assert result.exit_code == 1
    assert "resumed: dellserver" not in result.output
    assert "error" in result.output.lower()
