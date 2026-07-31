"""Tests for `coord review <aid>` (#1387) — the #555-promised escape hatch.

`dispatch_pending_reviews`'s #555 guard deliberately never auto-dispatches a
headless review for an interactive (`provider_name="claude-pty"`) work
completion, and its comment claims `coord review <id> -> dispatch_review`
exists as the deliberate-request escape hatch. It didn't, until now. These
tests cover the CLI wrapper's own logic (missing/not-done/no-branch/already-
in-flight/disabled guards, and the success path incl. the claude-pty case) —
`dispatch_review`'s internal candidate-selection/PR-opening logic already has
exhaustive coverage in test_review.py, so it's mocked here rather than
re-exercised.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Assignment, Board
from coord import state as state_mod

CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""

CONFIG_YAML_REVIEWS_DISABLED = CONFIG_YAML + "reviews:\n  enabled: false\n"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def config_file_reviews_disabled(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_REVIEWS_DISABLED)
    return p


def _make_assignment(assignment_id: str, **overrides) -> Assignment:
    defaults = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Some issue",
        assignment_id=assignment_id,
        type="work",
        status="done",
        branch="issue-42-fix",
    )
    defaults.update(overrides)
    return Assignment(**defaults)


def _make_review_assignment(of_id: str) -> Assignment:
    return Assignment(
        machine_name="server",
        repo_name="api",
        issue_number=42,
        issue_title="Some issue",
        assignment_id="review-001",
        type="review",
        status="pending",
        review_of_assignment_id=of_id,
    )


class TestCoordReviewGuards:
    def test_missing_assignment_errors(self, config_file: Path, coord_db) -> None:
        result = CliRunner().invoke(
            main, ["review", "no-such-id", "--config", str(config_file)]
        )
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_not_done_errors(self, config_file: Path, coord_db) -> None:
        a = _make_assignment("work-001", status="running")
        state_mod.save_board(Board(active=[a], completed=[]))

        result = CliRunner().invoke(
            main, ["review", "work-001", "--config", str(config_file)]
        )
        assert result.exit_code != 0
        assert "not 'done'" in result.output

    def test_no_branch_errors(self, config_file: Path, coord_db) -> None:
        a = _make_assignment("work-002", branch=None)
        state_mod.save_board(Board(active=[], completed=[a]))

        result = CliRunner().invoke(
            main, ["review", "work-002", "--config", str(config_file)]
        )
        assert result.exit_code != 0
        assert "no branch recorded" in result.output

    def test_already_in_flight_errors(self, config_file: Path, coord_db) -> None:
        a = _make_assignment("work-003")
        live_review = _make_review_assignment("work-003")
        state_mod.save_board(Board(active=[live_review], completed=[a]))

        with patch(
            "coord.review.dispatch_review"
        ) as disp:
            result = CliRunner().invoke(
                main, ["review", "work-003", "--config", str(config_file)]
            )

        assert result.exit_code != 0
        assert "already in flight" in result.output
        disp.assert_not_called()

    def test_reviews_disabled_errors(
        self, config_file_reviews_disabled: Path, coord_db
    ) -> None:
        a = _make_assignment("work-004")
        state_mod.save_board(Board(active=[], completed=[a]))

        with patch(
            "coord.review.dispatch_review"
        ) as disp:
            result = CliRunner().invoke(
                main,
                ["review", "work-004", "--config", str(config_file_reviews_disabled)],
            )

        assert result.exit_code != 0
        assert "disabled" in result.output
        disp.assert_not_called()

    def test_no_eligible_reviewer_errors(self, config_file: Path, coord_db) -> None:
        a = _make_assignment("work-005")
        state_mod.save_board(Board(active=[], completed=[a]))

        with patch(
            "coord.review.dispatch_review", return_value=None
        ) as disp:
            result = CliRunner().invoke(
                main, ["review", "work-005", "--config", str(config_file)]
            )

        assert result.exit_code != 0
        assert "no review dispatched" in result.output
        disp.assert_called_once()

    def test_reports_dispatch_review_reason_verbatim(
        self, config_file: Path, coord_db
    ) -> None:
        """#1627: the CLI must print dispatch_review's own reason instead of
        the old generic "no eligible reviewer machine, or a guard ... (see
        the coordinator log)" guess — that message pointed at a log entry
        the early guards never wrote."""
        a = _make_assignment("work-007")
        state_mod.save_board(Board(active=[], completed=[a]))

        def _fake_dispatch_review(assignment, board, cfg, **kwargs):
            assignment.review_dispatch_reason = (
                "assignment work-007 is type 'smoke', not reviewable work"
            )
            return None

        with patch(
            "coord.review.dispatch_review", side_effect=_fake_dispatch_review
        ) as disp:
            result = CliRunner().invoke(
                main, ["review", "work-007", "--config", str(config_file)]
            )

        assert result.exit_code != 0
        assert "is type 'smoke', not reviewable work" in result.output
        assert "see the coordinator log" not in result.output
        disp.assert_called_once()


class TestCoordReviewSuccess:
    def test_dispatches_exactly_one_review(self, config_file: Path, coord_db) -> None:
        a = _make_assignment("work-006")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        review_assignment = _make_review_assignment("work-006")

        def _fake_dispatch(completed, board_arg, cfg):
            board_arg.active.append(review_assignment)
            completed.pr_url = "https://github.com/acme/api/pull/7"
            return review_assignment

        with patch(
            "coord.review.dispatch_review", side_effect=_fake_dispatch
        ) as disp:
            result = CliRunner().invoke(
                main, ["review", "work-006", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        disp.assert_called_once()
        assert "review dispatched: review-001 on server" in result.output
        assert "https://github.com/acme/api/pull/7" in result.output

        reloaded = state_mod.build_board()
        assert any(
            x.assignment_id == "review-001" for x in reloaded.active
        )

    def test_dispatches_for_interactive_claude_pty_work(
        self, config_file: Path, coord_db
    ) -> None:
        """#555's own carve-out: an explicit `coord review` on a
        provider_name="claude-pty" (interactive) work completion must still
        dispatch — the guard only blocks the *automatic* bulk path."""
        a = _make_assignment("work-007", provider_name="claude-pty")
        board = Board(active=[], completed=[a])
        state_mod.save_board(board)

        review_assignment = _make_review_assignment("work-007")

        with patch(
            "coord.review.dispatch_review",
            return_value=review_assignment,
        ) as disp:
            result = CliRunner().invoke(
                main, ["review", "work-007", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        disp.assert_called_once()
        called_assignment = disp.call_args[0][0]
        assert called_assignment.provider_name == "claude-pty"
        assert "review dispatched: review-001 on server" in result.output
