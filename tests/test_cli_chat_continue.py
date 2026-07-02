"""Tests for the `coord chat-continue` CLI subcommand (#315)."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Proposal


@pytest.fixture
def simple_config(tmp_path):
    """Write a minimal coordinator.yml and return its path."""
    cfg_path = tmp_path / "coordinator.yml"
    cfg_path.write_text(
        """
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /home/user/src/api
"""
    )
    return cfg_path


def _insert_assignment(
    conn,
    *,
    assignment_id: str,
    machine_name: str = "laptop",
    repo_name: str = "api",
    issue_number: int = 10,
    issue_title: str = "Test issue",
    status: str = "done",
    claude_session_id: str | None = None,
    type: str = "refinement",
) -> None:
    conn.execute(
        """INSERT INTO assignments (
            assignment_id, machine_name, repo_name, issue_number, issue_title,
            status, type, dispatched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            assignment_id,
            machine_name,
            repo_name,
            issue_number,
            issue_title,
            status,
            type,
            time.time(),
        ),
    )
    if claude_session_id is not None:
        conn.execute(
            "UPDATE assignments SET claude_session_id=? WHERE assignment_id=?",
            (claude_session_id, assignment_id),
        )
    conn.commit()


class TestChatContinue:
    """Integration tests for `coord chat-continue`."""

    @patch("coord.dispatch.httpx.post")
    def test_dispatches_with_resume_session_id(
        self, mock_post, coord_db, simple_config
    ) -> None:
        """chat-continue passes resume_session_id to dispatch."""
        from coord.db import get_connection
        conn = get_connection()
        _insert_assignment(
            conn,
            assignment_id="old-aid-1",
            claude_session_id="ses-abc-xyz",
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "new-aid-1"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["chat-continue", "--config", str(simple_config), "old-aid-1", "follow up message"],
        )
        assert result.exit_code == 0, result.output

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["resume_session_id"] == "ses-abc-xyz"
        assert payload["type"] == "refinement"
        assert payload["briefing"] == "follow up message"

    @patch("coord.dispatch.httpx.post")
    def test_prints_new_assignment_id(
        self, mock_post, coord_db, simple_config
    ) -> None:
        """chat-continue prints the new assignment ID on stdout."""
        from coord.db import get_connection
        conn = get_connection()
        _insert_assignment(
            conn,
            assignment_id="old-aid-2",
            claude_session_id="ses-id-2",
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "new-aid-2"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["chat-continue", "--config", str(simple_config), "old-aid-2", "next turn"],
        )
        assert result.exit_code == 0
        assert "new-aid-2" in result.output

    def test_fails_when_assignment_missing(self, coord_db, simple_config) -> None:
        """chat-continue exits 1 when the prior assignment isn't in the DB."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["chat-continue", "--config", str(simple_config), "no-such-aid", "hello"],
        )
        assert result.exit_code == 1

    def test_fails_when_no_session_id(self, coord_db, simple_config) -> None:
        """chat-continue exits 1 when the assignment has no claude_session_id."""
        from coord.db import get_connection
        conn = get_connection()
        _insert_assignment(
            conn,
            assignment_id="old-aid-no-sess",
            claude_session_id=None,
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "chat-continue", "--config", str(simple_config),
                "old-aid-no-sess", "hi",
            ],
        )
        assert result.exit_code == 1
        assert "session" in result.output.lower() or "session" in (result.stderr or "").lower()

    @patch("coord.dispatch.httpx.post")
    def test_records_dispatch_in_db(
        self, mock_post, coord_db, simple_config
    ) -> None:
        """chat-continue records the new assignment in the coordinator DB."""
        from coord.db import get_connection
        conn = get_connection()
        _insert_assignment(
            conn,
            assignment_id="old-aid-3",
            claude_session_id="ses-record",
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "new-aid-3"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["chat-continue", "--config", str(simple_config), "old-aid-3", "continue please"],
        )
        assert result.exit_code == 0

        # Verify the new assignment row was inserted into the DB.
        row = conn.execute(
            "SELECT assignment_id, repo_name FROM assignments WHERE assignment_id=?",
            ("new-aid-3",),
        ).fetchone()
        assert row is not None
        assert row[0] == "new-aid-3"


class TestChatContinueTypePreservation:
    """Type round-trip tests for `coord chat-continue` (#316)."""

    @patch("coord.dispatch.httpx.post")
    def test_new_issue_chat_type_round_trips(
        self, mock_post, coord_db, simple_config
    ) -> None:
        """chat-continue sends type='new-issue-chat' when prior assignment has that type."""
        from coord.db import get_connection

        conn = get_connection()
        _insert_assignment(
            conn,
            assignment_id="nic-old-1",
            claude_session_id="ses-nic-1",
            type="new-issue-chat",
            issue_number=0,
            issue_title="(new issue draft)",
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "nic-new-1"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["chat-continue", "--config", str(simple_config), "nic-old-1", "describe the feature"],
        )
        assert result.exit_code == 0, result.output

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["type"] == "new-issue-chat"
        assert payload["resume_session_id"] == "ses-nic-1"

    @patch("coord.dispatch.httpx.post")
    def test_milestone_chat_type_round_trips(
        self, mock_post, coord_db, simple_config
    ) -> None:
        """chat-continue sends type='milestone-chat' when prior assignment
        has that type (#770) — a wrong fallback to 'refinement' would give
        the continuation turn the wrong system prompt/tool restrictions."""
        from coord.db import get_connection

        conn = get_connection()
        _insert_assignment(
            conn,
            assignment_id="mc-old-1",
            claude_session_id="ses-mc-1",
            type="milestone-chat",
            issue_number=100,
            issue_title="Milestone tracker",
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "mc-new-1"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["chat-continue", "--config", str(simple_config), "mc-old-1", "yes, write it"],
        )
        assert result.exit_code == 0, result.output

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["type"] == "milestone-chat"
        assert payload["resume_session_id"] == "ses-mc-1"

    @patch("coord.dispatch.httpx.post")
    def test_refinement_type_still_round_trips(
        self, mock_post, coord_db, simple_config
    ) -> None:
        """Existing refinement type still round-trips after the type-preservation change."""
        from coord.db import get_connection

        conn = get_connection()
        _insert_assignment(
            conn,
            assignment_id="ref-old-rt",
            claude_session_id="ses-ref-rt",
            type="refinement",
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "ref-new-rt"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["chat-continue", "--config", str(simple_config), "ref-old-rt", "another question"],
        )
        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs["json"]
        assert payload["type"] == "refinement"

    @patch("coord.dispatch.httpx.post")
    def test_unknown_type_falls_back_to_refinement(
        self, mock_post, coord_db, simple_config
    ) -> None:
        """A non-chat type (e.g. 'work') falls back to 'refinement' on continuation."""
        from coord.db import get_connection

        conn = get_connection()
        _insert_assignment(
            conn,
            assignment_id="work-old-fb",
            claude_session_id="ses-work-fb",
            type="work",
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "work-new-fb"}
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["chat-continue", "--config", str(simple_config), "work-old-fb", "follow up"],
        )
        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs["json"]
        assert payload["type"] == "refinement"
