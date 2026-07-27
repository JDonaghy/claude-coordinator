"""#1461: `coord status` must surface a usage-limit kill as a distinct
fleet-level condition — "N assignments failed: usage limit, resets HH:MM" —
rather than showing it as an indistinguishable bare advisory/failed row.

End-to-end regression: drive the actual `status` Click command (not just the
underlying detector) so the fix is verified at the layer the #1461 confusion
was actually observed at.
"""

from __future__ import annotations

import coord.network as network_mod
from click.testing import CliRunner

from coord.models import Assignment, Board
from coord.network import MachineStatus, StatusResult
from coord.state import get_connection, save_board


def _running(aid: str = "w1") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1454,
        issue_title="killed mid-flight",
        assignment_id=aid,
        type="work",
        status="running",
    )


def _run_status(valid_config_path, monkeypatch, *, agent_completed: list[dict]):
    from coord.commands.status import status as status_cmd
    from coord import config as config_mod

    cfg = config_mod.load(valid_config_path)
    laptop = next(m for m in cfg.machines if m.name == "laptop")
    server = next(m for m in cfg.machines if m.name == "server")

    def fake_check_all(machines, timeout=3.0, max_workers=None):
        return [
            MachineStatus(machine=laptop, state="online", latency_ms=1.0),
            MachineStatus(machine=server, state="offline", reason="connection refused"),
        ]

    def fake_fetch_status(machine, timeout=3.0):
        if machine.name == "laptop":
            return StatusResult(data={"active": [], "completed": agent_completed})
        return StatusResult(error="offline")

    monkeypatch.setattr(network_mod, "check_all", fake_check_all)
    monkeypatch.setattr(network_mod, "fetch_status", fake_fetch_status)

    runner = CliRunner()
    result = runner.invoke(
        status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_usage_limit_kill_surfaced_as_distinct_fleet_condition(
    valid_config_path, monkeypatch,
) -> None:
    board = Board(repos=[], machines=[], active=[_running("w1")])
    save_board(board)

    reason = "usage limit — resets 8:30pm (America/Chicago)"
    output = _run_status(
        valid_config_path, monkeypatch,
        agent_completed=[{
            "id": "w1",
            "status": "failed",
            "usage_limit_reason": reason,
            "spec": {
                "issue_number": 1454, "issue_title": "killed mid-flight",
                "repo_name": "api",
            },
        }],
    )

    assert "Usage limit" in output
    assert reason in output
    # Must NOT also show up in the generic advisory/failure noise with no
    # reset time attached — it's a distinct condition, not indistinguishable
    # ambient failure.
    assert "0 commits pushed" not in output

    # The reconciliation write must have landed on the persisted board row
    # too (so `coord drive` — reading straight from the DB — recognises it).
    conn = get_connection()
    row = conn.execute(
        "SELECT failure_reason FROM assignments WHERE assignment_id=?", ("w1",)
    ).fetchone()
    assert row["failure_reason"] == reason


def test_usage_limit_kill_excluded_from_advisory_bucket(
    valid_config_path, monkeypatch,
) -> None:
    """An ADVISORY-landing kill (#1456's shape) must appear in the usage-limit
    block, not the generic '0 commits pushed' Advisory bucket."""
    board = Board(repos=[], machines=[], active=[_running("w2")])
    save_board(board)

    reason = "usage limit — resets 8:30pm (America/Chicago)"
    output = _run_status(
        valid_config_path, monkeypatch,
        agent_completed=[{
            "id": "w2",
            "status": "advisory",
            "usage_limit_reason": reason,
            "spec": {
                "issue_number": 1456, "issue_title": "killed mid-flight, clean exit",
                "repo_name": "api",
            },
        }],
    )

    assert "Usage limit" in output
    assert "Advisory (needs attention" not in output
