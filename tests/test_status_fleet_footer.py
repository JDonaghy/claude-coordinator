"""#1631 (H-4): the always-visible ``coord status`` footer.

Covers the acceptance bar:

1. ``coord.health.aggregate`` counts/renders OK, WARN, and CRIT correctly,
   consuming only each unit's already-decided ``severity`` string (never
   re-deriving from raw numbers).
2. A machine that never reported health renders as ``unknown`` — visually
   distinct from ``OK`` — rather than silently green (#1485's failure mode).
3. ``coord status`` itself prints the footer, unconditionally, for OK, WARN,
   and CRIT fleets.
"""

from __future__ import annotations

import time

import coord.network as network_mod
from click.testing import CliRunner

from coord.commands.status import status as status_cmd
from coord.health.aggregate import (
    FleetHealthSummary,
    local_fleet_health_block,
    render_fleet_footer,
    summarize_fleet_health,
)
from coord.health.models import Severity
from coord.network import MachineStatus
from coord.state import save_machine_health

# ── unit: summarize_fleet_health / render_fleet_footer ──────────────────────


def _health_report(severity: str) -> dict:
    return {
        "schema": 1, "checked_at": time.time(), "severity": severity,
        "counts": {}, "skipped": [],
        "results": [{
            "key": "disk:/", "check_id": "disk", "scope": "machine", "subject": "/",
            "title": "disk", "label": "disk /", "severity": severity,
            "headroom": f"({severity})", "threshold": "", "detail": "",
            "trend": None, "values": {}, "error": None,
        }],
    }


def test_summarize_all_ok_renders_ok_not_silence() -> None:
    block = {
        "machine_health": [
            {"machine": "laptop", "severity": "ok"},
            {"machine": "server", "severity": "ok"},
        ],
        "fleet_checks": [],
    }
    summary = summarize_fleet_health(block)
    assert summary.worst is Severity.OK
    assert summary.counts == {"ok": 2, "warn": 0, "crit": 0, "unknown": 0}
    assert render_fleet_footer(summary) == "FLEET: OK  (coord health for detail)"


def test_summarize_warn() -> None:
    block = {
        "machine_health": [
            {"machine": "laptop", "severity": "warn"},
            {"machine": "server", "severity": "ok"},
        ],
        "fleet_checks": [],
    }
    summary = summarize_fleet_health(block)
    assert summary.worst is Severity.WARN
    assert render_fleet_footer(summary) == "FLEET: WARN 1  (coord health for detail)"


def test_summarize_crit_and_warn_orders_worst_last() -> None:
    block = {
        "machine_health": [
            {"machine": "laptop", "severity": "warn"},
            {"machine": "server", "severity": "crit"},
        ],
        "fleet_checks": [
            {"key": "fleet_board_latency", "severity": "warn"},
        ],
    }
    summary = summarize_fleet_health(block)
    assert summary.worst is Severity.CRIT
    assert summary.counts == {"ok": 0, "warn": 2, "crit": 1, "unknown": 0}
    # Ascending order, worst last — mirrors the issue's own example.
    assert render_fleet_footer(summary) == "FLEET: WARN 2, CRIT 1  (coord health for detail)"


def test_unknown_machine_is_not_ok() -> None:
    """A machine with no health data at all must never render as OK — the
    #1485 failure mode this whole engine exists to close."""
    block = {
        "machine_health": [{"machine": "ghost", "severity": "unknown"}],
        "fleet_checks": [],
    }
    summary = summarize_fleet_health(block)
    assert summary.worst is Severity.UNKNOWN
    footer = render_fleet_footer(summary)
    assert footer != "FLEET: OK  (coord health for detail)"
    assert "?" in footer


def test_summarize_empty_block_degrades_to_ok() -> None:
    """No fleet_health key at all (daemon predates #1630) — no units, not a
    crash, and not a fabricated warning."""
    summary = summarize_fleet_health(None)
    assert summary == FleetHealthSummary(worst=Severity.OK, counts={"ok": 0, "warn": 0, "crit": 0, "unknown": 0}, unit_count=0)
    assert render_fleet_footer(summary) == "FLEET: OK  (coord health for detail)"


def test_renderer_never_reads_raw_values() -> None:
    """A unit with WARN-looking raw numbers but an OK severity (a probe's own
    call, however unlikely) must be counted as OK — the renderer trusts
    `severity`, never `values`."""
    block = {
        "machine_health": [{
            "machine": "laptop", "severity": "ok",
            "results": [{"severity": "ok", "values": {"free_pct": 1.0}}],
        }],
        "fleet_checks": [],
    }
    summary = summarize_fleet_health(block)
    assert summary.worst is Severity.OK
    assert summary.counts["ok"] == 1


# ── unit: local_fleet_health_block (host mode, no board_service) ────────────


def test_local_fleet_health_block_reads_saved_severities(coord_db) -> None:
    now = time.time()
    save_machine_health(
        "laptop", state="online", reason="", latency_ms=5.0,
        health=_health_report("crit"), received_at=now,
    )
    block = local_fleet_health_block(["laptop", "server"])
    by_machine = {m["machine"]: m for m in block["machine_health"]}
    assert by_machine["laptop"]["severity"] == "crit"
    # Never polled at all -> unknown, not silently dropped or OK.
    assert by_machine["server"]["severity"] == "unknown"
    assert block["fleet_checks"] == []


# ── integration: `coord status` prints the footer ───────────────────────────


def _run_status(valid_config_path, monkeypatch) -> str:
    def _fake_check_all(machines, timeout=3.0, **kw):
        return [MachineStatus(machine=m, state="online", latency_ms=1.0) for m in machines]

    monkeypatch.setattr(network_mod, "check_all", _fake_check_all)
    from coord.network import StatusResult
    monkeypatch.setattr(
        network_mod, "fetch_status",
        lambda *a, **k: StatusResult(data={"active": [], "completed": []}),
    )

    runner = CliRunner()
    result = runner.invoke(
        status_cmd,
        ["--config", str(valid_config_path), "--no-reconcile"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_status_footer_ok(valid_config_path, monkeypatch, coord_db) -> None:
    now = time.time()
    for name in ("laptop", "server"):
        save_machine_health(
            name, state="online", reason="", latency_ms=5.0,
            health=_health_report("ok"), received_at=now,
        )
    output = _run_status(valid_config_path, monkeypatch)
    assert "FLEET: OK  (coord health for detail)" in output, output


def test_status_footer_warn(valid_config_path, monkeypatch, coord_db) -> None:
    now = time.time()
    save_machine_health(
        "laptop", state="online", reason="", latency_ms=5.0,
        health=_health_report("warn"), received_at=now,
    )
    save_machine_health(
        "server", state="online", reason="", latency_ms=5.0,
        health=_health_report("ok"), received_at=now,
    )
    output = _run_status(valid_config_path, monkeypatch)
    assert "FLEET: WARN 1  (coord health for detail)" in output, output


def test_status_footer_crit(valid_config_path, monkeypatch, coord_db) -> None:
    now = time.time()
    save_machine_health(
        "laptop", state="online", reason="", latency_ms=5.0,
        health=_health_report("crit"), received_at=now,
    )
    save_machine_health(
        "server", state="online", reason="", latency_ms=5.0,
        health=_health_report("warn"), received_at=now,
    )
    output = _run_status(valid_config_path, monkeypatch)
    assert "FLEET: WARN 1, CRIT 1  (coord health for detail)" in output, output


def test_status_footer_unknown_machine_distinct_from_ok(
    valid_config_path, monkeypatch, coord_db
) -> None:
    """Neither machine has ever reported health — the footer must say so
    (``?``), not render the reassuring ``OK`` line."""
    output = _run_status(valid_config_path, monkeypatch)
    assert "FLEET: OK  (coord health for detail)" not in output, output
    assert "FLEET: ? 2  (coord health for detail)" in output, output
