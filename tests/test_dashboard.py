"""Tests for the web dashboard API endpoints."""

from __future__ import annotations

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
