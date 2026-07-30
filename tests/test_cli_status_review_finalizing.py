"""#1566: `coord status`'s own inline reconcile block ("Reconcile board with
live agent data" in coord/commands/status.py) independently mirrors
coord/reconcile.py's logic — including the same bug: a review agent
reporting "done" was written straight to the board as status="done", before
`coord notify` has had a chance to parse + post the actual verdict. That
left a window where the board shows a finished review with no verdict,
indistinguishable from a genuinely dropped one (the #1346/#1348/#1563
failure mode).

This covers the fix in this THIRD write path (coord/reconcile.py's two
functions are covered by tests/test_reconcile_completions.py and
tests/test_review_state.py) — a completed review must land on the
intermediate "finalizing" status here too.
"""

from __future__ import annotations

import coord.network as network_mod
from click.testing import CliRunner

from coord.commands.status import status as status_cmd
from coord.models import Assignment, Board
from coord.network import MachineStatus, StatusResult
from coord.state import build_board, save_board


def test_status_reconcile_marks_completed_review_as_finalizing(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    review = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1472,
        issue_title="[review] fix the thing",
        status="running",
        assignment_id="rev-1",
        type="review",
        review_of_assignment_id="work-1",
    )
    save_board(Board(active=[review], completed=[]))

    payload = {
        "active": [],
        "completed": [
            {
                "id": "rev-1",
                "status": "done",
                "branch": "issue-1472-fix",
                "finished_at": 100.0,
            }
        ],
    }

    def _fake_check_all(machines, timeout=3.0, **kw):
        found = next((m for m in machines if m.name == "laptop"), None)
        assert found is not None
        return [MachineStatus(machine=found, state="online", latency_ms=1.0)]

    monkeypatch.setattr(network_mod, "check_all", _fake_check_all)
    monkeypatch.setattr(
        network_mod, "fetch_status", lambda *a, **k: StatusResult(data=payload)
    )

    runner = CliRunner()
    # Reconciliation runs by default (no --no-reconcile) — this is the branch
    # under test.
    result = runner.invoke(
        status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output

    board = build_board()
    rev = board.find_by_id("rev-1")
    assert rev is not None
    assert rev.status == "finalizing", (
        "a review agent reporting done must land on the intermediate "
        "'finalizing' status, not 'done', until coord notify captures the "
        "verdict — otherwise the board shows a finished review with no "
        "verdict, indistinguishable from a dropped one"
    )
