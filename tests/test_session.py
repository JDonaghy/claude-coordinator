"""Tests for session state persistence (coord/state.py session helpers + coord session command)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord.cli import main
from coord.state import load_session, write_session_end, write_session_start


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def session_dir(coord_db):
    """Provide an isolated in-memory DB for session tests."""
    return None


# ── Unit tests for state helpers ──────────────────────────────────────────────


class TestLoadSession:
    def test_returns_none_when_no_session(self, session_dir) -> None:
        assert load_session() is None

    def test_returns_data_after_write_start(self, session_dir) -> None:
        write_session_start()
        data = load_session()
        assert data is not None
        assert data["clean_shutdown"] is False
        assert "started_at" in data

    def test_returns_none_on_empty_db(self, session_dir) -> None:
        # No sessions inserted — should return None
        assert load_session() is None


class TestWriteSessionStart:
    def test_creates_session_with_clean_shutdown_false(self, session_dir) -> None:
        write_session_start()
        data = load_session()
        assert data is not None
        assert data["clean_shutdown"] is False
        assert "started_at" in data
        assert data["started_at"].endswith("Z")

    def test_write_session_start_works(self, session_dir) -> None:
        """write_session_start should work without crashing."""
        write_session_start()
        data = load_session()
        assert data is not None
        assert data["clean_shutdown"] is False


class TestWriteSessionEnd:
    def test_creates_session_with_clean_shutdown_true(self, session_dir) -> None:
        write_session_start()
        write_session_end(
            completed_ids=["abc-1", "abc-2"],
            issues_closed=[10, 20],
            total_cost_usd=1.23,
        )
        data = load_session()
        assert data is not None
        assert data["clean_shutdown"] is True
        assert data["completed_this_session"] == ["abc-1", "abc-2"]
        assert data["issues_closed"] == [10, 20]
        assert data["total_cost_usd"] == pytest.approx(1.23)
        assert "ended_at" in data
        assert data["ended_at"].endswith("Z")

    def test_preserves_started_at(self, session_dir) -> None:
        write_session_start()
        original_started = load_session()["started_at"]  # type: ignore[index]
        write_session_end(
            completed_ids=[],
            issues_closed=[],
            total_cost_usd=0.0,
        )
        data = load_session()
        assert data is not None
        assert data["started_at"] == original_started

    def test_works_without_prior_session_start(self, session_dir) -> None:
        """write_session_end should not crash even if no session was started."""
        write_session_end(
            completed_ids=["x1"],
            issues_closed=[5],
            total_cost_usd=0.5,
        )
        data = load_session()
        assert data is not None
        assert data["clean_shutdown"] is True
        assert data["started_at"] is None

    def test_empty_stats(self, session_dir) -> None:
        write_session_start()
        write_session_end(completed_ids=[], issues_closed=[], total_cost_usd=0.0)
        data = load_session()
        assert data is not None
        assert data["completed_this_session"] == []
        assert data["issues_closed"] == []
        assert data["total_cost_usd"] == 0.0


class TestSessionStartIdempotency:
    def test_does_not_overwrite_in_progress_session(self, session_dir) -> None:
        """The CLI pattern prevents overwriting an in-progress session."""
        write_session_start()
        data_after_first = load_session()
        assert data_after_first is not None
        started_at_first = data_after_first["started_at"]

        # Replicate the CLI conditional — should NOT call write_session_start again
        session = load_session()
        if session is None or session.get("clean_shutdown", True):
            write_session_start()

        data_after_second = load_session()
        assert data_after_second is not None
        # started_at must be unchanged since the second call was skipped
        assert data_after_second["started_at"] == started_at_first

    def test_overwrites_after_clean_shutdown(self, session_dir) -> None:
        """After a clean shutdown, the next dispatch starts a fresh session."""
        write_session_start()
        write_session_end(completed_ids=[], issues_closed=[], total_cost_usd=0.0)

        data_ended = load_session()
        assert data_ended is not None
        assert data_ended["clean_shutdown"] is True

        # Replicate the CLI conditional — SHOULD call write_session_start
        session = load_session()
        if session is None or session.get("clean_shutdown", True):
            write_session_start()

        data_new = load_session()
        assert data_new is not None
        assert data_new["clean_shutdown"] is False
        # A new session was started — no ended_at
        assert "ended_at" not in data_new


# ── CLI command tests ─────────────────────────────────────────────────────────


class TestSessionCommand:
    def _invoke(self) -> str:
        """Invoke `coord session`."""
        runner = CliRunner()
        result = runner.invoke(main, ["session"])
        return result.output

    def test_no_session_shows_not_found(self, session_dir) -> None:
        output = self._invoke()
        assert "No session state found" in output
        assert "coord assign" in output

    def test_active_session_shows_in_progress(self, session_dir) -> None:
        write_session_start()
        output = self._invoke()
        assert "in progress" in output
        assert "clean_shutdown: false" in output
        assert "coord resume" in output

    def test_clean_session_shows_summary(self, session_dir) -> None:
        write_session_start()
        write_session_end(
            completed_ids=["a1", "a2", "a3"],
            issues_closed=[1, 2],
            total_cost_usd=4.56,
        )
        output = self._invoke()
        assert "Last session:" in output
        assert "3 assignments" in output
        assert "2 issues" in output
        assert "$4.56" in output
