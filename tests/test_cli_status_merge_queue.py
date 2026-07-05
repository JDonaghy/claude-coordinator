"""#420: `coord status`'s merge-queue section must recompute the review/
smoke gate error live rather than echoing the stored ``entry.error`` string
verbatim. That string is only refreshed by a real merge attempt
(`coord.merge_queue.process`) — nothing clears it when an approval or verdict
lands through the normal interactive path (no `coord merge`/auto-loop tick in
between), so a mergeable entry could keep showing "review required but not
approved" indefinitely, inviting an operator to redundantly bounce
already-approved work (the #410 real-world case this issue reports).

End-to-end regression: drive the actual `status` Click command (not just the
underlying `coord.merge_queue.display_error` unit) so the fix is verified at
the same layer the bug was observed.
"""

from __future__ import annotations

from click.testing import CliRunner

from coord.merge_queue import PENDING, QueuedMerge, save_queue
from coord.models import Assignment, Board
from coord.state import save_board


def _work(aid: str = "w1") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix thing",
        assignment_id=aid,
        type="work",
        status="done",
        branch=f"worker/{aid}",
    )


def _review(of_aid: str, *, verdict: str = "approve") -> Assignment:
    return Assignment(
        machine_name="server",
        repo_name="api",
        issue_number=1,
        issue_title="Fix thing",
        assignment_id=f"rev-{of_aid}",
        type="review",
        status="done",
        review_of_assignment_id=of_aid,
        review_verdict=verdict,
    )


def _queue_entry(*, error: str | None) -> QueuedMerge:
    return QueuedMerge(
        assignment_id="w1",
        repo_name="api",
        repo_github="acme/api",
        branch="worker/w1",
        target_branch="main",
        issue_number=1,
        issue_title="Fix thing",
        state=PENDING,
        error=error,
    )


def _run_status(valid_config_path, monkeypatch):
    import coord.network as network_mod
    from coord.commands.status import status as status_cmd

    # Machines aren't under test here — skip real network probing so the
    # test is hermetic and fast.
    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: [])

    runner = CliRunner()
    result = runner.invoke(
        status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_status_clears_stale_review_error_once_approved(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """The #410 case: entry.error was stamped while the review was still
    pending; an approval landed afterward with no merge attempt in between.
    `coord status` must not keep showing "review required but not approved"."""
    save_board(Board(completed=[_work("w1"), _review("w1", verdict="approve")]))
    save_queue([_queue_entry(error="review required but not approved")])

    output = _run_status(valid_config_path, monkeypatch)

    assert "Merge queue:" in output
    assert "#1 (worker/w1" in output
    assert "review required but not approved" not in output


def test_status_keeps_review_error_when_still_unapproved(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """Sanity check: the fix must not blank a *genuinely* unapproved entry."""
    save_board(Board(completed=[_work("w1")]))
    save_queue([_queue_entry(error="review required but not approved")])

    output = _run_status(valid_config_path, monkeypatch)

    assert "Merge queue:" in output
    assert "error: review required but not approved" in output
