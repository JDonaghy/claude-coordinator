"""Tests for `coord audit` — the CLI query surface over the audit trail (#1037).

Black-box shape: seed `audit_log` rows directly via `coord.audit.record_audit`
(the write side, #1036), then drive the CLI with Click's `CliRunner` and
assert on both the human table and `--json` output. The CLI routes through
`coord.state.list_audit_log`, which falls back to the local DB when no
`board_service` is configured (the autouse `coord_db` fixture's in-memory DB,
same thread as CliRunner — no `rw_db` needed here, unlike the TestClient/
serve_app tests).
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from coord.audit import record_audit
from coord.cli import main


def test_audit_json_shape(coord_db) -> None:
    record_audit(tier="business", category="test", event_type="test_passed", actor="user", summary="all good", ts=1000.0)
    record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="shipped it", ts=1001.0)

    result = CliRunner().invoke(main, ["audit", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert set(body.keys()) == {"entries", "next_cursor", "has_more"}
    assert [e["summary"] for e in body["entries"]] == ["shipped it", "all good"]
    assert body["has_more"] is False


def test_audit_filters_plumb_through(coord_db) -> None:
    record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="merge row", repo="api", issue=7, ts=1000.0)
    record_audit(tier="business", category="test", event_type="test_passed", actor="user", summary="test row", repo="web", issue=8, ts=1001.0)

    result = CliRunner().invoke(main, ["audit", "--json", "--category", "merge", "--repo", "api"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert [e["summary"] for e in body["entries"]] == ["merge row"]


def test_audit_since_until_iso8601(coord_db) -> None:
    import time as _time

    record_audit(tier="business", category="test", event_type="x", actor="user", summary="old", ts=_time.time() - 86400 * 30)
    record_audit(tier="business", category="test", event_type="x", actor="user", summary="new", ts=_time.time())

    result = CliRunner().invoke(main, ["audit", "--json", "--since", "2000-01-01"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert {e["summary"] for e in body["entries"]} == {"old", "new"}


def test_audit_bad_timestamp_is_a_clean_cli_error(coord_db) -> None:
    result = CliRunner().invoke(main, ["audit", "--since", "not-a-timestamp"])
    assert result.exit_code != 0
    assert "not-a-timestamp" in result.output


def test_audit_human_table_default_output(coord_db) -> None:
    record_audit(tier="business", category="test", event_type="test_passed", actor="user", summary="all good", ts=1000.0)

    result = CliRunner().invoke(main, ["audit"])
    assert result.exit_code == 0, result.output
    assert "test_passed" in result.output
    assert "all good" in result.output


def test_audit_no_entries_message(coord_db) -> None:
    result = CliRunner().invoke(main, ["audit"])
    assert result.exit_code == 0, result.output
    assert "no audit entries match" in result.output


def test_audit_limit_and_cursor_hint(coord_db) -> None:
    for i in range(3):
        record_audit(tier="business", category="test", event_type="x", actor="user", summary=f"row {i}", ts=1000.0 + i)

    result = CliRunner().invoke(main, ["audit", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert "more rows available" in result.output
    assert "--cursor" in result.output
