"""#1116: `coord status` must not show [awaiting review] for a work assignment
whose test verdict is 'failed'.  The test-precedes-review gate correctly holds
the review, but the old code derived the display tag from ``review_state``
alone, which stays ``'pending'`` while the gate is held — so the row showed
"[awaiting review]" even though the test had failed, misleading the operator
into thinking the item was progressing normally.

Fix: when ``test_state == "failed"``, the CLI row now shows
"[✗ test FAILED — needs fix]" and suppresses the review-state tag entirely.
"""

from __future__ import annotations

import coord.network as network_mod
from click.testing import CliRunner

from coord.commands.status import status as status_cmd
from coord.models import Assignment, Board
from coord.state import save_board


def _work(
    aid: str = "w1",
    *,
    test_state: str | None = None,
    review_state: str = "pending",
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1116,
        issue_title="Fix the widget",
        assignment_id=aid,
        type="work",
        status="done",
        branch=f"issue-1116-{aid}",
        test_state=test_state,
        review_state=review_state,
    )


def _run_status(valid_config_path, monkeypatch) -> str:
    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: [])
    runner = CliRunner()
    result = runner.invoke(
        status_cmd, ["--config", str(valid_config_path)], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


# ---------------------------------------------------------------------------
# Bug-fix: test_state="failed" rows must show the failed tag, NOT [awaiting review]
# ---------------------------------------------------------------------------


def test_failed_test_state_shows_failed_tag(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """The #1116 case: a work item whose test gate failed must display the
    failure tag — not '[awaiting review]' — even though review_state='pending'."""
    save_board(Board(completed=[_work("w1", test_state="failed", review_state="pending")]))

    output = _run_status(valid_config_path, monkeypatch)

    assert "[✗ test FAILED — needs fix]" in output, output
    assert "[awaiting review]" not in output, output


def test_failed_test_state_not_confused_with_other_issue(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """When multiple completed items are present, the failed tag only applies
    to the failed item; others keep their review-state tag."""
    failed_item = _work("w-fail", test_state="failed", review_state="pending")
    # Different issue number so we can tell them apart in output
    ok_item = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Other issue",
        assignment_id="w-ok",
        type="work",
        status="done",
        branch="issue-42-w-ok",
        test_state=None,
        review_state="pending",
    )
    save_board(Board(completed=[failed_item, ok_item]))

    output = _run_status(valid_config_path, monkeypatch)

    # The failed item should show the failed tag
    assert "[✗ test FAILED — needs fix]" in output, output
    # The other item should still show awaiting review
    assert "[awaiting review]" in output, output


# ---------------------------------------------------------------------------
# No-regression: passing / none test_state keeps using review_state tag
# ---------------------------------------------------------------------------


def test_passed_test_state_keeps_review_tag(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """test_state='passed' must not suppress the review-state tag."""
    save_board(Board(completed=[_work("w1", test_state="passed", review_state="pending")]))

    output = _run_status(valid_config_path, monkeypatch)

    assert "[awaiting review]" in output, output
    assert "FAILED" not in output, output


def test_none_test_state_keeps_review_tag(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """test_state=None (never set) must not suppress the review-state tag."""
    save_board(Board(completed=[_work("w1", test_state=None, review_state="pending")]))

    output = _run_status(valid_config_path, monkeypatch)

    assert "[awaiting review]" in output, output
    assert "FAILED" not in output, output


def test_skipped_test_state_keeps_review_tag(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """test_state='skipped' must not suppress the review-state tag."""
    save_board(Board(completed=[_work("w1", test_state="skipped", review_state="pending")]))

    output = _run_status(valid_config_path, monkeypatch)

    assert "[awaiting review]" in output, output
    assert "FAILED" not in output, output


def test_failed_test_state_review_done_still_shows_failed_tag(
    valid_config_path, monkeypatch, coord_db,
) -> None:
    """Even if review_state has somehow advanced to 'done', a failed test still
    shows the failed tag (test failure takes priority over any review state)."""
    save_board(Board(completed=[_work("w1", test_state="failed", review_state="done")]))

    output = _run_status(valid_config_path, monkeypatch)

    assert "[✗ test FAILED — needs fix]" in output, output
    assert "[review done]" not in output, output
