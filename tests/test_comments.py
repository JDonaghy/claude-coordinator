"""Tests for coord.comments — comment formatters and marker parsing."""

from __future__ import annotations

import pytest

from coord.comments import (
    EVENT_BRIEFING,
    EVENT_COMPLETION,
    EVENT_FAILURE,
    format_briefing,
    format_completion,
    format_failure,
    parse_coord_comment_marker,
    parse_marker,
)


class TestBriefing:
    def test_includes_required_fields(self) -> None:
        body = format_briefing(
            assignment_id="abc123",
            machine_name="macbook",
            repo_name="api-gateway",
            issue_number=42,
            briefing="Fix the JWT bug.",
            files_likely=["src/auth.rs", "src/jwt.rs"],
        )
        assert "## Coordinator Assignment" in body
        assert "**Machine:** macbook" in body
        assert "**Repo:** api-gateway" in body
        assert "`src/auth.rs`" in body
        assert "`src/jwt.rs`" in body
        assert "### Briefing" in body
        assert "Fix the JWT bug." in body
        # Marker is present and parseable
        marker = parse_marker(body)
        assert marker is not None
        assert marker.event == EVENT_BRIEFING
        assert marker.fields["assignment"] == "abc123"
        assert marker.fields["machine"] == "macbook"
        assert marker.fields["repo"] == "api-gateway"
        assert marker.fields["issue"] == "42"

    def test_do_not_touch_rendered(self) -> None:
        body = format_briefing(
            assignment_id="x",
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            briefing="b",
            files_likely=["a.py"],
            do_not_touch=[("src/db.py", "server is working there")],
        )
        assert "**Do not touch:** `src/db.py` (server is working there)" in body

    def test_no_other_in_flight_renders_placeholder(self) -> None:
        body = format_briefing(
            assignment_id="x",
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            briefing="b",
            files_likely=["a.py"],
        )
        assert "**Do not touch:** (nothing else in flight)" in body

    def test_empty_briefing_has_placeholder(self) -> None:
        body = format_briefing(
            assignment_id="x",
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            briefing="",
        )
        assert "(no briefing provided)" in body


class TestCompletion:
    def test_includes_required_fields(self) -> None:
        body = format_completion(
            assignment_id="abc",
            machine_name="server",
            repo_name="user-svc",
            issue_number=7,
            exit_code=0,
            duration_seconds=125.4,
            log_path="/home/.coord/logs/abc.log",
            summary="Implemented the migration.",
        )
        assert "## Coordinator: Assignment Complete" in body
        assert "**Status:** done" in body
        assert "**Exit code:** 0" in body
        assert "2m 5s" in body
        assert "/home/.coord/logs/abc.log" in body
        assert "Implemented the migration." in body
        marker = parse_marker(body)
        assert marker is not None
        assert marker.event == EVENT_COMPLETION
        assert marker.fields["exit_code"] == "0"

    def test_duration_omitted_renders_dash(self) -> None:
        body = format_completion(
            assignment_id="a", machine_name="m", repo_name="r",
            issue_number=1, exit_code=0,
        )
        assert "**Duration:** —" in body


class TestFailure:
    def test_includes_error_section(self) -> None:
        body = format_failure(
            assignment_id="z",
            machine_name="laptop",
            repo_name="api",
            issue_number=3,
            exit_code=1,
            duration_seconds=4,
            log_path="/tmp/z.log",
            error="Compilation failed: missing import",
        )
        assert "## Coordinator: Assignment Failed" in body
        assert "**Status:** failed" in body
        assert "**Exit code:** 1" in body
        assert "### Error" in body
        assert "Compilation failed: missing import" in body
        marker = parse_marker(body)
        assert marker is not None
        assert marker.event == EVENT_FAILURE

    def test_no_exit_code_renders_dash(self) -> None:
        body = format_failure(
            assignment_id="z", machine_name="m", repo_name="r",
            issue_number=3, exit_code=None,
        )
        assert "**Exit code:** —" in body
        marker = parse_marker(body)
        assert marker is not None
        # exit_code with empty value is dropped from marker
        assert "exit_code" not in marker.fields


class TestMarkerParsing:
    def test_returns_none_for_no_marker(self) -> None:
        assert parse_marker("hello world") is None

    def test_returns_none_for_marker_without_event(self) -> None:
        assert parse_marker("<!-- coord:nope=1 -->") is None

    def test_parses_first_marker_only(self) -> None:
        body = (
            "<!-- coord:event=briefing assignment=a -->\n"
            "<!-- coord:event=completion assignment=b -->"
        )
        m = parse_marker(body)
        assert m is not None
        assert m.event == "briefing"
        assert m.fields["assignment"] == "a"


class TestParseCoordCommentMarker:
    """#873: the unified marker parse feeding the durable issue_comments
    mirror's coord_event/coord_assignment_id/machine/verdict columns."""

    def test_returns_none_for_no_marker(self) -> None:
        assert parse_coord_comment_marker("just a human comment") is None

    def test_parses_generic_event_marker(self) -> None:
        body = format_completion(
            assignment_id="abc123",
            machine_name="macbook",
            repo_name="api-gateway",
            issue_number=42,
            exit_code=0,
        )
        parsed = parse_coord_comment_marker(body)
        assert parsed == {
            "event": "completion",
            "assignment_id": "abc123",
            "machine": "macbook",
            "verdict": None,
        }

    def test_parses_failure_marker(self) -> None:
        body = format_failure(
            assignment_id="xyz789",
            machine_name="dellserver",
            repo_name="api-gateway",
            issue_number=7,
            exit_code=1,
        )
        parsed = parse_coord_comment_marker(body)
        assert parsed is not None
        assert parsed["event"] == "failure"
        assert parsed["assignment_id"] == "xyz789"
        assert parsed["machine"] == "dellserver"

    def test_falls_back_to_review_header(self) -> None:
        body = (
            "<!-- coord:review verdict=approve blocking=0 nonblocking=1 "
            "reviewer=precision assignment=deadbeef -->\n"
            "### Review\n\nLooks good."
        )
        parsed = parse_coord_comment_marker(body)
        assert parsed == {
            "event": "review",
            "assignment_id": "deadbeef",
            "machine": "precision",
            "verdict": "approve",
        }

    def test_review_header_without_verdict_is_not_a_marker(self) -> None:
        # Malformed/incomplete coord:review header (no verdict token) — the
        # generic marker also fails (no event=), so the whole comment is
        # correctly unrecognised rather than half-parsed.
        assert parse_coord_comment_marker("<!-- coord:review reviewer=x -->") is None
