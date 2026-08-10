"""#1351: finalize_interactive_exit wires the Test transcript-floor before
the operator-prompt backstop.

Mirrors the wiring discipline of the #606 review transcript-floor
(tests/test_transcript_floor.py + the transcript-floor block in
finalize_interactive_exit): a recovered TEST_VERDICT block must land on the
WORK row named by `smoke_of`, never on the smoke session's own
`assignment_id` row, and must be idempotent against an already-recorded
`test_state` — mirroring `_prompt_and_relay_test_verdict`'s own idempotency
gate so the two backstops never fight each other.
"""

from __future__ import annotations

from unittest.mock import patch

from coord import state as state_mod
from coord.interactive import finalize_interactive_exit
from coord.review import TestVerdictFindings
from tests.test_issue_store_seam import _seed_running_assignment


def _test_state_of(assignment_id: str) -> str | None:
    from coord.state import build_board

    row = build_board().find_by_id(assignment_id)
    return row.test_state if row else None


class TestFinalizeWiresTestTranscriptFloor:
    def test_records_recovered_verdict_on_work_row(self) -> None:
        _seed_running_assignment("work-1351", assignment_type="work", issue_number=1351)
        _seed_running_assignment("smoke-1351", assignment_type="smoke", issue_number=1351)

        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.interactive._review_findings_from_transcript",
                return_value=None,
            ),
            patch(
                "coord.interactive._test_verdict_from_transcript",
                return_value=TestVerdictFindings(
                    verdict="failed", reason="crashes on launch"
                ),
            ) as mock_floor,
        ):
            result = finalize_interactive_exit(
                assignment_id="smoke-1351",
                repo_name="api",
                repo_github="acme/api",
                issue_number=1351,
                machine_name="laptop",
                worktree_path=None,
                base_branch="main",
                exit_code=0,
                started_at=0.0,
                repo_path=None,
                smoke_of="work-1351",
            )

        mock_floor.assert_called_once()
        assert result.test_verdict_recovered == "failed"
        assert _test_state_of("work-1351") == "failed"
        # Never recorded against the SMOKE session's own row (#1351's core rule).
        assert _test_state_of("smoke-1351") is None

    def test_idempotent_when_work_already_has_a_verdict(self) -> None:
        """Mirrors `_prompt_and_relay_test_verdict`'s #1349 idempotency gate:
        an already-set test_state on the WORK row must not be clobbered, and
        the transcript scan itself must not even run."""
        _seed_running_assignment("work-1351b", assignment_type="work", issue_number=1351)
        _seed_running_assignment("smoke-1351b", assignment_type="smoke", issue_number=1351)
        state_mod.record_test_verdict(assignment_id="work-1351b", test_state="passed")

        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.interactive._review_findings_from_transcript",
                return_value=None,
            ),
            patch("coord.interactive._test_verdict_from_transcript") as mock_floor,
        ):
            result = finalize_interactive_exit(
                assignment_id="smoke-1351b",
                repo_name="api",
                repo_github="acme/api",
                issue_number=1351,
                machine_name="laptop",
                worktree_path=None,
                base_branch="main",
                exit_code=0,
                started_at=0.0,
                repo_path=None,
                smoke_of="work-1351b",
            )

        mock_floor.assert_not_called()
        assert result.test_verdict_recovered is None
        assert _test_state_of("work-1351b") == "passed"

    def test_no_recovery_leaves_result_field_none(self) -> None:
        _seed_running_assignment("work-1351c", assignment_type="work", issue_number=1351)
        _seed_running_assignment("smoke-1351c", assignment_type="smoke", issue_number=1351)

        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.interactive._review_findings_from_transcript",
                return_value=None,
            ),
            patch(
                "coord.interactive._test_verdict_from_transcript",
                return_value=None,
            ) as mock_floor,
        ):
            result = finalize_interactive_exit(
                assignment_id="smoke-1351c",
                repo_name="api",
                repo_github="acme/api",
                issue_number=1351,
                machine_name="laptop",
                worktree_path=None,
                base_branch="main",
                exit_code=0,
                started_at=0.0,
                repo_path=None,
                smoke_of="work-1351c",
            )

        mock_floor.assert_called_once()
        assert result.test_verdict_recovered is None
        assert _test_state_of("work-1351c") is None

    def test_smoke_of_none_is_a_noop(self) -> None:
        """Every non-smoke caller passes smoke_of=None (the default) — the
        Test floor must never run, structurally the same self-gating the
        review floor relies on for a work session's transcript."""
        _seed_running_assignment("rev-1351", assignment_type="review", issue_number=1351)

        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.interactive._review_findings_from_transcript",
                return_value=None,
            ),
            patch("coord.interactive._test_verdict_from_transcript") as mock_floor,
        ):
            result = finalize_interactive_exit(
                assignment_id="rev-1351",
                repo_name="api",
                repo_github="acme/api",
                issue_number=1351,
                machine_name="laptop",
                worktree_path=None,
                base_branch="main",
                exit_code=0,
                started_at=0.0,
                repo_path=None,
                # smoke_of NOT passed — every non-smoke caller today.
            )

        mock_floor.assert_not_called()
        assert result.test_verdict_recovered is None

    def test_recovers_even_when_smoke_row_already_closed(self) -> None:
        """The modal failure mode (#1351 review finding): the agent forgets
        to run `coord test` but DOES run the closing `coord report-result
        --status done` the smoke briefing unconditionally requires — so by
        the time this session's tmux exits and finalize_interactive_exit
        runs, the smoke row's own status is already terminal ("done"), not
        "running". The Test transcript-floor targets the WORK row
        (`smoke_of`), a different row than the one
        `_assignment_already_recorded(assignment_id)` inspects, so it must
        still run and recover the verdict even though this session's own row
        is already closed."""
        _seed_running_assignment(
            "work-1351d", assignment_type="work", issue_number=1351
        )
        _seed_running_assignment(
            "smoke-1351d", assignment_type="smoke", issue_number=1351
        )

        from coord import issue_store

        issue_store.post_result(
            issue_store.ResultRecord(
                assignment_id="smoke-1351d",
                machine_name="laptop",
                repo_name="api",
                repo_github="acme/api",
                issue_number=1351,
                status="done",
                verdict=None,
                summary="smoke session closed per the briefing's terminal step",
            )
        )

        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.interactive._review_findings_from_transcript",
                return_value=None,
            ),
            patch(
                "coord.interactive._test_verdict_from_transcript",
                return_value=TestVerdictFindings(
                    verdict="failed", reason="crashes on launch"
                ),
            ) as mock_floor,
        ):
            result = finalize_interactive_exit(
                assignment_id="smoke-1351d",
                repo_name="api",
                repo_github="acme/api",
                issue_number=1351,
                machine_name="laptop",
                worktree_path=None,
                base_branch="main",
                exit_code=0,
                started_at=0.0,
                repo_path=None,
                smoke_of="work-1351d",
            )

        mock_floor.assert_called_once()
        assert result.test_verdict_recovered == "failed"
        assert _test_state_of("work-1351d") == "failed"
        assert _test_state_of("smoke-1351d") is None
