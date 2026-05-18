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
        board_file = tmp_path / "board.json"
        save_board(board, path=board_file)

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
        mock_fetch.return_value = {"active": [], "completed": []}

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


class TestChatAPI:
    def test_chat_requires_message(self) -> None:
        client = _client()
        r = client.post("/api/chat", json={"message": ""})
        assert r.status_code == 400

    def test_chat_invalid_json(self) -> None:
        client = _client()
        r = client.post("/api/chat", content="bad", headers={"content-type": "application/json"})
        assert r.status_code == 400


class TestCLI:
    def test_web_help(self) -> None:
        from click.testing import CliRunner
        from coord.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["web", "--help"])
        assert result.exit_code == 0
        assert "7434" in result.output
