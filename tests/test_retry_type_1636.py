"""#1636: `coord retry` (and the `auto_reassign` path it shares with
`_reassign`) must not silently re-dispatch a `smoke`/`review` assignment as a
fresh `type="work"` worker pointed at the already-complete branch.

Root cause was `_reassign()` hardcoding `type="work"` in both the dispatch
payload and the retry `Assignment` regardless of `failed.type`. The fix:

- `_reassign` now carries `failed.type` through for WORK_LIKE_TYPES
  ("work", "mock-author", "test-author") — the regression this file pins is
  that a `type="work"` retry is completely unchanged.
- For any other type (`smoke`, `review`, ...) `_reassign` raises
  `UnsupportedRetryType` instead of silently downgrading, and `coord retry`
  turns that into a refusal naming the command that actually re-runs the
  right stage.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import Config, ModelsConfig
from coord.models import Assignment, Board, Machine, Repo

from .conftest import output_and_stderr


def _cfg() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
            Machine(
                name="server", host="server.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
        ],
        models=ModelsConfig(default="sonnet"),
    )


def _failed(**overrides) -> Assignment:
    base = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="t",
        briefing="b",
        assignment_id="failedid",
        status="failed",
        type="work",
        model="sonnet",
        branch="issue-42-t",
    )
    base.update(overrides)
    return Assignment(**base)


# ── Unit: _reassign itself ───────────────────────────────────────────────


class TestReassignCarriesType:
    @patch("coord.reconcile.httpx.post")
    def test_work_type_regression_unchanged(self, mock_post: MagicMock) -> None:
        """Control: the existing work-retry path must be untouched — type
        in ('work') produces type='work' out, branch continued, model
        escalated at the call site."""
        from coord.reconcile import _reassign

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        board = Board()
        failed = _failed(type="work")

        result = _reassign(failed, board, _cfg(), model="opus")

        assert result is not None
        assert result.type == "work"
        assert result.branch == "issue-42-t"
        assert result.model == "opus"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["type"] == "work"
        assert payload["target_branch"] == "issue-42-t"
        assert payload["model"] == "opus"

    @patch("coord.reconcile.httpx.post")
    def test_mock_author_type_is_preserved_not_downgraded(
        self, mock_post: MagicMock
    ) -> None:
        """mock-author is WORK_LIKE (flows through the same pipeline) but
        its own type must survive the retry too, not silently become
        plain 'work'."""
        from coord.reconcile import _reassign

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        board = Board()
        failed = _failed(type="mock-author")

        result = _reassign(failed, board, _cfg())

        assert result is not None
        assert result.type == "mock-author"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["type"] == "mock-author"

    @patch("coord.reconcile.httpx.post")
    def test_smoke_type_raises_instead_of_silently_dispatching_work(
        self, mock_post: MagicMock
    ) -> None:
        from coord.reconcile import UnsupportedRetryType, _reassign

        board = Board()
        failed = _failed(
            type="smoke", assignment_id="smokeid",
            review_of_assignment_id="workid1",
        )

        with pytest.raises(UnsupportedRetryType) as exc_info:
            _reassign(failed, board, _cfg())

        assert exc_info.value.assignment_type == "smoke"
        assert exc_info.value.work_assignment_id == "workid1"
        # Must fail BEFORE any dispatch POST — no side effects on refusal.
        mock_post.assert_not_called()

    @patch("coord.reconcile.httpx.post")
    def test_review_type_raises_instead_of_silently_dispatching_work(
        self, mock_post: MagicMock
    ) -> None:
        from coord.reconcile import UnsupportedRetryType, _reassign

        board = Board()
        failed = _failed(
            type="review", assignment_id="reviewid",
            review_of_assignment_id="workid2",
        )

        with pytest.raises(UnsupportedRetryType) as exc_info:
            _reassign(failed, board, _cfg())

        assert exc_info.value.assignment_type == "review"
        assert exc_info.value.work_assignment_id == "workid2"
        mock_post.assert_not_called()


class TestDescribeUnsupportedRetryType:
    def test_smoke_names_the_smoke_of_command(self) -> None:
        from coord.reconcile import UnsupportedRetryType, describe_unsupported_retry_type

        msg = describe_unsupported_retry_type(
            UnsupportedRetryType("smoke", "workid1")
        )
        assert "coord assign --interactive --smoke-of workid1" in msg

    def test_review_names_the_review_of_command(self) -> None:
        from coord.reconcile import UnsupportedRetryType, describe_unsupported_retry_type

        msg = describe_unsupported_retry_type(
            UnsupportedRetryType("review", "workid2")
        )
        assert "coord assign --interactive --review-of workid2" in msg

    def test_missing_work_id_falls_back_to_generic_message(self) -> None:
        from coord.reconcile import UnsupportedRetryType, describe_unsupported_retry_type

        msg = describe_unsupported_retry_type(UnsupportedRetryType("smoke", None))
        assert "None" not in msg
        assert "smoke" in msg


# ── CLI: `coord retry` ──────────────────────────────────────────────────


class TestCliRetryRefusesNonWorkTypes:
    def test_smoke_assignment_is_refused_naming_smoke_of(
        self, valid_config_path: Path
    ) -> None:
        board = Board(completed=[_failed(
            type="smoke", assignment_id="smokeid",
            review_of_assignment_id="workid1",
        )])
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.reconcile._reassign") as reassign,
        ):
            result = CliRunner().invoke(
                main, ["retry", "smokeid", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 1, out
        assert "--smoke-of workid1" in out
        # Refused before ever reaching _reassign — no fresh work dispatch.
        reassign.assert_not_called()

    def test_review_assignment_is_refused_naming_review_of(
        self, valid_config_path: Path
    ) -> None:
        board = Board(completed=[_failed(
            type="review", assignment_id="reviewid",
            review_of_assignment_id="workid2",
        )])
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.reconcile._reassign") as reassign,
        ):
            result = CliRunner().invoke(
                main, ["retry", "reviewid", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 1, out
        assert "--review-of workid2" in out
        reassign.assert_not_called()

    def test_work_assignment_retry_is_unchanged_and_states_its_type(
        self, valid_config_path: Path
    ) -> None:
        """Regression + #1636 fix 3: the CLI states the type it dispatched."""
        board = Board(completed=[_failed(type="work", assignment_id="workid")])
        retried = Assignment(
            machine_name="server", repo_name="api", issue_number=42,
            issue_title="[retry] t", assignment_id="new-retry-id",
            type="work", status="running", branch="issue-42-t",
        )
        with (
            patch("coord.board_service.read_board", return_value=board),
            patch("coord.board_service.write_board"),
            patch("coord.reconcile._reassign", return_value=retried) as reassign,
        ):
            result = CliRunner().invoke(
                main, ["retry", "workid", "--config", str(valid_config_path)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "Retried:" in out
        assert "type=work" in out
        reassign.assert_called_once()
