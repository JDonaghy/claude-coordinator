"""Tests for #846 — detect & flag long-running / non-converging assignments.

Mirrors ``tests/test_stuck.py``'s fixtures/shape for
``coord.notify.detect_needs_attention`` / ``post_needs_attention`` /
``attention_signal``, the wall-clock + non-convergence counterpart to
``detect_stuck`` (self-reported ``STUCK:`` lines).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coord import notify as notify_mod
from coord import state as state_mod
from coord.comments import EVENT_NEEDS_ATTENTION, format_needs_attention
from coord.config import Config, PipelineConfig
from coord.models import Assignment, Board, Machine, Proposal, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
        ],
        pipeline=PipelineConfig(
            attention_thresholds={"work": 60.0},
            convergence_rounds=3,
        ),
    )


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db) -> Path:
    return tmp_path / "state"


def _record(coord_dir: Path, assignment_id: str, machine: str = "laptop") -> None:
    proposal = Proposal(
        id=1,
        machine_name=machine,
        repo_name="api",
        issue_number=42,
        issue_title="Add feature X",
        rationale="r",
        files_likely=["src/a.py"],
        briefing="b",
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github="acme/api",
    )


def _bump_review_iteration(assignment_id: str, review_iteration: int) -> None:
    """Set review_iteration on an already-dispatched row without disturbing
    dispatched_at/repo_github (save_board's upsert doesn't touch either on
    conflict — see coord.state._UPSERT_SQL)."""
    board = Board(
        active=[
            Assignment(
                assignment_id=assignment_id,
                machine_name="laptop",
                repo_name="api",
                issue_number=42,
                issue_title="Add feature X",
                status="running",
                type="work",
                review_iteration=review_iteration,
            )
        ],
        completed=[],
    )
    state_mod.save_board(board)


# ── attention_signal (pure core) ────────────────────────────────────────────


class TestAttentionSignal:
    def test_not_running_never_flags(self, config: Config) -> None:
        reason, detail = notify_mod.attention_signal(
            assignment_type="work", status="done", dispatched_at=0.0,
            review_iteration=99, config=config, now=100000.0,
        )
        assert (reason, detail) == (None, None)

    def test_under_wall_clock_threshold_no_flag(self, config: Config) -> None:
        reason, _ = notify_mod.attention_signal(
            assignment_type="work", status="running", dispatched_at=1000.0,
            review_iteration=0, config=config, now=1010.0,
        )
        assert reason is None

    def test_over_wall_clock_threshold_flags(self, config: Config) -> None:
        reason, detail = notify_mod.attention_signal(
            assignment_type="work", status="running", dispatched_at=1000.0,
            review_iteration=0, config=config, now=1000.0 + 61.0,
        )
        assert reason == "wall_clock"
        assert detail

    def test_non_convergence_takes_priority_over_wall_clock(self, config: Config) -> None:
        # Under threshold on wall-clock, but already thrashing — still flags.
        reason, _ = notify_mod.attention_signal(
            assignment_type="work", status="running", dispatched_at=1000.0,
            review_iteration=3, config=config, now=1001.0,
        )
        assert reason == "non_convergence"

    def test_unknown_type_falls_back_to_work_threshold(self, config: Config) -> None:
        reason, _ = notify_mod.attention_signal(
            assignment_type="mock-author", status="running", dispatched_at=1000.0,
            review_iteration=0, config=config, now=1000.0 + 61.0,
        )
        assert reason == "wall_clock"


# ── detect_needs_attention ──────────────────────────────────────────────────


class TestDetectNeedsAttention:
    def test_no_dispatched_returns_empty(self, coord_dir: Path, config: Config) -> None:
        assert notify_mod.detect_needs_attention(config) == []

    def test_fresh_dispatch_not_flagged(self, coord_dir: Path, config: Config) -> None:
        _record(coord_dir, "abc123")
        # now == dispatch time (fixture default), well under the 60s threshold.
        assert notify_mod.detect_needs_attention(config) == []

    def test_wall_clock_over_threshold_flags(self, coord_dir: Path, config: Config) -> None:
        import time as _time

        _record(coord_dir, "abc123")
        results = notify_mod.detect_needs_attention(config, now=_time.time() + 3600)
        assert len(results) == 1
        detection, record = results[0]
        assert detection.assignment_id == "abc123"
        assert detection.reason == "wall_clock"
        assert detection.repo_name == "api"
        assert detection.issue_number == 42
        assert record["repo_github"] == "acme/api"

    def test_non_convergence_flags_regardless_of_wall_clock(
        self, coord_dir: Path, config: Config
    ) -> None:
        _record(coord_dir, "abc123")
        _bump_review_iteration("abc123", 3)
        results = notify_mod.detect_needs_attention(config)
        assert len(results) == 1
        assert results[0][0].reason == "non_convergence"

    def test_already_notified_needs_attention_not_returned(
        self, coord_dir: Path, config: Config
    ) -> None:
        import time as _time

        _record(coord_dir, "abc123")
        state_mod.mark_notified("abc123:needs-attention", EVENT_NEEDS_ATTENTION)
        results = notify_mod.detect_needs_attention(config, now=_time.time() + 3600)
        assert results == []

    def test_terminal_assignment_not_flagged_by_completion_notify(
        self, coord_dir: Path, config: Config
    ) -> None:
        """An assignment already notified as completed should not be scanned
        (mirrors detect_stuck's completed-assignment exclusion)."""
        import time as _time

        _record(coord_dir, "abc123")
        state_mod.mark_notified("abc123", "completion")
        results = notify_mod.detect_needs_attention(config, now=_time.time() + 3600)
        assert results == []

    def test_no_double_notify_across_two_runs(self, coord_dir: Path, config: Config) -> None:
        import time as _time

        _record(coord_dir, "abc123")
        later = _time.time() + 3600
        first = notify_mod.detect_needs_attention(config, now=later)
        assert len(first) == 1
        with patch.object(notify_mod, "github_ops") as mock_gh:
            notify_mod.post_needs_attention(*first[0])
            assert mock_gh.post_issue_comment.called
        second = notify_mod.detect_needs_attention(config, now=later)
        assert second == []


# ── format_needs_attention ───────────────────────────────────────────────────


class TestFormatNeedsAttention:
    def test_wall_clock_reason_renders_running_too_long(self) -> None:
        body = format_needs_attention(
            assignment_id="abc-123",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            reason="wall_clock",
            detail="Running 52m, past the 45m threshold for type='work'.",
        )
        assert "abc-123" in body
        assert "#42" in body
        assert "Running too long" in body
        assert "52m" in body
        assert f"<!-- coord:event={EVENT_NEEDS_ATTENTION}" in body

    def test_non_convergence_reason_renders_not_converging(self) -> None:
        body = format_needs_attention(
            assignment_id="abc-123",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            reason="non_convergence",
            detail="4 fix/review round(s)...",
        )
        assert "Not converging" in body
