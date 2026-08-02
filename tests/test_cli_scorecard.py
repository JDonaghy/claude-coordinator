"""Tests for `coord scorecard` — the CLI wiring for the dogfood scorecard
(#1559): composes coord.github_ops (milestone issues + labels),
coord.usage.fetch_usage_rows (board rows), and coord.state.list_audit_log
(merge durability cross-check) and hands them to the pure
coord.scorecard aggregator.

GitHub reads are mocked at the coord.github_ops function level (never a real
`gh` subprocess — the autouse `_gh_guard` fixture in conftest.py would raise
if one snuck through). Board rows are mocked at ``coord.usage.
fetch_usage_rows`` directly — the same convention ``tests/test_usage.py``
uses for its CLI-layer tests (``SqliteStore.list_assignments()`` opens its
own read-only connection straight to the on-disk DB path, bypassing the
in-memory ``coord_db`` fixture, so seeding via ``record_dispatched_assignment``
and reading it back through ``fetch_usage_rows()`` doesn't round-trip in a
test process).
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from coord import github_ops
from coord.cli import main

from .conftest import VALID_CONFIG, output_and_stderr


def _write_config(tmp_path):
    p = tmp_path / "coordinator.yml"
    p.write_text(VALID_CONFIG)
    return p


def _row(*, assignment_id, issue_number, repo="api", status="merged", **overrides):
    row = {
        "assignment_id": assignment_id,
        "repo_name": repo,
        "issue_number": issue_number,
        "issue_title": f"issue {issue_number}",
        "type": "work",
        "status": status,
        "review_of_assignment_id": None,
        "review_iteration": 0,
        "provider_name": None,
        "dispatched_at": 1000.0,
        "finished_at": 1100.0,
        "cost_usd": 1.0,
        "model": "sonnet",
        "input_tokens": 100,
        "output_tokens": 50,
    }
    row.update(overrides)
    return row


def test_scorecard_json_end_to_end(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)

    monkeypatch.setattr(
        github_ops, "get_milestone", lambda repo, number: {"number": number, "title": "M49"}
    )
    monkeypatch.setattr(
        github_ops,
        "get_milestone_issues",
        lambda repo, title, state="all": [
            {"number": 1, "title": "clean issue", "state": "CLOSED", "labels": []},
            {
                "number": 2,
                "title": "escaped one",
                "state": "CLOSED",
                "labels": [{"name": "escaped:post-merge"}],
            },
        ],
    )

    import coord.usage as usage_mod

    rows = [
        _row(assignment_id="r1", issue_number=1, status="merged"),
        _row(assignment_id="r2", issue_number=2, status="merged"),
    ]
    monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: rows)

    result = CliRunner().invoke(
        main, ["scorecard", "api", "49", "--json", "--config", str(config_path)]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["milestone"] == 49
    assert body["milestone_title"] == "M49"
    assert body["repo_name"] == "api"
    numbers = {row["number"] for row in body["issues"]}
    assert numbers == {1, 2}
    by_number = {row["number"]: row for row in body["issues"]}
    assert by_number[1]["first_pass"] == "yes"
    assert by_number[1]["has_cost_data"] is True
    assert by_number[2]["escaped_defect_stage"] == "post-merge"
    assert body["totals"]["issue_count"] == 2
    assert body["totals"]["first_pass"]["yes"] == 2


def test_scorecard_human_table_has_the_five_metric_lines(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(
        github_ops, "get_milestone", lambda repo, number: {"number": number, "title": "M50"}
    )
    monkeypatch.setattr(
        github_ops,
        "get_milestone_issues",
        lambda repo, title, state="all": [
            {"number": 1, "title": "clean issue", "state": "CLOSED", "labels": []},
        ],
    )
    import coord.usage as usage_mod

    monkeypatch.setattr(
        usage_mod, "fetch_usage_rows",
        lambda *a, **k: [_row(assignment_id="r1", issue_number=1, status="merged")],
    )

    result = CliRunner().invoke(main, ["scorecard", "api", "50", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "First-pass acceptance:" in result.output
    assert "Human interventions:" in result.output
    assert "Cost + wall-clock:" in result.output
    assert "Escaped defects:" in result.output
    assert "Process bugs surfaced:" in result.output


def test_scorecard_verbose_prints_per_issue_table(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(
        github_ops, "get_milestone", lambda repo, number: {"number": number, "title": "M50"}
    )
    monkeypatch.setattr(
        github_ops,
        "get_milestone_issues",
        lambda repo, title, state="all": [
            {"number": 1, "title": "clean issue", "state": "CLOSED", "labels": []},
        ],
    )
    import coord.usage as usage_mod

    monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [])

    result = CliRunner().invoke(
        main, ["scorecard", "api", "50", "--verbose", "--config", str(config_path)]
    )
    assert result.exit_code == 0, result.output
    assert "#1" in result.output
    assert "clean issue" in result.output


def test_scorecard_unknown_repo_is_a_clean_error(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(main, ["scorecard", "nope", "49", "--config", str(config_path)])
    assert result.exit_code != 0
    assert "unknown repo" in result.output


def test_scorecard_milestone_lookup_failure_is_a_clean_error(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)

    def _boom(repo, number):
        raise RuntimeError("gh: milestone not found")

    monkeypatch.setattr(github_ops, "get_milestone", _boom)
    result = CliRunner().invoke(main, ["scorecard", "api", "999", "--config", str(config_path)])
    assert result.exit_code != 0
    assert "could not resolve milestone" in result.output


def test_scorecard_board_fetch_failure_degrades_to_unknown_not_fatal(tmp_path, monkeypatch) -> None:
    """A daemon/board hiccup shouldn't block the label-only metrics — cost
    and first-pass just report unknown for every issue instead of the whole
    command erroring out."""
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(
        github_ops, "get_milestone", lambda repo, number: {"number": number, "title": "M49"}
    )
    monkeypatch.setattr(
        github_ops,
        "get_milestone_issues",
        lambda repo, title, state="all": [
            {"number": 1, "title": "clean issue", "state": "CLOSED", "labels": ["process-bug"]},
        ],
    )
    import coord.usage as usage_mod

    def _boom(*a, **k):
        raise RuntimeError("daemon unreachable")

    monkeypatch.setattr(usage_mod, "fetch_usage_rows", _boom)

    result = CliRunner().invoke(
        main, ["scorecard", "api", "49", "--json", "--config", str(config_path)]
    )
    assert result.exit_code == 0, result.output
    assert "warning: could not fetch board assignment rows" in output_and_stderr(result)
    # The warning (stderr) prints before the JSON payload (stdout) and
    # CliRunner interleaves both into `.output` — skip to the opening brace
    # rather than assume line boundaries, since the JSON itself spans lines.
    body = json.loads(result.output[result.output.index("{"):])
    (issue,) = body["issues"]
    assert issue["first_pass"] == "unknown"
    assert issue["has_cost_data"] is False
    assert issue["process_bug"] is True
    assert issue["regression_test"] == "unknown"


def test_scorecard_audit_log_paginates_past_the_500_row_page_size(tmp_path, monkeypatch) -> None:
    """A repo with a long merge history shouldn't silently truncate the
    durability cross-check to the 500 newest "merged" events — the CLI must
    follow next_cursor/has_more back to the milestone's own creation date."""
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(
        github_ops,
        "get_milestone",
        lambda repo, number: {
            "number": number, "title": "M49", "created_at": "2024-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        github_ops,
        "get_milestone_issues",
        lambda repo, title, state="all": [
            {"number": 1, "title": "clean issue", "state": "CLOSED", "labels": []},
        ],
    )
    import coord.usage as usage_mod

    monkeypatch.setattr(
        usage_mod, "fetch_usage_rows",
        lambda *a, **k: [_row(assignment_id="r1", issue_number=1, status="done")],
    )

    import coord.state as state_mod

    calls = []

    def _fake_list_audit_log(*, repo, category, event_type, since, limit, cursor):
        calls.append({"since": since, "limit": limit, "cursor": cursor})
        if cursor is None:
            return {
                "entries": [{"event_type": "merged", "repo": "api", "issue": 1, "ts": 999.0}],
                "next_cursor": "page2",
                "has_more": True,
            }
        return {
            "entries": [{"event_type": "merged", "repo": "api", "issue": 1, "ts": 998.0}],
            "next_cursor": None,
            "has_more": False,
        }

    monkeypatch.setattr(state_mod, "list_audit_log", _fake_list_audit_log)

    result = CliRunner().invoke(
        main, ["scorecard", "api", "49", "--json", "--config", str(config_path)]
    )
    assert result.exit_code == 0, result.output
    # Two pages fetched — pagination followed next_cursor/has_more rather
    # than stopping at the first page.
    assert len(calls) == 2
    assert calls[0]["cursor"] is None
    assert calls[1]["cursor"] == "page2"
    # Bounded by the milestone's own creation date, not unbounded.
    assert calls[0]["since"] is not None
    body = json.loads(result.output[result.output.index("{"):])
    (issue,) = body["issues"]
    # The board row never flipped to "merged", but the paginated audit
    # cross-check found a "merged" event for issue #1 — durability holds.
    assert issue["first_pass"] == "yes"


def test_scorecard_audit_log_page_cap_warns_instead_of_hanging(tmp_path, monkeypatch) -> None:
    """If a repo's merge history is so long the cross-check can't reach the
    milestone's creation date within the page cap, warn rather than loop
    forever or silently under-report."""
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(
        github_ops, "get_milestone", lambda repo, number: {"number": number, "title": "M49"}
    )
    monkeypatch.setattr(
        github_ops,
        "get_milestone_issues",
        lambda repo, title, state="all": [
            {"number": 1, "title": "clean issue", "state": "CLOSED", "labels": []},
        ],
    )
    import coord.usage as usage_mod

    monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [])

    import coord.state as state_mod

    def _always_more(*, repo, category, event_type, since, limit, cursor):
        return {"entries": [], "next_cursor": "next", "has_more": True}

    monkeypatch.setattr(state_mod, "list_audit_log", _always_more)

    result = CliRunner().invoke(
        main, ["scorecard", "api", "49", "--json", "--config", str(config_path)]
    )
    assert result.exit_code == 0, result.output
    assert "audit log merge cross-check stopped after" in output_and_stderr(result)
