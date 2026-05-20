"""Tests for coord/usage.py and the 'coord usage' CLI command."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

import coord.state as state_mod
from coord.cli import main
from coord.models import Assignment
from coord.usage import (
    HIGH_BURN_RATE_USD_PER_HOUR,
    AssignmentUsage,
    SessionUsage,
    build_session_usage,
    collect_usage,
    format_burn_rate_line,
    format_usage_report,
    parse_usage_from_log,
)
from coord.worker_events import WorkerSummary, update_summary, WorkerEvent


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_log(path: Path, events: list[dict]) -> Path:
    """Write a stream-json log file containing *events*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def _result_event(**kwargs) -> dict:
    return {"type": "result", **kwargs}


def _init_event(model: str = "claude-sonnet-4-6", session_id: str = "abc") -> dict:
    return {"type": "system", "subtype": "init", "model": model, "session_id": session_id}


def _assignment(
    assignment_id: str = "abc12345",
    repo_name: str = "my-repo",
    issue_number: int = 42,
    issue_title: str = "Fix the bug",
    status: str = "done",
    model: str | None = None,
    dispatched_at: float | None = None,
) -> Assignment:
    return Assignment(
        machine_name="m1",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        assignment_id=assignment_id,
        status=status,
        model=model,
        dispatched_at=dispatched_at,
    )


# ── WorkerSummary token extraction ────────────────────────────────────────────


class TestTokenExtraction:
    """update_summary() should extract token counts from result events."""

    def test_tokens_in_usage_subobject(self) -> None:
        summary = WorkerSummary()
        event = WorkerEvent(
            type="result",
            raw={
                "type": "result",
                "total_cost_usd": 0.10,
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 500,
                },
            },
        )
        update_summary(summary, event)
        assert summary.input_tokens == 1000
        assert summary.output_tokens == 200
        assert summary.cache_creation_tokens == 50
        assert summary.cache_read_tokens == 500

    def test_tokens_at_top_level(self) -> None:
        summary = WorkerSummary()
        event = WorkerEvent(
            type="result",
            raw={
                "type": "result",
                "total_cost_usd": 0.05,
                "input_tokens": 300,
                "output_tokens": 100,
            },
        )
        update_summary(summary, event)
        assert summary.input_tokens == 300
        assert summary.output_tokens == 100

    def test_alt_cache_key_names(self) -> None:
        """usage.cache_creation_tokens and cache_read_tokens as alt names."""
        summary = WorkerSummary()
        event = WorkerEvent(
            type="result",
            raw={
                "type": "result",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_tokens": 20,
                    "cache_read_tokens": 30,
                },
            },
        )
        update_summary(summary, event)
        assert summary.cache_creation_tokens == 20
        assert summary.cache_read_tokens == 30

    def test_missing_tokens_stay_zero(self) -> None:
        summary = WorkerSummary()
        event = WorkerEvent(
            type="result",
            raw={"type": "result", "total_cost_usd": 0.01},
        )
        update_summary(summary, event)
        assert summary.input_tokens == 0
        assert summary.output_tokens == 0
        assert summary.cache_creation_tokens == 0
        assert summary.cache_read_tokens == 0

    def test_to_dict_includes_token_fields(self) -> None:
        summary = WorkerSummary(input_tokens=100, output_tokens=50)
        d = summary.to_dict()
        assert d["input_tokens"] == 100
        assert d["output_tokens"] == 50
        assert d["cache_creation_tokens"] == 0
        assert d["cache_read_tokens"] == 0


# ── parse_usage_from_log ──────────────────────────────────────────────────────


class TestParseUsageFromLog:
    def test_parses_cost_and_tokens(self, tmp_path: Path) -> None:
        log = _make_log(
            tmp_path / "a.log",
            [
                _init_event("claude-haiku-4-5"),
                _result_event(
                    total_cost_usd=0.45,
                    num_turns=12,
                    duration_ms=83_000,
                    usage={
                        "input_tokens": 1000,
                        "output_tokens": 200,
                        "cache_read_input_tokens": 500,
                    },
                ),
            ],
        )
        u = parse_usage_from_log(log)
        assert u is not None
        assert u.total_cost_usd == pytest.approx(0.45)
        assert u.num_turns == 12
        assert u.duration_ms == 83_000
        assert u.model == "claude-haiku-4-5"
        assert u.input_tokens == 1000
        assert u.output_tokens == 200
        assert u.cache_read_tokens == 500

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert parse_usage_from_log(tmp_path / "nope.log") is None

    def test_plain_text_log_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "plain.log"
        p.write_text("just some plain text output\n")
        assert parse_usage_from_log(p) is None


# ── AssignmentUsage helpers ───────────────────────────────────────────────────


class TestAssignmentUsage:
    def test_duration_str_seconds(self) -> None:
        u = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", duration_ms=45_000
        )
        assert u.duration_str() == "45s"

    def test_duration_str_minutes(self) -> None:
        u = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", duration_ms=125_000
        )
        assert u.duration_str() == "2m 5s"

    def test_duration_str_hours(self) -> None:
        u = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", duration_ms=3_661_000
        )
        assert u.duration_str() == "1h 1m"

    def test_duration_str_unknown(self) -> None:
        u = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", duration_ms=None
        )
        assert u.duration_str() == "?"

    def test_total_tokens(self) -> None:
        u = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done",
            input_tokens=300, output_tokens=100
        )
        assert u.total_tokens == 400


# ── SessionUsage calculations ─────────────────────────────────────────────────


class TestSessionUsage:
    def _two_assignment_session(self, started_at: float) -> SessionUsage:
        a1 = AssignmentUsage(
            assignment_id="aaa", repo_name="repo-a", issue_number=1,
            issue_title="t1", status="done", model="claude-sonnet-4-6",
            total_cost_usd=0.50, input_tokens=500, output_tokens=100,
        )
        a2 = AssignmentUsage(
            assignment_id="bbb", repo_name="repo-b", issue_number=2,
            issue_title="t2", status="done", model="claude-haiku-4-5",
            total_cost_usd=0.25, input_tokens=200, output_tokens=50,
        )
        return SessionUsage(started_at=started_at, assignments=[a1, a2])

    def test_total_cost(self) -> None:
        s = self._two_assignment_session(time.time() - 3600)
        assert s.total_cost_usd == pytest.approx(0.75)

    def test_total_tokens(self) -> None:
        s = self._two_assignment_session(time.time() - 3600)
        assert s.total_input_tokens == 700
        assert s.total_output_tokens == 150

    def test_burn_rate_one_hour(self) -> None:
        # Started exactly 1 hour ago → burn rate ≈ total_cost
        s = self._two_assignment_session(time.time() - 3600)
        rate = s.burn_rate_usd_per_hour()
        assert rate is not None
        assert rate == pytest.approx(0.75, abs=0.01)

    def test_burn_rate_no_session_time(self) -> None:
        s = SessionUsage(started_at=None, assignments=[])
        assert s.burn_rate_usd_per_hour() is None

    def test_burn_rate_zero_cost(self) -> None:
        s = SessionUsage(started_at=time.time() - 3600, assignments=[])
        assert s.burn_rate_usd_per_hour() == 0.0

    def test_cost_by_model(self) -> None:
        s = self._two_assignment_session(time.time() - 3600)
        by_model = s.cost_by_model()
        assert by_model["claude-sonnet-4-6"] == pytest.approx(0.50)
        assert by_model["claude-haiku-4-5"] == pytest.approx(0.25)

    def test_count_by_model(self) -> None:
        s = self._two_assignment_session(time.time() - 3600)
        counts = s.count_by_model()
        assert counts["claude-sonnet-4-6"] == 1
        assert counts["claude-haiku-4-5"] == 1

    def test_model_unknown_label(self) -> None:
        a = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", model=None, total_cost_usd=0.10,
        )
        s = SessionUsage(started_at=None, assignments=[a])
        assert "(unknown)" in s.cost_by_model()


# ── collect_usage / build_session_usage ──────────────────────────────────────


class TestCollectUsage:
    def test_reads_local_log(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"
        _make_log(
            logs_dir / "abc123.log",
            [
                _init_event("claude-sonnet-4-6"),
                _result_event(total_cost_usd=0.30, num_turns=5),
            ],
        )
        a = _assignment(assignment_id="abc123", repo_name="r1", issue_number=7)
        result = collect_usage([a], logs_dir=logs_dir)
        assert len(result) == 1
        assert result[0].total_cost_usd == pytest.approx(0.30)
        assert result[0].model == "claude-sonnet-4-6"
        assert result[0].repo_name == "r1"
        assert result[0].issue_number == 7

    def test_falls_back_to_remote_data(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"  # empty — no local log
        a = _assignment(assignment_id="xyz789")
        remote_by_id = {"xyz789": {"total_cost_usd": 0.15, "model_used": "claude-haiku-4-5"}}
        result = collect_usage([a], logs_dir=logs_dir, remote_by_id=remote_by_id)
        assert len(result) == 1
        assert result[0].total_cost_usd == pytest.approx(0.15)
        assert result[0].model == "claude-haiku-4-5"

    def test_skips_assignment_without_id(self) -> None:
        a = _assignment(assignment_id="")
        a.assignment_id = None  # type: ignore[assignment]
        result = collect_usage([a])
        assert result == []

    def test_local_log_takes_precedence_over_remote(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"
        _make_log(
            logs_dir / "id1.log",
            [_init_event("claude-sonnet-4-6"), _result_event(total_cost_usd=0.50)],
        )
        a = _assignment(assignment_id="id1")
        remote_by_id = {"id1": {"total_cost_usd": 0.99}}
        result = collect_usage([a], logs_dir=logs_dir, remote_by_id=remote_by_id)
        # Local log value wins.
        assert result[0].total_cost_usd == pytest.approx(0.50)


class TestBuildSessionUsage:
    def test_derives_started_at_from_dispatch_time(self, tmp_path: Path) -> None:
        t0 = time.time() - 1800  # 30 minutes ago
        a = _assignment(assignment_id="id1", dispatched_at=t0)
        s = build_session_usage([a], logs_dir=tmp_path / "logs")
        assert s.started_at == pytest.approx(t0)

    def test_explicit_started_at_takes_precedence(self, tmp_path: Path) -> None:
        t0 = time.time() - 3600
        t1 = time.time() - 1000
        a = _assignment(assignment_id="id1", dispatched_at=t1)
        s = build_session_usage([a], logs_dir=tmp_path / "logs", started_at=t0)
        assert s.started_at == pytest.approx(t0)

    def test_no_assignments_no_started_at(self, tmp_path: Path) -> None:
        s = build_session_usage([], logs_dir=tmp_path / "logs")
        assert s.started_at is None


# ── format_burn_rate_line ─────────────────────────────────────────────────────


class TestFormatBurnRateLine:
    def _session_with_rate(self, rate_usd_per_hr: float) -> SessionUsage:
        # Set started_at so that elapsed_hours ≈ 1, then set cost accordingly.
        started_at = time.time() - 3600
        total = rate_usd_per_hr * 1.0  # 1 hour elapsed
        a = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", total_cost_usd=total,
        )
        return SessionUsage(started_at=started_at, assignments=[a])

    def test_returns_none_below_threshold(self) -> None:
        s = self._session_with_rate(HIGH_BURN_RATE_USD_PER_HOUR - 0.5)
        assert format_burn_rate_line(s) is None

    def test_returns_line_above_threshold(self) -> None:
        s = self._session_with_rate(HIGH_BURN_RATE_USD_PER_HOUR + 1.0)
        line = format_burn_rate_line(s)
        assert line is not None
        assert "burn rate" in line
        assert "⚠" in line

    def test_returns_none_when_no_session_time(self) -> None:
        s = SessionUsage(started_at=None, assignments=[])
        assert format_burn_rate_line(s) is None


# ── format_usage_report ───────────────────────────────────────────────────────


class TestFormatUsageReport:
    def _session(self) -> SessionUsage:
        a1 = AssignmentUsage(
            assignment_id="abc12345678",
            repo_name="my-repo",
            issue_number=42,
            issue_title="Fix the bug",
            status="done",
            model="claude-sonnet-4-6",
            total_cost_usd=0.45,
            num_turns=12,
            duration_ms=83_000,
            input_tokens=1000,
            output_tokens=200,
        )
        a2 = AssignmentUsage(
            assignment_id="def9876",
            repo_name="other-repo",
            issue_number=38,
            issue_title="Another issue",
            status="running",
            model="claude-haiku-4-5",
            total_cost_usd=0.30,
            num_turns=8,
            duration_ms=None,
        )
        return SessionUsage(
            started_at=time.time() - 3600,
            assignments=[a1, a2],
        )

    def test_contains_session_header(self) -> None:
        report = format_usage_report(self._session())
        assert "Session usage" in report
        assert "$0.75" in report
        assert "burn rate" in report

    def test_contains_per_assignment_section(self) -> None:
        report = format_usage_report(self._session())
        assert "Per-assignment" in report
        assert "abc12345" in report  # truncated to 8 chars
        assert "#42" in report or "42" in report
        assert "my-repo" in report

    def test_contains_per_model_section(self) -> None:
        report = format_usage_report(self._session())
        assert "Per-model" in report
        assert "claude-sonnet-4-6" in report
        assert "claude-haiku-4-5" in report

    def test_contains_token_summary_when_tokens_present(self) -> None:
        report = format_usage_report(self._session())
        assert "Token totals" in report
        assert "1,000" in report  # input tokens formatted with comma

    def test_no_token_summary_when_no_tokens(self) -> None:
        a = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done",
        )
        s = SessionUsage(started_at=None, assignments=[a])
        report = format_usage_report(s)
        assert "Token totals" not in report

    def test_empty_session(self) -> None:
        s = SessionUsage(started_at=None, assignments=[])
        report = format_usage_report(s)
        assert "No assignments found" in report

    def test_high_burn_rate_shows_warning(self) -> None:
        # Set up a session with a very high burn rate.
        a = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done",
            total_cost_usd=HIGH_BURN_RATE_USD_PER_HOUR * 2,  # 2x threshold in 1 hour
        )
        s = SessionUsage(started_at=time.time() - 3600, assignments=[a])
        report = format_usage_report(s)
        assert "⚠" in report


# ── CLI command: coord usage ──────────────────────────────────────────────────


class TestUsageCommand:
    @pytest.fixture
    def coord_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
        """Provide isolated DB + redirect logs dir."""
        import coord.usage as usage_mod
        monkeypatch.setattr(usage_mod, "COORD_DIR", tmp_path)
        monkeypatch.setattr(usage_mod, "LOGS_DIR", tmp_path / "logs")
        return tmp_path

    def _write_board(self, assignments: list[Assignment]) -> None:
        from coord.models import Board
        from coord.state import save_board
        active = [a for a in assignments if a.status in ("running", "pending")]
        completed = [a for a in assignments if a.status in ("done", "failed")]
        save_board(Board(round_number=1, active=active, completed=completed))

    def test_no_board_shows_no_assignments(self, coord_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["usage"])
        assert result.exit_code == 0
        assert "No assignments found" in result.output

    def test_shows_assignment_from_local_log(self, coord_dir: Path) -> None:
        logs_dir = coord_dir / "logs"
        _make_log(
            logs_dir / "myid001.log",
            [
                _init_event("claude-sonnet-4-6"),
                _result_event(total_cost_usd=0.88, num_turns=15, duration_ms=60_000),
            ],
        )
        a = _assignment(assignment_id="myid001", repo_name="cool-repo", issue_number=99, status="done")
        self._write_board([a])

        runner = CliRunner()
        result = runner.invoke(main, ["usage"])
        assert result.exit_code == 0
        assert "$0.88" in result.output
        assert "cool-repo" in result.output
        assert "claude-sonnet-4-6" in result.output

    def test_shows_zero_cost_when_no_log(self, coord_dir: Path) -> None:
        a = _assignment(assignment_id="noLog99", repo_name="r", status="done")
        self._write_board([a])

        runner = CliRunner()
        result = runner.invoke(main, ["usage"])
        assert result.exit_code == 0
        assert "noLog99" in result.output

    def test_per_model_section_present(self, coord_dir: Path) -> None:
        logs_dir = coord_dir / "logs"
        _make_log(
            logs_dir / "id001.log",
            [_init_event("claude-haiku-4-5"), _result_event(total_cost_usd=0.10)],
        )
        a = _assignment(assignment_id="id001", status="done")
        self._write_board([a])

        runner = CliRunner()
        result = runner.invoke(main, ["usage"])
        assert result.exit_code == 0
        assert "Per-model" in result.output
        assert "claude-haiku-4-5" in result.output
