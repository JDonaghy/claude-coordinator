"""Tests for worker progress parsing, warning detection, and coord stop."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from coord.agent import AgentServer, AssignmentSpec, DONE, RUNNING
from coord.cli import main
from coord.models import Assignment, Board
from coord.progress import parse_progress, WorkerProgress
from coord.state import save_board


def _init_repo(path: Path) -> Path:
    """Create a minimal git repo with one commit so worktrees can be created."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


# ── Progress parsing ───────────────────────────────────────────────────────


class TestParseProgress:
    def test_extracts_status_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text(
            "# header\n"
            "some output\n"
            "STATUS: read codebase → planning approach → confidence: high\n"
            "more output\n"
            "STATUS: first build passed → running tests → confidence: medium\n"
        )
        p = parse_progress(str(log))
        assert len(p.updates) == 2
        assert "read codebase" in p.updates[0]
        assert "first build" in p.updates[1]
        assert p.latest_confidence == "medium"
        assert p.stuck is None

    def test_extracts_stuck_signal(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text(
            "STATUS: trying approach 1 → confidence: medium\n"
            "STATUS: approach 1 failed → trying approach 2 → confidence: low\n"
            "STUCK: tried PATH fix and rustup, both failed. Blocker: missing system dep\n"
        )
        p = parse_progress(str(log))
        assert p.stuck is not None
        assert "missing system dep" in p.stuck
        assert any("STUCK" in w for w in p.warnings)

    def test_detects_consecutive_low_confidence(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text(
            "STATUS: first try → confidence: medium\n"
            "STATUS: second try → confidence: low\n"
            "STATUS: third try → confidence: low\n"
        )
        p = parse_progress(str(log))
        assert any("low" in w for w in p.warnings)

    def test_no_warnings_for_high_confidence(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text(
            "STATUS: setup → building → confidence: high\n"
            "STATUS: tests passing → cleanup → confidence: high\n"
        )
        p = parse_progress(str(log))
        assert p.warnings == []
        assert p.latest_confidence == "high"

    def test_missing_log_returns_empty(self, tmp_path: Path) -> None:
        p = parse_progress(str(tmp_path / "nope.log"))
        assert p.updates == []
        assert p.stuck is None

    def test_empty_log(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text("")
        p = parse_progress(str(log))
        assert p.updates == []

    def test_limits_updates_to_10(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        lines = [f"STATUS: step {i} → confidence: high\n" for i in range(20)]
        log.write_text("".join(lines))
        p = parse_progress(str(log))
        assert len(p.updates) == 10

    def test_to_dict(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text("STATUS: doing stuff → confidence: medium\n")
        p = parse_progress(str(log))
        d = p.to_dict()
        assert "updates" in d
        assert "stuck" in d
        assert "warnings" in d
        assert "latest_confidence" in d
        assert d["latest_confidence"] == "medium"


# ── stream-json path ───────────────────────────────────────────────────────


def _ndjson_log(log: Path, events: list[dict]) -> None:
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n")


class TestParseProgressStreamJson:
    def test_stream_json_log_yields_rolling_update(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        _ndjson_log(
            log,
            [
                {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6",
                 "session_id": "s1"},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            ],
        )
        p = parse_progress(str(log))
        assert p.updates  # synthesised "Turn N: tool" line
        assert "Turn" in p.updates[-1]

    def test_stream_json_rate_limit_surfaces_warning(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        _ndjson_log(
            log,
            [
                {"type": "system", "subtype": "init", "model": "x", "session_id": "s"},
                {"type": "rate_limit_event", "resets_at": 1716160000.0},
            ],
        )
        p = parse_progress(str(log))
        assert any("rate limited" in w for w in p.warnings)

    def test_plain_text_still_uses_regex_parser(self, tmp_path: Path) -> None:
        """Backward compat: non-stream-json logs still parsed by old STATUS regex."""
        log = tmp_path / "test.log"
        log.write_text(
            "STATUS: doing stuff → confidence: high\n"
            "STUCK: missing system dep\n"
        )
        p = parse_progress(str(log))
        assert p.stuck is not None
        assert "missing system dep" in p.stuck
        assert any("STUCK" in w for w in p.warnings)


# ── Agent server progress integration ──────────────────────────────────────


class TestAgentProgress:
    def test_progress_returned_for_running_assignment(self, tmp_path: Path) -> None:
        repo_dir = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo_dir)},
            worker_command=lambda spec: ["/bin/sh", "-c",
                "echo 'STATUS: started → building → confidence: high'; sleep 30"],
        )
        spec = AssignmentSpec(
            repo_name="api", repo_path=str(repo_dir),
            issue_number=1, issue_title="t", briefing="b",
        )
        a = server.assign(spec)

        import time
        for _ in range(50):
            if server.get(a.id).status == RUNNING:
                break
            time.sleep(0.02)
        time.sleep(0.1)

        prog = server.progress(a.id)
        assert prog is not None
        assert len(prog["updates"]) >= 1
        assert "started" in prog["updates"][0]
        server.shutdown(kill_running=True)

    def test_progress_included_in_list_assignments(self, tmp_path: Path) -> None:
        repo_dir = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo_dir)},
            worker_command=lambda spec: ["/bin/sh", "-c",
                "echo 'STATUS: working → confidence: high'; sleep 30"],
        )
        spec = AssignmentSpec(
            repo_name="api", repo_path=str(repo_dir),
            issue_number=1, issue_title="t", briefing="b",
        )
        a = server.assign(spec)

        import time
        for _ in range(50):
            if server.get(a.id).status == RUNNING:
                break
            time.sleep(0.02)
        time.sleep(0.1)

        status = server.list_assignments()
        assert len(status["active"]) == 1
        assert "progress" in status["active"][0]
        assert len(status["active"][0]["progress"]["updates"]) >= 1
        server.shutdown(kill_running=True)

    def test_progress_none_for_unknown_id(self, tmp_path: Path) -> None:
        server = AgentServer(
            machine_name="test", repos=["api"],
            state_dir=tmp_path / "state",
        )
        assert server.progress("nonexistent") is None


# ── coord stop CLI ─────────────────────────────────────────────────────────


class TestCoordStop:
    def test_stop_updates_board(self, tmp_path: Path, coord_db) -> None:
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                assignment_id="abc123", status="running",
            ),
        ])
        save_board(board)

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: laptop\n    host: laptop.tailnet\n    repos: [api]\n"
        )

        runner = CliRunner()
        with patch("coord.cli.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, raise_for_status=lambda: None)
            result = runner.invoke(main, [
                "stop", "abc123", "--config", str(config_file),
            ])

        assert result.exit_code == 0
        assert "cancelled" in result.output
        assert "marked failed" in result.output

    def test_stop_unknown_assignment(self, tmp_path: Path, coord_db) -> None:
        save_board(Board())

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )

        runner = CliRunner()
        result = runner.invoke(main, [
            "stop", "nonexistent", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "not found" in result.output


# ── Help text ──────────────────────────────────────────────────────────────


class TestHelpText:
    def test_stop_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["stop", "--help"])
        assert result.exit_code == 0
        assert "Cancel" in result.output
