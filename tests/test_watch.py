"""Tests for `coord watch` and format_important_event()."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.worker_events import WorkerEvent, format_important_event


# ── Helpers ─────────────────────────────────────────────────────────────────


def _ndjson(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _write_log(log_path: Path, events: list[dict]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(_ndjson(events))


ASSIGNMENT_ID = "watch-test-001"

MINIMAL_CONFIG = """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
"""


# ── format_important_event unit tests ────────────────────────────────────────


class TestFormatImportantEvent:
    def _event(self, data: dict) -> WorkerEvent:
        return WorkerEvent(
            type=data.get("type", "unknown"),
            subtype=data.get("subtype"),
            raw=data,
        )

    def test_init_event(self) -> None:
        e = self._event(
            {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6",
             "session_id": "abcdefgh1234"}
        )
        out = format_important_event(e)
        assert out is not None
        assert "[init]" in out
        assert "claude-sonnet-4-6" in out
        assert "abcdefgh" in out  # first 8 chars of session_id

    def test_init_event_truncates_session(self) -> None:
        e = self._event(
            {"type": "system", "subtype": "init", "model": "m",
             "session_id": "AAABBBCCCDDDEEE"}
        )
        out = format_important_event(e)
        assert out is not None
        assert "AAABBBCC" in out
        assert "DDDEEE" not in out

    def test_system_non_init_not_important(self) -> None:
        e = self._event({"type": "system", "subtype": "something_else"})
        assert format_important_event(e) is None

    def test_rate_limit_throttled(self) -> None:
        e = self._event(
            {"type": "rate_limit_event",
             "rate_limit_info": {"status": "throttled", "resetsAt": 1716160000}}
        )
        out = format_important_event(e)
        assert out is not None
        assert "[rate_limit]" in out
        assert "throttled" in out
        assert "1716160000" in out

    def test_rate_limit_allowed_not_surfaced(self) -> None:
        e = self._event(
            {"type": "rate_limit_event",
             "rate_limit_info": {"status": "allowed"}}
        )
        assert format_important_event(e) is None

    def test_rate_limit_no_info_surfaces(self) -> None:
        """rate_limit_event without rate_limit_info sub-object is always notable."""
        e = self._event({"type": "rate_limit_event", "resets_at": 99999})
        out = format_important_event(e)
        assert out is not None
        assert "[rate_limit]" in out

    def test_result_completed(self) -> None:
        e = self._event(
            {"type": "result", "is_error": False, "duration_ms": 90000,
             "num_turns": 12, "total_cost_usd": 0.42, "stop_reason": "end_turn"}
        )
        out = format_important_event(e)
        assert out is not None
        assert "[result]" in out
        assert "completed" in out
        assert "1m 30s" in out
        assert "12 turns" in out
        assert "$0.42" in out
        assert "end_turn" in out

    def test_result_failed(self) -> None:
        e = self._event(
            {"type": "result", "is_error": True, "duration_ms": 5000,
             "num_turns": 3, "total_cost_usd": 0.05, "stop_reason": "error"}
        )
        out = format_important_event(e)
        assert out is not None
        assert "failed" in out

    def test_result_with_permission_denials_string(self) -> None:
        e = self._event(
            {"type": "result", "is_error": False, "duration_ms": 1000,
             "num_turns": 1, "total_cost_usd": 0.0, "stop_reason": "end_turn",
             "permission_denials": ["Bash: not allowed"]}
        )
        out = format_important_event(e)
        assert out is not None
        assert "[denied]" in out
        assert "Bash" in out

    def test_result_with_permission_denials_dict(self) -> None:
        e = self._event(
            {"type": "result", "is_error": False, "duration_ms": 1000,
             "num_turns": 1, "total_cost_usd": 0.0, "stop_reason": "end_turn",
             "permission_denials": [{"tool_name": "Edit", "reason": "read-only mode"}]}
        )
        out = format_important_event(e)
        assert out is not None
        assert "[denied]" in out
        assert "Edit" in out
        assert "read-only mode" in out

    def test_stuck_in_assistant_text(self) -> None:
        e = self._event(
            {"type": "assistant",
             "message": {"content": [
                 {"type": "text",
                  "text": "I tried two approaches.\nSTUCK: cannot find the module\nGiving up."}
             ]}}
        )
        out = format_important_event(e)
        assert out is not None
        assert "[stuck]" in out
        assert "cannot find the module" in out

    def test_assistant_without_stuck_not_important(self) -> None:
        e = self._event(
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "Working on it..."}]}}
        )
        assert format_important_event(e) is None

    def test_tool_use_not_important(self) -> None:
        e = self._event(
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
        )
        assert format_important_event(e) is None

    def test_tool_result_not_important(self) -> None:
        e = self._event({"type": "tool_result", "tool_use_id": "x"})
        assert format_important_event(e) is None


# ── coord watch CLI tests ─────────────────────────────────────────────────────


class TestWatchCommand:
    """Tests for `coord watch`. All tests write the full log upfront so that
    CliRunner (synchronous) sees all events in one pass and exits on the
    result event without actually blocking."""

    def _setup(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(MINIMAL_CONFIG)

        dispatched_file = tmp_path / "dispatched.json"
        dispatched_file.write_text(
            json.dumps([{
                "assignment_id": ASSIGNMENT_ID,
                "machine_name": "laptop",
                "repo_name": "api",
                "repo_github": "acme/api",
                "issue_number": 1,
            }])
        )

        # watch looks for: DEFAULT_STATE_DIR / "logs" / f"{assignment_id}.log"
        # We set DEFAULT_STATE_DIR = tmp_path, so logs live at tmp_path/logs/
        logs_dir = tmp_path / "logs"
        log_file = logs_dir / f"{ASSIGNMENT_ID}.log"
        return config_file, dispatched_file, log_file

    def _invoke(
        self,
        tmp_path: Path,
        config_file: Path,
        dispatched_file: Path,
        log_file: Path,
        extra_args: list[str] | None = None,
    ):
        runner = CliRunner()
        args = ["watch", ASSIGNMENT_ID, "--config", str(config_file)]
        if extra_args:
            args += extra_args

        from coord import cli as _cli_mod

        def _non_blocking_tail(path, interval=1.0):
            """Read lines synchronously without blocking after EOF."""
            with open(path) as f:
                for line in f:
                    yield line.rstrip("\n")

        # Patch DEFAULT_STATE_DIR in coord.agent so that the local import
        # inside watch() picks up the tmp_path-based directory.
        # Also patch _tail_log so we don't actually block waiting for new lines.
        with (
            patch("coord.state.DISPATCHED_FILE", dispatched_file),
            patch("coord.agent.DEFAULT_STATE_DIR", tmp_path),
            patch.object(_cli_mod, "_tail_log", new=_non_blocking_tail),
        ):
            result = runner.invoke(main, args)

        return result

    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["watch", "--help"])
        assert result.exit_code == 0
        assert "Watch" in result.output or "watch" in result.output.lower()

    def test_missing_assignment_exits_2(self, tmp_path: Path) -> None:
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(MINIMAL_CONFIG)
        dispatched_file = tmp_path / "dispatched.json"
        dispatched_file.write_text("[]")

        runner = CliRunner()
        with patch("coord.state.DISPATCHED_FILE", dispatched_file):
            result = runner.invoke(
                main, ["watch", "nonexistent-id", "--config", str(config_file)]
            )
        assert result.exit_code == 2
        assert "not found" in result.output

    def test_happy_path_shows_important_events(self, tmp_path: Path) -> None:
        config_file, dispatched_file, log_file = self._setup(tmp_path)
        _write_log(log_file, [
            {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6",
             "session_id": "sess1234abcd"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Starting work..."}
            ]}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "result", "is_error": False, "duration_ms": 60000,
             "num_turns": 5, "total_cost_usd": 0.10, "stop_reason": "end_turn"},
        ])

        result = self._invoke(tmp_path, config_file, dispatched_file, log_file)
        assert result.exit_code == 0
        assert "[init]" in result.output
        assert "claude-sonnet-4-6" in result.output
        assert "[result]" in result.output
        assert "completed" in result.output
        # Tool use is NOT important
        assert "Bash" not in result.output
        # Non-STUCK assistant text is NOT important
        assert "Starting work" not in result.output

    def test_result_event_exits_command(self, tmp_path: Path) -> None:
        config_file, dispatched_file, log_file = self._setup(tmp_path)
        _write_log(log_file, [
            {"type": "system", "subtype": "init", "model": "m", "session_id": "s"},
            {"type": "result", "is_error": False, "duration_ms": 1000,
             "num_turns": 1, "total_cost_usd": 0.01, "stop_reason": "end_turn"},
            # Any lines after result should not be processed
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "PHANTOM: should not appear"}
            ]}},
        ])

        result = self._invoke(tmp_path, config_file, dispatched_file, log_file)
        assert result.exit_code == 0
        assert "PHANTOM" not in result.output

    def test_exit_code_1_on_failure(self, tmp_path: Path) -> None:
        config_file, dispatched_file, log_file = self._setup(tmp_path)
        _write_log(log_file, [
            {"type": "result", "is_error": True, "duration_ms": 2000,
             "num_turns": 2, "total_cost_usd": 0.02, "stop_reason": "error"},
        ])

        result = self._invoke(tmp_path, config_file, dispatched_file, log_file)
        assert result.exit_code == 1
        assert "failed" in result.output

    def test_show_all_shows_every_event(self, tmp_path: Path) -> None:
        config_file, dispatched_file, log_file = self._setup(tmp_path)
        _write_log(log_file, [
            {"type": "system", "subtype": "init", "model": "m", "session_id": "s"},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Doing stuff"}
            ]}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}},
            {"type": "result", "is_error": False, "duration_ms": 1000,
             "num_turns": 1, "total_cost_usd": 0.01, "stop_reason": "end_turn"},
        ])

        result = self._invoke(
            tmp_path, config_file, dispatched_file, log_file,
            extra_args=["--all"],
        )
        assert result.exit_code == 0
        # --all should show the assistant and tool_use lines too
        assert "[assistant]" in result.output
        assert "[tool]" in result.output or "Bash" in result.output

    def test_rate_limit_event_surfaced(self, tmp_path: Path) -> None:
        config_file, dispatched_file, log_file = self._setup(tmp_path)
        _write_log(log_file, [
            {"type": "system", "subtype": "init", "model": "m", "session_id": "s"},
            {"type": "rate_limit_event",
             "rate_limit_info": {"status": "throttled", "resetsAt": 9999}},
            {"type": "result", "is_error": False, "duration_ms": 1000,
             "num_turns": 1, "total_cost_usd": 0.01, "stop_reason": "end_turn"},
        ])

        result = self._invoke(tmp_path, config_file, dispatched_file, log_file)
        assert result.exit_code == 0
        assert "[rate_limit]" in result.output
        assert "throttled" in result.output

    def test_stuck_in_assistant_surfaced(self, tmp_path: Path) -> None:
        config_file, dispatched_file, log_file = self._setup(tmp_path)
        _write_log(log_file, [
            {"type": "system", "subtype": "init", "model": "m", "session_id": "s"},
            {"type": "assistant", "message": {"content": [
                {"type": "text",
                 "text": "I tried everything.\nSTUCK: cannot find the config file\nAbort."}
            ]}},
            {"type": "result", "is_error": True, "duration_ms": 5000,
             "num_turns": 3, "total_cost_usd": 0.05, "stop_reason": "error"},
        ])

        result = self._invoke(tmp_path, config_file, dispatched_file, log_file)
        assert result.exit_code == 1
        assert "[stuck]" in result.output
        assert "cannot find the config file" in result.output

    def test_permission_denials_in_result_surfaced(self, tmp_path: Path) -> None:
        config_file, dispatched_file, log_file = self._setup(tmp_path)
        _write_log(log_file, [
            {"type": "result", "is_error": False, "duration_ms": 1000,
             "num_turns": 1, "total_cost_usd": 0.01, "stop_reason": "end_turn",
             "permission_denials": ["Bash: execution not allowed"]},
        ])

        result = self._invoke(tmp_path, config_file, dispatched_file, log_file)
        assert result.exit_code == 0
        assert "[denied]" in result.output
        assert "Bash" in result.output

    def test_init_event_model_and_session(self, tmp_path: Path) -> None:
        config_file, dispatched_file, log_file = self._setup(tmp_path)
        _write_log(log_file, [
            {"type": "system", "subtype": "init",
             "model": "claude-opus-4-5",
             "session_id": "XYZXYZXYZXYZ"},
            {"type": "result", "is_error": False, "duration_ms": 1000,
             "num_turns": 1, "total_cost_usd": 0.01, "stop_reason": "end_turn"},
        ])

        result = self._invoke(tmp_path, config_file, dispatched_file, log_file)
        assert result.exit_code == 0
        assert "claude-opus-4-5" in result.output
        assert "XYZXYZXY" in result.output  # first 8 chars

    def test_header_comment_line_skipped_in_filtered_mode(self, tmp_path: Path) -> None:
        config_file, dispatched_file, log_file = self._setup(tmp_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(
            "# agent=coord argv=claude -p\n"
            + _ndjson([
                {"type": "result", "is_error": False, "duration_ms": 1000,
                 "num_turns": 1, "total_cost_usd": 0.01, "stop_reason": "end_turn"},
            ])
        )

        result = self._invoke(tmp_path, config_file, dispatched_file, log_file)
        assert result.exit_code == 0, result.output
        # Header comment should NOT appear in filtered mode
        assert "# agent" not in result.output

    def test_header_comment_shown_in_all_mode(self, tmp_path: Path) -> None:
        config_file, dispatched_file, log_file = self._setup(tmp_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(
            "# agent=coord argv=claude -p\n"
            + _ndjson([
                {"type": "result", "is_error": False, "duration_ms": 1000,
                 "num_turns": 1, "total_cost_usd": 0.01, "stop_reason": "end_turn"},
            ])
        )

        result = self._invoke(
            tmp_path, config_file, dispatched_file, log_file,
            extra_args=["--all"],
        )
        assert result.exit_code == 0
        assert "# agent" in result.output
