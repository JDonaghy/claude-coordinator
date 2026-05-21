"""Tests for stream-json worker event parsing and summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coord.worker_events import (
    WorkerEvent,
    WorkerSummary,
    detect_anomalies,
    is_stream_json,
    iter_events,
    parse_event,
    parse_log,
    render_event,
    render_log,
    update_summary,
)


# ── Fixture helpers ────────────────────────────────────────────────────────


def _ndjson(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _init_event(model: str = "claude-sonnet-4-6", session_id: str = "abc123") -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "model": model,
    }


def _assistant_text_event(text: str, *, model: str = "claude-sonnet-4-6") -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant_tool_use_event(name: str, tool_input: dict, *, model: str = "claude-sonnet-4-6") -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "content": [{"type": "tool_use", "name": name, "input": tool_input, "id": "tu_1"}],
        },
    }


def _tool_use_event(name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "name": name, "input": tool_input}


def _result_event(**fields) -> dict:
    return {"type": "result", **fields}


# ── parse_event ────────────────────────────────────────────────────────────


class TestParseEvent:
    def test_valid_json_returns_event(self) -> None:
        e = parse_event('{"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"}')
        assert e is not None
        assert e.type == "system"
        assert e.subtype == "init"
        assert e.raw["model"] == "claude-sonnet-4-6"

    def test_invalid_json_returns_none(self) -> None:
        assert parse_event("not json at all") is None

    def test_blank_line_returns_none(self) -> None:
        assert parse_event("") is None
        assert parse_event("   \n") is None

    def test_json_array_returns_none(self) -> None:
        # We only accept top-level objects.
        assert parse_event('["a", "b"]') is None

    def test_json_scalar_returns_none(self) -> None:
        assert parse_event("42") is None

    def test_missing_type_falls_back(self) -> None:
        e = parse_event('{"foo": "bar"}')
        assert e is not None
        assert e.type == "unknown"

    def test_truncated_json_returns_none(self) -> None:
        """A mid-write incomplete JSON line (e.g. last line of a live log) is
        skipped silently — not raised to the caller."""
        assert parse_event('{"type": "assistant", "message": {') is None
        assert parse_event('{"type": "result",') is None
        assert parse_event("{") is None


# ── is_stream_json ─────────────────────────────────────────────────────────


class TestIsStreamJson:
    def test_json_first_line(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text('{"type": "system"}\n')
        assert is_stream_json(p) is True

    def test_plain_text(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text("STATUS: doing stuff\n")
        assert is_stream_json(p) is False

    def test_missing_file(self, tmp_path: Path) -> None:
        assert is_stream_json(tmp_path / "nope.log") is False

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.log"
        p.write_text("")
        assert is_stream_json(p) is False

    def test_skips_leading_comment(self, tmp_path: Path) -> None:
        # Agent prepends a `# argv=...` header — should still be detected.
        p = tmp_path / "log.log"
        p.write_text("# agent=test argv=claude -p\n" + '{"type": "system"}\n')
        assert is_stream_json(p) is True

    def test_plain_text_after_comment(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text("# header\nSTATUS: doing stuff\n")
        assert is_stream_json(p) is False


# ── update_summary / parse_log ─────────────────────────────────────────────


class TestParseLog:
    def test_init_event_extracts_session_and_model(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(_ndjson([_init_event(model="claude-opus-4-7", session_id="sess-1")]))
        summary = parse_log(p)
        assert summary.session_id == "sess-1"
        assert summary.model_used == "claude-opus-4-7"

    def test_assistant_events_counted_as_turns(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_text_event("Let me read the issue..."),
                    _assistant_text_event("I'll edit the file."),
                    _assistant_text_event("Done."),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.num_turns == 3

    def test_bash_command_extracted(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_tool_use_event("Bash", {"command": "git fetch origin"}),
                    _assistant_tool_use_event("Bash", {"command": "git status"}),
                ]
            )
        )
        summary = parse_log(p)
        assert "git fetch origin" in summary.bash_commands
        assert "git status" in summary.bash_commands
        assert summary.last_tool == "Bash"

    def test_edit_file_path_extracted(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_tool_use_event(
                        "Edit", {"file_path": "coord/cli.py", "old_string": "a", "new_string": "b"}
                    ),
                    _assistant_tool_use_event("Write", {"file_path": "tests/test_new.py", "content": "x"}),
                ]
            )
        )
        summary = parse_log(p)
        assert "coord/cli.py" in summary.files_edited
        assert "tests/test_new.py" in summary.files_edited

    def test_top_level_tool_use_event(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _tool_use_event("Bash", {"command": "ls -la"}),
                    _tool_use_event("Edit", {"file_path": "README.md"}),
                ]
            )
        )
        summary = parse_log(p)
        assert "ls -la" in summary.bash_commands
        assert "README.md" in summary.files_edited

    def test_result_event_extracts_cost_and_stop(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_text_event("hi"),
                    _result_event(
                        total_cost_usd=0.234,
                        stop_reason="end_turn",
                        num_turns=6,
                        duration_ms=252000,
                        permission_denials=[],
                    ),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.total_cost_usd == pytest.approx(0.234)
        assert summary.stop_reason == "end_turn"
        assert summary.num_turns == 6
        assert summary.duration_ms == 252000

    def test_result_event_extracts_permission_denials(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _result_event(
                        total_cost_usd=0.01,
                        stop_reason="end_turn",
                        permission_denials=["Bash(rm -rf /)"],
                    ),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.permission_denials == ["Bash(rm -rf /)"]

    def test_rate_limit_event_sets_flag(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    {"type": "rate_limit_event", "resets_at": 1716160000.0},
                ]
            )
        )
        summary = parse_log(p)
        assert summary.rate_limited is True
        assert summary.rate_limit_resets_at == pytest.approx(1716160000.0)

    def test_missing_file_returns_empty_summary(self, tmp_path: Path) -> None:
        summary = parse_log(tmp_path / "nope.log")
        assert summary.num_turns == 0
        assert summary.session_id is None

    def test_skips_unparseable_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        # Header comment plus mix of valid/invalid lines.
        p.write_text(
            "# agent header\n"
            "not json\n"
            + json.dumps(_init_event())
            + "\n"
            + json.dumps(_assistant_text_event("hello"))
            + "\n"
        )
        summary = parse_log(p)
        assert summary.session_id == "abc123"
        assert summary.num_turns == 1


# ── iter_events ────────────────────────────────────────────────────────────


class TestIterEvents:
    def test_yields_events(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(_ndjson([_init_event(), _assistant_text_event("hi")]))
        events = list(iter_events(p))
        assert len(events) == 2
        assert events[0].type == "system"
        assert events[1].type == "assistant"

    def test_tail_read(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        # Write a lot of events.
        events = [_init_event()] + [_assistant_text_event(f"turn {i}") for i in range(100)]
        p.write_text(_ndjson(events))
        # Tail with small budget — should still parse the tail without crashing.
        tail_events = list(iter_events(p, tail_bytes=256))
        assert len(tail_events) > 0
        assert all(isinstance(e, WorkerEvent) for e in tail_events)


# ── render_event / render_log ──────────────────────────────────────────────


class TestRender:
    def test_init_renders(self) -> None:
        e = parse_event(json.dumps(_init_event(model="claude-sonnet-4-6", session_id="abc")))
        out = render_event(e)
        assert "init" in out
        assert "claude-sonnet-4-6" in out
        assert "abc" in out

    def test_assistant_text_renders_with_turn_counter(self) -> None:
        e1 = parse_event(json.dumps(_assistant_text_event("Let me read the issue...")))
        e2 = parse_event(json.dumps(_assistant_text_event("I'll add the new command")))
        counter = [0]
        out1 = render_event(e1, turn_counter=counter)
        out2 = render_event(e2, turn_counter=counter)
        assert "Turn 1" in out1
        assert "Turn 2" in out2
        assert "Let me read the issue" in out1

    def test_bash_tool_use_renders(self) -> None:
        e = parse_event(json.dumps(_tool_use_event("Bash", {"command": "git push origin HEAD"})))
        out = render_event(e)
        assert "Bash" in out
        assert "git push origin HEAD" in out

    def test_edit_tool_use_renders(self) -> None:
        e = parse_event(json.dumps(_tool_use_event("Edit", {"file_path": "coord/cli.py"})))
        out = render_event(e)
        assert "Edit" in out
        assert "coord/cli.py" in out

    def test_result_renders_summary_line(self) -> None:
        e = parse_event(
            json.dumps(
                _result_event(
                    total_cost_usd=0.23,
                    stop_reason="end_turn",
                    num_turns=6,
                    duration_ms=252000,
                )
            )
        )
        out = render_event(e)
        assert "result" in out
        assert "0.23" in out
        assert "6" in out
        assert "end_turn" in out

    def test_render_log_walks_file(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_text_event("Let me read the issue..."),
                    _assistant_tool_use_event("Bash", {"command": "git fetch origin"}),
                    _result_event(total_cost_usd=0.1, stop_reason="end_turn", num_turns=2, duration_ms=1000),
                ]
            )
        )
        lines = list(render_log(p))
        assert any("init" in l for l in lines)
        assert any("Turn 1" in l for l in lines)
        # The assistant turn carries a tool_use rather than text, so it
        # should be summarised as either text or tool_use=Bash.
        assert any("result" in l for l in lines)


# ── detect_anomalies ───────────────────────────────────────────────────────


class TestDetectAnomalies:
    def test_repeated_bash_command_flagged(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        events = [_init_event()] + [
            _tool_use_event("Bash", {"command": "make test"}) for _ in range(3)
        ]
        p.write_text(_ndjson(events))
        warnings = detect_anomalies(p)
        assert any("repeated" in w and "make test" in w for w in warnings)

    def test_rate_limit_flagged(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    {"type": "rate_limit_event", "resets_at": 1716160000.0},
                ]
            )
        )
        warnings = detect_anomalies(p)
        assert any("rate limited" in w for w in warnings)

    def test_permission_denials_flagged(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _result_event(
                        total_cost_usd=0.0,
                        permission_denials=["Bash(rm -rf /)"],
                    ),
                ]
            )
        )
        warnings = detect_anomalies(p)
        assert any("permission denials" in w for w in warnings)

    def test_many_turns_no_commit_flagged(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        events = [_init_event()] + [
            _assistant_text_event(f"thinking {i}") for i in range(20)
        ]
        p.write_text(_ndjson(events))
        warnings = detect_anomalies(p)
        assert any("turns without a git commit" in w for w in warnings)

    def test_no_anomalies_for_healthy_run(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_text_event("hello"),
                    _tool_use_event("Bash", {"command": "git commit -m 'feat: x'"}),
                    _result_event(total_cost_usd=0.05, stop_reason="end_turn", num_turns=1),
                ]
            )
        )
        assert detect_anomalies(p) == []


# ── WorkerSummary ──────────────────────────────────────────────────────────


class TestWorkerSummary:
    def test_to_dict_round_trips(self) -> None:
        s = WorkerSummary(session_id="abc", num_turns=3, total_cost_usd=0.5)
        d = s.to_dict()
        assert d["session_id"] == "abc"
        assert d["num_turns"] == 3
        assert d["total_cost_usd"] == 0.5

    def test_update_summary_in_place(self) -> None:
        s = WorkerSummary()
        e = parse_event(json.dumps(_init_event(model="claude-sonnet-4-6", session_id="zzz")))
        update_summary(s, e)
        assert s.session_id == "zzz"
        assert s.model_used == "claude-sonnet-4-6"
