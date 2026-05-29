"""Tests for the network-aware CLI commands (status, log, approve)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from coord import network, state as state_mod
from coord.cli import main

from .conftest import output_and_stderr


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
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


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db) -> Path:
    """Provide an isolated in-memory DB for state and return a temp dir."""
    return tmp_path


def _online_health(machine_name: str = "laptop") -> dict:
    return {"machine": machine_name, "capabilities": [], "repos": ["api"], "active": 0, "completed": 0}


class TestStatus:
    def test_all_machines_shown_when_online(self, config_file: Path, coord_dir: Path) -> None:
        statuses = [
            network.MachineStatus(machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]), state=network.ONLINE, latency_ms=12.0, health=_online_health()),
            network.MachineStatus(machine=MagicMock(name="server", host="server.tailnet", repos=["api"]), state=network.ONLINE, latency_ms=20.0, health=_online_health("server")),
        ]
        # Configure MagicMock to behave like Machine for the formatting code
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]
        statuses[1].machine.name = "server"
        statuses[1].machine.host = "server.tailnet"
        statuses[1].machine.repos = ["api"]

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=network.StatusResult(data={"active": [], "completed": []})):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        assert "laptop" in result.output
        assert "server" in result.output
        assert "online" in result.output
        assert "idle" in result.output

    def test_status_unavailable_when_fetch_fails(self, config_file: Path, coord_dir: Path) -> None:
        """When /health passes but /status returns 500, show 'status unavailable' not 'idle'."""
        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE, latency_ms=12.0, health=_online_health(),
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=network.StatusResult(error="HTTP 500")):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        assert "status unavailable" in result.output
        assert "500" in result.output
        assert "idle" not in result.output

    def test_status_unavailable_on_timeout(self, config_file: Path, coord_dir: Path) -> None:
        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE, latency_ms=3000.0, health=_online_health(),
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=network.StatusResult(error="timeout")):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        assert "status unavailable" in result.output
        assert "timeout" in result.output
        assert "idle" not in result.output

    def test_offline_machine_reported_with_reason(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        # Use the real check_all path but force httpx to raise
        with patch.object(
            network.httpx, "get",
            side_effect=httpx.ConnectError("[Errno 111] Connection refused"),
        ):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "offline" in result.output
        assert "refused" in result.output

    def test_machine_filter_limits_output(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            if "/status" in url:
                r.json.return_value = {"active": [], "completed": []}
            else:
                r.json.return_value = _online_health()
            return r

        with patch.object(network.httpx, "get", side_effect=fake_get):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file), "--machine", "laptop"]
            )
        assert result.exit_code == 0, result.output
        assert "laptop" in result.output
        assert "server" not in result.output

    def test_unknown_machine_filter_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        result = CliRunner().invoke(
            main, ["status", "--config", str(config_file), "--machine", "ghost"]
        )
        assert result.exit_code != 0
        assert "ghost" in result.output

    def test_auto_reconcile_updates_board(self, config_file: Path, coord_dir: Path) -> None:
        """When an agent reports an assignment complete, coord status should reconcile the board."""
        from coord.models import Assignment, Board
        from coord.state import save_board, load_board

        # Set up a board with one active assignment
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                assignment_id="abc123", status="running",
            ),
        ])
        save_board(board)

        # Agent reports the assignment as completed
        agent_status = network.StatusResult(data={
            "active": [],
            "completed": [{"id": "abc123", "status": "done", "branch": "issue-42-fix-auth", "finished_at": 1000.0}],
        })
        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE, latency_ms=12.0, health=_online_health(),
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=agent_status):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        assert "reconciled 1" in result.output

        # Verify the board was actually updated
        updated_board = load_board()
        assert len(updated_board.active) == 0
        assert len(updated_board.completed) == 1
        assert updated_board.completed[0].branch == "issue-42-fix-auth"

    def test_no_reconcile_flag_skips_reconcile(self, config_file: Path, coord_dir: Path) -> None:
        """--no-reconcile should skip board updates even when agent reports completions."""
        from coord.models import Assignment, Board
        from coord.state import save_board, load_board

        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                assignment_id="abc123", status="running",
            ),
        ])
        save_board(board)

        agent_status = network.StatusResult(data={
            "active": [],
            "completed": [{"id": "abc123", "status": "done", "branch": "issue-42-fix-auth"}],
        })
        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE, latency_ms=12.0, health=_online_health(),
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=agent_status):
            result = CliRunner().invoke(main, ["status", "--no-reconcile", "--config", str(config_file)])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        assert "reconciled" not in result.output

        # Board should still have the assignment as active
        unchanged_board = load_board()
        assert len(unchanged_board.active) == 1


class TestLog:
    def test_remote_log_via_dispatched_ledger(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        # Seed a dispatched record so the CLI knows where to fetch
        from coord.models import Proposal
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t", rationale="r",
        )
        state_mod.record_dispatched(
            assignment_id="abc123", proposal=proposal, repo_github="acme/api"
        )

        with patch(
            "coord.network.fetch_log",
            return_value=(200, b"remote log content\n"),
        ):
            result = CliRunner().invoke(
                main, ["log", "abc123", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        assert "remote log content" in result.output

    def test_remote_log_via_explicit_machine_flag(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch(
            "coord.network.fetch_log",
            return_value=(200, b"explicit machine log"),
        ):
            result = CliRunner().invoke(
                main,
                ["log", "xyz", "--config", str(config_file), "--machine", "server"],
            )
        assert result.exit_code == 0
        assert "explicit machine log" in result.output

    def test_remote_404_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch(
            "coord.network.fetch_log",
            return_value=(404, b""),
        ):
            result = CliRunner().invoke(
                main,
                ["log", "missing", "--config", str(config_file), "--machine", "laptop"],
            )
        assert result.exit_code == 1
        assert "no log" in output_and_stderr(result).lower()

    def test_local_fallback_when_no_dispatched_record(
        self,
        config_file: Path,
        coord_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from coord import agent as agent_mod

        monkeypatch.setattr(agent_mod, "DEFAULT_STATE_DIR", tmp_path)
        log_path = tmp_path / "logs" / "loose.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("local fallback content")

        result = CliRunner().invoke(
            main, ["log", "loose", "--config", str(config_file), "--local"]
        )
        assert "local fallback content" in result.output


class TestApproveNetworkErrors:
    def test_offline_machine_reports_clearly(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        from coord.models import Proposal
        state_mod.save_proposals(
            [
                Proposal(
                    id=1, machine_name="laptop", repo_name="api",
                    issue_number=10, issue_title="t", rationale="r",
                    files_likely=["a.py"], briefing="b",
                ),
            ]
        )

        with patch(
            "coord.dispatch.httpx.post",
            side_effect=httpx.ConnectError("[Errno 111] Connection refused"),
        ):
            result = CliRunner().invoke(
                main, ["approve", "1", "--config", str(config_file)]
            )
        # The CLI continues past dispatch failures and exits 0
        assert "dispatch failed" in result.output
        assert "laptop" in result.output
        assert "offline" in result.output or "refused" in result.output
