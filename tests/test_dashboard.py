"""Tests for the web dashboard API endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock

from starlette.testclient import TestClient

from coord.config import Config
from coord.dashboard.server import build_app
from coord.models import Assignment, Board, Machine, Proposal, Repo
from coord.state import save_board


def _config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(
            name="laptop", host="laptop.tailnet", repos=["api"],
            repo_paths={"api": "/tmp/api"},
        )],
    )


def _client(tmp_path: Path | None = None) -> TestClient:
    return TestClient(build_app(_config()))


class TestIndexPage:
    def test_serves_html(self) -> None:
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "coord dashboard" in r.text


class TestBoardAPI:
    def test_returns_board_data(self, tmp_path: Path) -> None:
        board = Board(
            round_number=3,
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="abc", status="running",
                ),
            ],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=10, issue_title="Add logging",
                    assignment_id="def", status="done",
                    finished_at=1.0,
                ),
            ],
        )

        client = _client()
        with patch("coord.dashboard.server.load_board") as mock_load:
            mock_load.return_value = board
            r = client.get("/api/board")

        assert r.status_code == 200
        data = r.json()
        assert data["round_number"] == 3
        assert len(data["active"]) == 1
        assert data["active"][0]["issue_number"] == 42

    def test_empty_board(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=None),
            patch("coord.dashboard.server.build_board", return_value=Board()),
        ):
            r = client.get("/api/board")
        assert r.status_code == 200
        assert r.json()["active"] == []


class TestMachinesAPI:
    @patch("coord.dashboard.server.fetch_status")
    @patch("coord.dashboard.server.check_all")
    def test_returns_machine_list(
        self, mock_check: MagicMock, mock_fetch: MagicMock,
    ) -> None:
        mock_status = MagicMock()
        mock_status.machine = Machine(name="laptop", host="laptop.tailnet", repos=["api"])
        mock_status.state = "online"
        mock_status.reason = ""
        mock_status.latency_ms = 5.0
        mock_status.is_online = True
        mock_check.return_value = [mock_status]
        from coord.network import StatusResult
        mock_fetch.return_value = StatusResult(data={"active": [], "completed": []})

        client = _client()
        r = client.get("/api/machines")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["name"] == "laptop"
        assert data[0]["state"] == "online"


class TestProposalsAPI:
    def test_returns_proposals(self, tmp_path: Path) -> None:
        proposals = [
            Proposal(
                id=1, machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                rationale="test", files_likely=["auth.py"],
            ),
        ]
        client = _client()
        with patch("coord.dashboard.server.load_proposals", return_value=proposals):
            r = client.get("/api/proposals")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["issue_number"] == 42

    def test_empty_proposals(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.load_proposals", return_value=[]):
            r = client.get("/api/proposals")
        assert r.status_code == 200
        assert r.json() == []


class TestApproveAPI:
    @patch("coord.state.build_board", return_value=Board())
    @patch("coord.state.save_board")
    @patch("coord.state.clear_proposals")
    @patch("coord.state.record_dispatched")
    @patch("coord.state.load_dispatched", return_value=[])
    @patch("coord.state.load_proposals")
    @patch("coord.dispatch.post_briefing")
    @patch("coord.dispatch.httpx.post")
    def test_approve_dispatches(
        self, mock_post, mock_briefing, mock_load_p, mock_load_d,
        mock_record, mock_clear, mock_save, mock_build,
    ) -> None:
        mock_load_p.return_value = [
            Proposal(
                id=1, machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix",
                rationale="test",
            ),
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "xyz"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp

        client = _client()
        r = client.post("/api/approve", json={"ids": [1]})
        assert r.status_code == 200
        data = r.json()
        assert data["results"][0]["ok"]

    def test_approve_invalid_json(self) -> None:
        client = _client()
        r = client.post("/api/approve", content="not json", headers={"content-type": "application/json"})
        assert r.status_code == 400

    def test_approve_empty_ids(self) -> None:
        client = _client()
        r = client.post("/api/approve", json={"ids": []})
        assert r.status_code == 400


class TestRejectAPI:
    def test_reject_removes_proposals(self) -> None:
        proposals = [
            Proposal(id=1, machine_name="m", repo_name="api",
                     issue_number=1, issue_title="A", rationale=""),
            Proposal(id=2, machine_name="m", repo_name="api",
                     issue_number=2, issue_title="B", rationale=""),
        ]
        client = _client()
        with (
            patch("coord.state.load_proposals", return_value=proposals),
            patch("coord.state.save_proposals") as mock_save,
        ):
            r = client.post("/api/reject", json={"ids": [1]})
        assert r.status_code == 200
        data = r.json()
        assert data["removed"] == 1
        assert data["remaining"] == 1
        saved = mock_save.call_args.args[0]
        assert len(saved) == 1
        assert saved[0].id == 2

    def test_reject_invalid_json(self) -> None:
        client = _client()
        r = client.post("/api/reject", content="bad", headers={"content-type": "application/json"})
        assert r.status_code == 400

    def test_reject_empty_ids(self) -> None:
        client = _client()
        r = client.post("/api/reject", json={"ids": []})
        assert r.status_code == 400


class TestDiffAPI:
    def test_diff_not_found(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=Board()),
            patch("coord.dashboard.server.build_board", return_value=Board()),
        ):
            r = client.get("/api/diff/nonexistent")
        assert r.status_code == 404

    def test_diff_no_branch(self) -> None:
        board = Board(completed=[
            Assignment(machine_name="m", repo_name="api", issue_number=1,
                       issue_title="t", assignment_id="abc", status="done",
                       branch=None),
        ])
        client = _client()
        with patch("coord.dashboard.server.load_board", return_value=board):
            r = client.get("/api/diff/abc")
        assert r.status_code == 404
        assert "no branch" in r.json()["error"]

    @patch("coord.github_ops._gh")
    def test_diff_from_pr(self, mock_gh: MagicMock) -> None:
        board = Board(completed=[
            Assignment(machine_name="m", repo_name="api", issue_number=1,
                       issue_title="t", assignment_id="abc", status="done",
                       branch="feat/x"),
        ])
        mock_gh.return_value = "diff --git a/f.py b/f.py\n+new line"
        client = _client()
        with patch("coord.dashboard.server.load_board", return_value=board):
            r = client.get("/api/diff/abc")
        assert r.status_code == 200
        assert "new line" in r.json()["diff"]


class TestBriefingOverride:
    @patch("coord.state.build_board", return_value=Board())
    @patch("coord.state.save_board")
    @patch("coord.state.clear_proposals")
    @patch("coord.state.record_dispatched")
    @patch("coord.state.load_dispatched", return_value=[])
    @patch("coord.state.load_proposals")
    @patch("coord.dispatch.post_briefing")
    @patch("coord.dispatch.httpx.post")
    def test_briefing_override_applied(
        self, mock_post, mock_briefing, mock_load_p, *_mocks,
    ) -> None:
        mock_load_p.return_value = [
            Proposal(id=1, machine_name="laptop", repo_name="api",
                     issue_number=42, issue_title="Fix",
                     rationale="test", briefing="original"),
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "xyz"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp

        client = _client()
        r = client.post("/api/approve", json={
            "ids": [1],
            "briefings": {"1": "edited briefing"},
        })
        assert r.status_code == 200
        call_args = mock_post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["briefing"] == "edited briefing"


class TestChatAPI:
    def test_chat_requires_message(self) -> None:
        client = _client()
        r = client.post("/api/chat", json={"message": ""})
        assert r.status_code == 400

    def test_chat_invalid_json(self) -> None:
        client = _client()
        r = client.post("/api/chat", content="bad", headers={"content-type": "application/json"})
        assert r.status_code == 400


class TestPipelineAction:
    """Tests for /api/pipeline/action — dispatch feedback fields."""

    def _board_with_done(self) -> "Board":
        return Board(
            active=[],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="work001", status="done",
                    branch="issue-42-fix-auth",
                    finished_at=1.0,
                ),
            ],
        )

    def test_dispatch_review_returns_machine_and_id(self) -> None:
        review_assignment = Assignment(
            machine_name="desktop", repo_name="api",
            issue_number=42, issue_title="Fix auth",
            assignment_id="rev00001", status="running", type="review",
        )
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_done()),
            patch("coord.review.dispatch_review", return_value=review_assignment) as mock_dr,
            patch("coord.dashboard.server.save_board"),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "dispatch_review",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["machine_name"] == "desktop"
        assert data["assignment_id"] == "rev00001"

    def test_dispatch_review_none_returns_error(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_done()),
            patch("coord.review.dispatch_review", return_value=None),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "dispatch_review",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "error" in data
        assert len(data["error"]) > 0

    def test_dispatch_review_exception_returns_500(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_done()),
            patch("coord.review.dispatch_review", side_effect=RuntimeError("agent down")),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "dispatch_review",
            })
        assert r.status_code == 500
        data = r.json()
        assert data["ok"] is False
        assert "agent down" in data["error"]

    def test_dispatch_smoke_returns_machine_and_id(self) -> None:
        smoke_assignment = Assignment(
            machine_name="gpu-box", repo_name="api",
            issue_number=42, issue_title="Fix auth",
            assignment_id="smk00001", status="running", type="smoke",
        )
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_done()),
            patch("coord.smoke.dispatch_smoke", return_value=smoke_assignment),
            patch("coord.dashboard.server.save_board"),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "dispatch_smoke",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["machine_name"] == "gpu-box"
        assert data["assignment_id"] == "smk00001"

    def test_dispatch_smoke_none_returns_error(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_done()),
            patch("coord.smoke.dispatch_smoke", return_value=None),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "dispatch_smoke",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "error" in data

    def test_pipeline_action_unknown_assignment(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.load_board", return_value=Board()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "doesnotexist",
                "action": "dispatch_review",
            })
        assert r.status_code == 404

    def test_pipeline_action_missing_fields(self) -> None:
        client = _client()
        r = client.post("/api/pipeline/action", json={"action": "dispatch_review"})
        assert r.status_code == 400


class TestPipelineActionTestVerdict:
    """Tests for /api/pipeline/action action='test-verdict'."""

    def _board_with_done(self) -> "Board":
        return Board(
            active=[],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="work001", status="done",
                    branch="issue-42-fix-auth",
                    finished_at=1.0,
                ),
            ],
        )

    def test_pass_verdict_records_passed(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_done()),
            patch("coord.state.record_test_verdict") as mock_rtv,
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "test-verdict",
                "verdict": "pass",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["test_state"] == "passed"
        mock_rtv.assert_called_once_with(
            assignment_id="work001",
            test_state="passed",
            test_reason=None,
            smoke_test="pass",
            smoke_test_reason=None,
        )

    def test_fail_verdict_records_failed_with_reason(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_done()),
            patch("coord.state.record_test_verdict") as mock_rtv,
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "test-verdict",
                "verdict": "fail",
                "reason": "cargo test failed on line 42",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["test_state"] == "failed"
        mock_rtv.assert_called_once_with(
            assignment_id="work001",
            test_state="failed",
            test_reason="cargo test failed on line 42",
            smoke_test="fail",
            smoke_test_reason="cargo test failed on line 42",
        )

    def test_skip_verdict_records_skipped(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_done()),
            patch("coord.state.record_test_verdict") as mock_rtv,
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "test-verdict",
                "verdict": "skip",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["test_state"] == "skipped"
        # skip does not mirror to smoke_test
        mock_rtv.assert_called_once_with(
            assignment_id="work001",
            test_state="skipped",
            test_reason=None,
            smoke_test=None,
            smoke_test_reason=None,
        )

    def test_invalid_verdict_returns_400(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.load_board", return_value=self._board_with_done()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "test-verdict",
                "verdict": "notaverdict",
            })
        assert r.status_code == 400
        assert "error" in r.json()

    def test_exception_returns_500(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_done()),
            patch("coord.state.record_test_verdict", side_effect=RuntimeError("db locked")),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "test-verdict",
                "verdict": "pass",
            })
        assert r.status_code == 500
        data = r.json()
        assert data["ok"] is False
        assert "db locked" in data["error"]


class TestPipelineActionRecordReviewVerdict:
    """Tests for /api/pipeline/action action='record-review-verdict'.

    The phone client sends the WORK assignment id (as returned by GET
    /api/pipeline).  The handler must look up the linked review assignment and
    write to THAT row — not the work row — because compute_pipeline reads
    findings back from the review assignment.
    """

    def _board_with_work_and_review(self) -> "Board":
        """A completed work assignment with a linked completed review assignment."""
        return Board(
            active=[],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="work001", status="done",
                    finished_at=1.0,
                ),
                Assignment(
                    machine_name="desktop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="rev001", status="done",
                    type="review",
                    review_of_assignment_id="work001",
                    finished_at=2.0,
                ),
            ],
        )

    def _board_with_work_only(self) -> "Board":
        """A work assignment with NO linked review assignment."""
        return Board(
            active=[],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="work001", status="done",
                    finished_at=1.0,
                ),
            ],
        )

    def test_approve_verdict_persists_to_review_id(self) -> None:
        """The mock must be called with the review assignment id, not the work id."""
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_work_and_review()),
            patch("coord.notify._persist_review_findings") as mock_prf,
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "approve",
                "body": "LGTM — code is clean.",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        # Key assertion: written to rev001 (the review row), NOT work001.
        mock_prf.assert_called_once_with("rev001", "approve", "LGTM — code is clean.")

    def test_request_changes_verdict_persists_to_review_id(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_work_and_review()),
            patch("coord.notify._persist_review_findings") as mock_prf,
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "request-changes",
                "body": "Missing tests on the new endpoint.",
            })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        mock_prf.assert_called_once_with(
            "rev001", "request-changes", "Missing tests on the new endpoint.",
        )

    def test_no_review_assignment_returns_404(self) -> None:
        """404 when the work assignment has no linked review assignment."""
        client = _client()
        with patch("coord.dashboard.server.load_board", return_value=self._board_with_work_only()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "approve",
                "body": "LGTM",
            })
        assert r.status_code == 404
        assert "no review assignment" in r.json()["error"]

    def test_invalid_verdict_returns_400(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.load_board", return_value=self._board_with_work_and_review()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "reject",
                "body": "Some body",
            })
        assert r.status_code == 400
        assert "error" in r.json()

    def test_missing_body_returns_400(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.load_board", return_value=self._board_with_work_and_review()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "approve",
            })
        assert r.status_code == 400
        assert "body" in r.json()["error"]

    def test_exception_returns_500(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_work_and_review()),
            patch("coord.notify._persist_review_findings", side_effect=RuntimeError("db locked")),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "work001",
                "action": "record-review-verdict",
                "verdict": "approve",
                "body": "LGTM",
            })
        assert r.status_code == 500
        assert r.json()["ok"] is False


class TestPipelineReviewFindings:
    """Tests that GET /api/pipeline includes review_verdict and review_findings_body."""

    def _board_with_review(self) -> "Board":
        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="Fix auth",
            assignment_id="work001", status="done",
            branch="issue-42-fix-auth",
            finished_at=1.0,
        )
        review = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="Fix auth",
            assignment_id="rev001", status="done",
            type="review",
            review_of_assignment_id="work001",
            review_verdict="approve",
            review_posted_at=2.0,
            finished_at=2.0,
        )
        return Board(active=[], completed=[work, review])

    def test_review_verdict_and_body_in_pipeline_response(self) -> None:
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=self._board_with_review()),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch(
                "coord.state.load_assignment_review_findings",
                return_value=("approve", "LGTM — clean diff."),
            ),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        items = r.json()
        # Only work assignments appear in the pipeline view.
        assert len(items) == 1
        item = items[0]
        assert item["assignment_id"] == "work001"
        assert item["review_verdict"] == "approve"
        assert item["review_findings_body"] == "LGTM — clean diff."

    def test_no_review_assignment_yields_none_findings(self) -> None:
        """A work assignment with no review yet has None verdict + body."""
        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=99, issue_title="Standalone",
            assignment_id="work002", status="done",
            finished_at=1.0,
        )
        board = Board(active=[], completed=[work])
        client = _client()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.state.load_assignment_review_findings") as mock_lrf,
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 1
        assert items[0]["review_verdict"] is None
        assert items[0]["review_findings_body"] is None
        # load_assignment_review_findings must not be called when there's no review.
        mock_lrf.assert_not_called()


class TestDashboardDispatchUI:
    """Tests confirming the HTML includes the new dispatch-feedback elements."""

    def test_dispatch_status_css_present(self) -> None:
        client = _client()
        r = client.get("/")
        assert "dispatch-status" in r.text
        assert "dispatch-pending" in r.text
        assert "dispatch-ok" in r.text
        assert "dispatch-err" in r.text

    def test_pipeline_area_wrapper_in_source(self) -> None:
        client = _client()
        r = client.get("/")
        assert "pipeline-area" in r.text

    def test_dispatch_status_js_in_source(self) -> None:
        client = _client()
        r = client.get("/")
        assert "dispatchStatus" in r.text
        assert "renderDispatchStatus" in r.text
        assert "updateCardPipeline" in r.text
        assert "Dispatching #" in r.text
        assert "✓ Dispatched" in r.text
        assert "✗ Dispatch failed" in r.text


class TestXSSSafety:
    def test_html_served_has_escape_function(self) -> None:
        client = _client()
        r = client.get("/")
        assert "const E = " in r.text

    def test_board_data_does_not_appear_unescaped_in_source(self) -> None:
        client = _client()
        r = client.get("/")
        assert "${a.issue_title}" not in r.text
        assert "E(a.issue_title)" in r.text


# ── _poll_once unit tests ────────────────────────────────────────────────────


class TestPollOnce:
    """Unit tests for the module-level _poll_once function.

    Each test drives _poll_once directly with a fake board and mocked
    _fetch_agent_status so no real network calls are made.
    """

    def _make_config(self) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )

    def _running_board(self, aid: str = "abc") -> Board:
        return Board(
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id=aid, status="running",
                    dispatched_at=1.0,
                ),
            ],
        )

    def _agent_resp(self, aid: str, status: str) -> dict:
        return {
            "active": [],
            "completed": [{"id": aid, "status": status}],
        }

    def test_running_to_done_fires_assignment_completed(self) -> None:
        from coord.dashboard.server import _poll_once
        from coord.events import ASSIGNMENT_COMPLETED, EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("abc")

        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=self._agent_resp("abc", "done"),
        ):
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        assert len(es._history) == 1
        assert es._history[0].type == ASSIGNMENT_COMPLETED
        assert "abc" in seen

    def test_running_to_failed_fires_assignment_failed(self) -> None:
        from coord.dashboard.server import _poll_once
        from coord.events import ASSIGNMENT_FAILED, EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("xyz")

        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=self._agent_resp("xyz", "failed"),
        ):
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        assert len(es._history) == 1
        assert es._history[0].type == ASSIGNMENT_FAILED

    def test_running_to_cancelled_fires_assignment_cancelled(self) -> None:
        """Bug 1 regression: cancelled must not fire ASSIGNMENT_FAILED."""
        from coord.dashboard.server import ASSIGNMENT_CANCELLED, _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("ccc")

        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=self._agent_resp("ccc", "cancelled"),
        ):
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        assert len(es._history) == 1
        assert es._history[0].type == ASSIGNMENT_CANCELLED

    def test_running_to_advisory_fires_assignment_advisory(self) -> None:
        """#448 regression: advisory must fire ASSIGNMENT_ADVISORY, not FAILED.

        Without the dashboard fix, advisory fell through to the else-branch
        and emitted a red ASSIGNMENT_FAILED toast for a clean 0-commit exit.
        """
        from coord.dashboard.server import ASSIGNMENT_ADVISORY, _poll_once
        from coord.events import ASSIGNMENT_FAILED, EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("adv1")

        agent_resp = {
            "active": [],
            "completed": [{
                "id": "adv1",
                "status": "advisory",
                "zero_commit_reason": "worker exited cleanly but pushed 0 commits",
            }],
        }
        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=agent_resp,
        ):
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        assert len(es._history) == 1
        event = es._history[0]
        assert event.type == ASSIGNMENT_ADVISORY, (
            f"expected {ASSIGNMENT_ADVISORY}, got {event.type!r} — "
            "advisory must not be routed to ASSIGNMENT_FAILED"
        )
        assert event.type != ASSIGNMENT_FAILED
        # zero_commit_reason should be carried through to the client.
        assert event.data.get("zero_commit_reason") == (
            "worker exited cleanly but pushed 0 commits"
        )

    def test_absent_over_threshold_appears_in_possibly_stuck(self) -> None:
        """An assignment absent from agent data past the threshold is stuck."""
        from coord.dashboard.server import _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("stuck1")

        # Agent is reachable but knows nothing about "stuck1".
        agent_resp = {"active": [], "completed": []}
        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=agent_resp,
        ):
            # dispatched_at=1.0, now=1000.0 → 999 s > _STUCK_THRESHOLD (300 s)
            result = asyncio.run(
                _poll_once(config, es, seen, orphaned, board=board, now=1000.0)
            )

        ids = [r["assignment_id"] for r in result]
        assert "stuck1" in ids

    def test_absent_under_threshold_not_in_possibly_stuck(self) -> None:
        """An assignment absent from agent data under the threshold is not stuck."""
        from coord.dashboard.server import _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        seen: set[str] = set()
        orphaned: dict[str, float] = {}
        board = self._running_board("fresh1")

        agent_resp = {"active": [], "completed": []}
        with patch(
            "coord.dashboard.server._fetch_agent_status",
            return_value=agent_resp,
        ):
            # dispatched_at=1.0, now=100.0 → 99 s < _STUCK_THRESHOLD (300 s)
            result = asyncio.run(
                _poll_once(config, es, seen, orphaned, board=board, now=100.0)
            )

        ids = [r["assignment_id"] for r in result]
        assert "fresh1" not in ids

    def test_seen_terminal_prevents_refiring(self) -> None:
        """An assignment already in seen_terminal must not query the agent again."""
        from coord.dashboard.server import _poll_once
        from coord.events import EventSource

        config = self._make_config()
        es = EventSource()
        # Pre-populate seen_terminal with the assignment's id.
        seen: set[str] = {"abc"}
        orphaned: dict[str, float] = {}
        board = self._running_board("abc")

        with patch(
            "coord.dashboard.server._fetch_agent_status",
        ) as mock_fetch:
            asyncio.run(_poll_once(config, es, seen, orphaned, board=board, now=1000.0))

        # running set will be empty because aid is in seen_terminal → early return
        mock_fetch.assert_not_called()
        assert len(es._history) == 0


# ── Bug regression tests (HTML/JS) ──────────────────────────────────────────


class TestBugFixes:
    """Regression tests that ensure the HTML/JS bug fixes stay in place."""

    def test_bug2_toast_uses_textcontent_not_e(self) -> None:
        """Bug 2: showToast uses textContent so E() in toast strings double-encodes."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        # The fixed code passes plain d.repo_name / d.machine_name, not E(...).
        assert "E(d.repo_name)" not in r.text
        assert "E(d.machine_name)" not in r.text

    def test_bug3_cost_null_guard(self) -> None:
        """Bug 3: a cost of $0 was hidden by the falsy check; fix uses != null."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        # The fixed code uses optional-chain + != null for both stats fields.
        assert "!= null" in r.text

    def test_bug4_no_invalid_css_title_property(self) -> None:
        """Bug 4: `title:` is not a valid CSS property — must not appear in <style>."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        # Extract the style block and confirm `title:` is absent.
        import re
        style_blocks = re.findall(r"<style[^>]*>(.*?)</style>", r.text, re.DOTALL)
        for block in style_blocks:
            assert "title:" not in block, (
                f"Found invalid CSS `title:` property in <style> block"
            )

    def test_bug6_bell_toggle_present(self) -> None:
        """Bug 6: bell toggle button, function, and localStorage persistence."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "bell-btn" in r.text
        assert "toggleBell" in r.text
        assert "bellEnabled" in r.text
        assert "localStorage" in r.text

    def test_bug1_cancelled_event_handled_in_html(self) -> None:
        """Bug 1: client-side listener for assignment_cancelled must exist."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "assignment_cancelled" in r.text

    def test_bug1_assignment_cancelled_constant_exported(self) -> None:
        """Bug 1: ASSIGNMENT_CANCELLED must be importable from server module."""
        from coord.dashboard.server import ASSIGNMENT_CANCELLED
        assert ASSIGNMENT_CANCELLED == "assignment_cancelled"


class TestCLI:
    def test_web_help(self) -> None:
        from click.testing import CliRunner
        from coord.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["web", "--help"])
        assert result.exit_code == 0
        assert "7434" in result.output


class TestSSEEvents:
    """Tests for /events SSE endpoint (issue #214)."""

    def test_events_route_is_registered(self) -> None:
        """The /events route must exist in the dashboard app.

        We verify by inspecting the Starlette app routes directly — streaming
        the body would block the test because SSE never closes on the server.
        """
        from coord.dashboard.server import build_app as _build_app
        app = _build_app(_config())
        route_paths = [
            getattr(r, "path", None)
            for r in getattr(app, "routes", [])
        ]
        assert "/events" in route_paths, f"Expected /events in routes, got: {route_paths}"

    def test_events_html_includes_sse_connection(self) -> None:
        """The HTML must include the SSE connection code."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "connectSSE" in r.text
        assert "EventSource" in r.text
        assert "/events" in r.text

    def test_html_includes_toast_system(self) -> None:
        """The HTML must include toast notification elements."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "toast-container" in r.text
        assert "showToast" in r.text
        assert "toast-done" in r.text
        assert "toast-failed" in r.text

    def test_html_includes_audio_bell(self) -> None:
        """The HTML must include audio bell code."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "playBell" in r.text
        assert "AudioContext" in r.text

    def test_html_includes_sse_dot_indicator(self) -> None:
        """The HTML must show a live-events connection status indicator."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "sse-dot" in r.text

    def test_html_includes_stuck_detection(self) -> None:
        """The HTML must show possibly-stuck warning and unstick button."""
        client = _client()
        r = client.get("/")
        assert r.status_code == 200
        assert "possibly_stuck" in r.text
        assert "stuck-banner" in r.text
        assert "btn-unstick" in r.text
        assert "Cancel" in r.text


class TestUnstickAction:
    """Tests for the 'unstick' pipeline action (issue #214)."""

    def _board_with_running(self) -> "Board":
        import time as _t
        return Board(
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=42, issue_title="Fix auth",
                    assignment_id="run001", status="running",
                    dispatched_at=_t.time() - 600,  # 10 min ago
                ),
            ],
        )

    def test_unstick_marks_failed_and_returns_ok(self) -> None:
        client = _client()
        board = self._board_with_running()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.dashboard.server.save_board"),
            patch("coord.dashboard.server.httpx.post", side_effect=Exception("unreachable")),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "run001",
                "action": "unstick",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["cancelled_on_agent"] is False
        # Assignment should be marked failed in the board
        a = board.find_by_id("run001")
        assert a is not None
        assert a.status == "failed"

    def test_unstick_cancelled_on_agent_when_reachable(self) -> None:
        client = _client()
        board = self._board_with_running()
        mock_response = MagicMock()
        mock_response.status_code = 200
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.dashboard.server.save_board"),
            patch("coord.dashboard.server.httpx.post", return_value=mock_response),
        ):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "run001",
                "action": "unstick",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["cancelled_on_agent"] is True

    def test_unstick_unknown_assignment_returns_404(self) -> None:
        client = _client()
        with patch("coord.dashboard.server.load_board", return_value=Board()):
            r = client.post("/api/pipeline/action", json={
                "assignment_id": "doesnotexist",
                "action": "unstick",
            })
        assert r.status_code == 404
