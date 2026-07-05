"""Tests for #874: completion_summary capture and persistence.

Covers:
* ``coord.progress._extract_completion_summary_from_text`` — regex logic
* ``coord.progress.parse_completion_summary_from_log`` — file-based extraction
* ``coord.notify._capture_completion_summary`` — both prose-present and
  prose-absent paths (the acceptance bar for this issue).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── _extract_completion_summary_from_text ──────────────────────────────────


class TestExtractCompletionSummaryFromText:
    """Unit tests for the pure-function text extractor."""

    def _extract(self, text: str):
        from coord.progress import _extract_completion_summary_from_text
        return _extract_completion_summary_from_text(text)

    def test_extracts_prose_under_heading(self) -> None:
        text = (
            "Some worker output here.\n"
            "### Summary\n"
            "Implemented the completion_summary field and wired it into notify.\n"
            "\n"
            "SMOKE_TESTS:\n"
            "- Scenario — trigger — signal\n"
            "END_SMOKE_TESTS\n"
        )
        result = self._extract(text)
        assert result is not None
        assert "completion_summary" in result
        assert "wired it into notify" in result

    def test_no_summary_heading_returns_none(self) -> None:
        text = (
            "STATUS: build passed → running tests → confidence: high\n"
            "SMOKE_TESTS:\n"
            "- item — trigger — signal\n"
            "END_SMOKE_TESTS\n"
        )
        result = self._extract(text)
        assert result is None

    def test_empty_summary_body_returns_none(self) -> None:
        """An empty or whitespace-only body under ### Summary is treated as absent."""
        text = "### Summary\n\n### Next section\n"
        result = self._extract(text)
        assert result is None

    def test_picks_last_block_when_multiple(self) -> None:
        """Workers may re-emit their summary after further edits — last wins."""
        text = (
            "### Summary\n"
            "First attempt summary.\n"
            "\n"
            "### Summary\n"
            "Revised final summary.\n"
        )
        result = self._extract(text)
        assert result == "Revised final summary."

    def test_strips_leading_trailing_whitespace(self) -> None:
        text = "### Summary\n\n  Trimmed.  \n\n### Next\n"
        result = self._extract(text)
        assert result == "Trimmed."

    def test_multiline_prose_preserved(self) -> None:
        text = (
            "### Summary\n"
            "Line one of the summary.\n"
            "Line two of the summary.\n"
            "\n"
            "Still the summary paragraph.\n"
        )
        result = self._extract(text)
        assert result is not None
        assert "Line one" in result
        assert "Line two" in result
        assert "Still the summary paragraph" in result

    def test_stops_before_next_heading(self) -> None:
        text = (
            "### Summary\n"
            "Just the summary.\n"
            "### SMOKE_TESTS\n"
            "Should not be included.\n"
        )
        result = self._extract(text)
        assert result is not None
        assert "Just the summary." in result
        assert "Should not be included" not in result

    def test_case_insensitive_heading(self) -> None:
        """### summary (lowercase) should still match."""
        text = "### summary\nLowercase heading content.\n"
        result = self._extract(text)
        assert result is not None
        assert "Lowercase heading content" in result


# ── parse_completion_summary_from_log ────────────────────────────────────


class TestParseCompletionSummaryFromLog:
    """Integration tests for the file-backed parser."""

    def _parse(self, log_path: Path):
        from coord.progress import parse_completion_summary_from_log
        return parse_completion_summary_from_log(log_path)

    def test_extracts_summary_from_plain_log(self, tmp_path: Path) -> None:
        log = tmp_path / "worker.log"
        log.write_text(
            "STATUS: implemented changes → running tests → confidence: high\n"
            "### Summary\n"
            "Added the completion_summary column and capture helper.\n"
            "\n"
            "SMOKE_TESTS:\n"
            "- DB column — coord notify — field populated\n"
            "END_SMOKE_TESTS\n",
            encoding="utf-8",
        )
        result = self._parse(log)
        assert result is not None
        assert "completion_summary column" in result

    def test_absent_summary_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "worker.log"
        log.write_text(
            "STATUS: done → all tests pass → confidence: high\n"
            "SMOKE_TESTS:\n"
            "- item — trigger — signal\n"
            "END_SMOKE_TESTS\n",
            encoding="utf-8",
        )
        result = self._parse(log)
        assert result is None

    def test_nonexistent_log_returns_none(self, tmp_path: Path) -> None:
        result = self._parse(tmp_path / "ghost.log")
        assert result is None

    def test_extracts_from_stream_json_log(self, tmp_path: Path) -> None:
        """Stream-json logs embed assistant text inside JSON events."""
        log = tmp_path / "worker.jsonl"
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "### Summary\nShipped the feature cleanly.\n"}
            ]}},
        ]
        lines = "\n".join(json.dumps(e) for e in events)
        log.write_text(lines + "\n", encoding="utf-8")
        result = self._parse(log)
        assert result is not None
        assert "Shipped the feature cleanly" in result

    def test_absent_summary_in_stream_json_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "worker.jsonl"
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "No summary heading here.\n"}
            ]}},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
        result = self._parse(log)
        assert result is None


# ── _capture_completion_summary (notify helper) ───────────────────────────


class TestCaptureCompletionSummary:
    """Tests for ``coord.notify._capture_completion_summary``.

    This is the acceptance bar for #874 — both the prose-present and
    prose-absent paths must be covered.
    """

    def _make_transition(self, assignment_id: str = "aid-1") -> object:
        from coord.notify import Transition
        return Transition(
            assignment_id=assignment_id,
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            event="completion",
            exit_code=0,
        )

    # ── prose-present path ─────────────────────────────────────────────────

    def test_captures_summary_from_local_log(self, tmp_path: Path) -> None:
        """When the local log exists and has a ### Summary block, it is persisted."""
        log = tmp_path / "worker.log"
        log.write_text(
            "STATUS: done\n"
            "### Summary\n"
            "Worker fixed the bug in the DB migration.\n",
            encoding="utf-8",
        )

        transition = self._make_transition()
        entry = {"log_path": str(log)}

        with patch("coord.state.update_assignment_completion_summary") as mock_update:
            from coord.notify import _capture_completion_summary
            _capture_completion_summary(transition, entry)

        mock_update.assert_called_once_with(
            "aid-1", "Worker fixed the bug in the DB migration."
        )

    def test_falls_back_to_agent_endpoint_when_no_local_log(self) -> None:
        """When log_path is absent, the remote /logs/<id> endpoint is tried."""
        transition = self._make_transition()
        entry = {"log_path": None}

        with (
            patch("coord.notify._agent_host", return_value="laptop.tailnet"),
            patch(
                "coord.progress.parse_completion_summary_from_agent",
                return_value="Remote summary prose.",
            ) as mock_remote,
            patch("coord.state.update_assignment_completion_summary") as mock_update,
        ):
            from coord.notify import _capture_completion_summary
            _capture_completion_summary(transition, entry)

        mock_remote.assert_called_once_with("laptop.tailnet", "aid-1")
        mock_update.assert_called_once_with("aid-1", "Remote summary prose.")

    def test_uses_local_log_when_available_skipping_remote(
        self, tmp_path: Path
    ) -> None:
        """When the local log yields a result, the remote endpoint is NOT called."""
        log = tmp_path / "local.log"
        log.write_text("### Summary\nLocal summary.\n", encoding="utf-8")

        transition = self._make_transition()
        entry = {"log_path": str(log)}

        with (
            patch("coord.notify._agent_host", return_value="laptop.tailnet"),
            patch(
                "coord.progress.parse_completion_summary_from_agent",
            ) as mock_remote,
            patch("coord.state.update_assignment_completion_summary") as mock_update,
        ):
            from coord.notify import _capture_completion_summary
            _capture_completion_summary(transition, entry)

        mock_remote.assert_not_called()
        mock_update.assert_called_once_with("aid-1", "Local summary.")

    # ── prose-absent path ──────────────────────────────────────────────────

    def test_does_not_persist_when_log_has_no_summary(self, tmp_path: Path) -> None:
        """A worker log without a ### Summary block leaves the field NULL (no update)."""
        log = tmp_path / "no-summary.log"
        log.write_text(
            "STATUS: done → all tests pass → confidence: high\n"
            "SMOKE_TESTS: (none — change is internal)\n"
            "END_SMOKE_TESTS\n",
            encoding="utf-8",
        )

        transition = self._make_transition()
        entry = {"log_path": str(log)}

        with patch("coord.state.update_assignment_completion_summary") as mock_update:
            from coord.notify import _capture_completion_summary
            _capture_completion_summary(transition, entry)

        mock_update.assert_not_called()

    def test_does_not_persist_when_no_log_and_no_remote_summary(self) -> None:
        """No log + remote returns None → update is not called (field stays NULL)."""
        transition = self._make_transition()
        entry = {"log_path": None}

        with (
            patch("coord.notify._agent_host", return_value="laptop.tailnet"),
            patch(
                "coord.progress.parse_completion_summary_from_agent",
                return_value=None,
            ),
            patch("coord.state.update_assignment_completion_summary") as mock_update,
        ):
            from coord.notify import _capture_completion_summary
            _capture_completion_summary(transition, entry)

        mock_update.assert_not_called()

    def test_silent_when_no_machine_host_and_no_log(self) -> None:
        """When _agent_host returns None (unknown host) and no log, silently no-ops."""
        transition = self._make_transition()
        entry = {"log_path": None}

        with (
            patch("coord.notify._agent_host", return_value=None),
            patch("coord.state.update_assignment_completion_summary") as mock_update,
        ):
            from coord.notify import _capture_completion_summary
            _capture_completion_summary(transition, entry)  # must not raise

        mock_update.assert_not_called()

    # ── error-isolation (best-effort discipline) ────────────────────────────

    def test_persist_failure_is_silent(self, tmp_path: Path) -> None:
        """An exception from ``update_assignment_completion_summary`` is swallowed."""
        log = tmp_path / "worker.log"
        log.write_text("### Summary\nSomething.\n", encoding="utf-8")

        transition = self._make_transition()
        entry = {"log_path": str(log)}

        with patch(
            "coord.state.update_assignment_completion_summary",
            side_effect=RuntimeError("DB locked"),
        ):
            from coord.notify import _capture_completion_summary
            _capture_completion_summary(transition, entry)  # must not raise

    def test_log_parse_failure_is_silent(self, tmp_path: Path) -> None:
        """An OSError reading the log is swallowed; remote fallback is tried."""
        log = tmp_path / "worker.log"
        log.write_text("### Summary\nSomething.\n", encoding="utf-8")

        transition = self._make_transition()
        entry = {"log_path": str(log)}

        with (
            patch(
                "coord.progress.parse_completion_summary_from_log",
                side_effect=OSError("disk error"),
            ),
            patch("coord.notify._agent_host", return_value=None),
            patch("coord.state.update_assignment_completion_summary") as mock_update,
        ):
            from coord.notify import _capture_completion_summary
            _capture_completion_summary(transition, entry)  # must not raise

        mock_update.assert_not_called()
