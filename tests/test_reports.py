"""Tests for the report engine (#1742) — `coord/reports.py`, `coord report`,
and the daemon's `GET /report` + `GET /report/{report_id}`.

Three layers, tested at the seam each one actually owns:

* ``fold_issue_activity`` is **pure** (fixture events, explicit window, no
  clock), so every derivation — started_at / fix_iterations / verdict order /
  outcome / the anomaly notes — is asserted here without a DB or a daemon.
* pagination is asserted against a *fake paged source*, not by inspection:
  the audit read path hard-caps a single call at 500 rows, and a busy 13h
  window exceeds that.
* the CLI and the two endpoints are black-boxed (``CliRunner`` / Starlette
  ``TestClient``) against seeded ``audit_log`` rows, including the "running a
  report does not mutate the board" invariant.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from coord.audit import record_audit
from coord.cli import main
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.reports import (
    REPORTS,
    ReportError,
    UnknownReportError,
    catalogue,
    detect_prior_activity,
    fetch_audit_window,
    fold_issue_activity,
    parse_duration,
    resolve_params,
    run_report,
)
from coord.serve_app import build_app

# A stable window: 2026-08-02 20:16Z → 2026-08-03 09:16Z, the known-good 13h
# window from the issue. Expressed as bare epoch floats so nothing here needs
# a clock.
T0 = 1_785_000_000.0
WINDOW = (T0, T0 + 13 * 3600)


def _ev(
    ts_offset: float,
    category: str,
    event_type: str,
    *,
    repo: str = "api",
    issue: int | None = 1,
    machine: str | None = None,
    details: dict | None = None,
    entry_id: int | None = None,
) -> dict:
    """One audit entry in the shape ``query_audit_log`` returns."""
    return {
        "id": int(ts_offset) if entry_id is None else entry_id,
        "ts": T0 + ts_offset,
        "tier": "business",
        "category": category,
        "event_type": event_type,
        "actor": "drive",
        "repo": repo,
        "issue": issue,
        "assignment_id": None,
        "machine": machine,
        "summary": f"{category}/{event_type}",
        "details": details,
    }


# ── the pure fold ──────────────────────────────────────────────────────────


class TestFoldIssueActivity:
    def test_one_row_per_issue_with_every_derived_field(self) -> None:
        entries = [
            _ev(10, "drive", "drive_started"),
            _ev(20, "dispatch", "dispatched", machine="precision", details={"type": "work"}),
            _ev(30, "test", "test_failed"),
            _ev(40, "dispatch", "dispatched", machine="dellserver", details={"type": "work"}),
            _ev(50, "test", "test_passed"),
            _ev(60, "review", "review_request-changes"),
            _ev(70, "review", "review_approve"),
            _ev(80, "merge", "merged"),
            _ev(90, "drive", "drive_exited", details={"exit_code": 0, "reason": "ok"}),
            # A second issue, so grouping is actually exercised.
            _ev(15, "dispatch", "dispatched", issue=2, details={"type": "work"}),
        ]
        result = fold_issue_activity(entries, WINDOW)

        assert result.report_id == "issue-activity"
        assert result.window == WINDOW
        # Pure: no clock reached; generated_at defaults to the window end.
        assert result.generated_at == WINDOW[1]

        row = next(r for r in result.rows if r["issue"] == 1)
        assert row["repo"] == "api"
        assert row["started_at"] == T0 + 10
        assert row["started_before_window"] is False
        assert row["machines"] == ["precision", "dellserver"]
        assert row["fix_iterations"] == 1  # two work dispatches, one is the first
        assert row["test_verdicts"] == ["failed", "passed"]
        assert row["review_verdicts"] == ["request-changes", "approve"]
        assert row["merged_at"] == T0 + 80
        assert row["drive_exit"] == {"at": T0 + 90, "exit_code": 0, "reason": "ok"}
        assert row["outcome"] == "merged"

        assert {r["issue"] for r in result.rows} == {1, 2}

    def test_columns_are_the_wire_contract(self) -> None:
        result = fold_issue_activity([_ev(10, "merge", "merged")], WINDOW)
        assert result.columns == [
            "repo", "issue", "title", "started_at", "machines",
            "fix_iterations", "test_verdicts", "review_verdicts",
            "merged_at", "drive_exit", "outcome",
        ]
        # Every declared column is present on every row.
        for row in result.rows:
            for column in result.columns:
                assert column in row

    def test_drive_started_wins_over_dispatch_for_started_at(self) -> None:
        entries = [
            _ev(5, "drive", "drive_started"),
            _ev(9, "dispatch", "dispatched", details={"type": "work"}),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["started_at"] == T0 + 5

    def test_dispatch_alone_sets_started_at(self) -> None:
        row = fold_issue_activity(
            [_ev(7, "dispatch", "dispatched", details={"type": "work"})], WINDOW
        ).rows[0]
        assert row["started_at"] == T0 + 7
        assert row["started_before_window"] is False
        assert row["fix_iterations"] == 0

    def test_started_before_window_is_null_start_not_a_bogus_one(self) -> None:
        """An issue with in-window activity whose first dispatch predates the
        window must NOT report the first in-window event as its start."""
        entries = [
            _ev(120, "test", "test_passed"),
            _ev(300, "review", "review_approve"),
            _ev(600, "merge", "merged"),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["started_at"] is None
        assert row["started_before_window"] is True
        # Nothing in-window is the first dispatch, so no fix iterations either.
        assert row["fix_iterations"] == 0

    def test_first_in_window_dispatch_is_the_start_and_the_rest_are_fixes(self) -> None:
        """Documented limitation: the fold sees only in-window events, so the
        first in-window work dispatch IS the start as far as the window is
        concerned — ``started_before_window`` fires only when the window
        contains no start event at all."""
        entries = [
            _ev(100, "dispatch", "dispatched", details={"type": "work"}),
            _ev(200, "dispatch", "dispatched", details={"type": "work"}),
            _ev(300, "dispatch", "dispatched", details={"type": "work"}),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["started_at"] == T0 + 100
        assert row["started_before_window"] is False
        assert row["fix_iterations"] == 2

    def test_review_and_smoke_dispatches_are_not_fix_iterations(self) -> None:
        entries = [
            _ev(10, "dispatch", "dispatched", details={"type": "work"}),
            _ev(20, "dispatch", "dispatched", details={"type": "review"}),
            _ev(30, "dispatch", "dispatched", details={"type": "smoke"}),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["fix_iterations"] == 0

    def test_entries_may_arrive_newest_first(self) -> None:
        """The audit read path is newest-first; ordered lists must still come
        out in chronological order."""
        entries = [
            _ev(70, "review", "review_approve"),
            _ev(60, "review", "review_request-changes"),
            _ev(30, "test", "test_failed"),
            _ev(50, "test", "test_passed"),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["test_verdicts"] == ["failed", "passed"]
        assert row["review_verdicts"] == ["request-changes", "approve"]

    def test_titles_are_injected_not_guessed(self) -> None:
        result = fold_issue_activity(
            [_ev(10, "merge", "merged")], WINDOW, titles={("api", 1): "Fix the thing"}
        )
        assert result.rows[0]["title"] == "Fix the thing"

    def test_missing_title_is_none_not_an_error(self) -> None:
        assert fold_issue_activity([_ev(10, "merge", "merged")], WINDOW).rows[0]["title"] is None

    def test_events_without_repo_or_issue_are_noted_not_silently_dropped(self) -> None:
        entries = [
            _ev(10, "merge", "merged"),
            _ev(20, "housekeeping", "sweep", repo=None, issue=None),
            _ev(21, "housekeeping", "sweep", repo="api", issue=None),
        ]
        result = fold_issue_activity(entries, WINDOW)
        assert len(result.rows) == 1
        assert any("carry no repo/issue" in n for n in result.notes)
        assert any("2 event(s)" in n for n in result.notes)

    def test_counts_partial_defaults_to_false(self) -> None:
        """Regression guard: with the default empty ``prior_activity``, every
        row carries the new ``counts_partial`` key (additive) but it is
        always False — behaviour is unchanged from before #1760."""
        row = fold_issue_activity(
            [_ev(10, "dispatch", "dispatched", details={"type": "work"})], WINDOW
        ).rows[0]
        assert row["counts_partial"] is False


class TestPriorActivity:
    """#1760: the caller-supplied ``prior_activity`` set is the fold's only
    way to learn about events outside its own window.  These mirror the
    issue's own reproduction — claude-coordinator#1629, where the original
    dispatch predates the window but a fix-1 dispatch (and its review cycle)
    falls inside it."""

    def test_prior_activity_issue_reports_no_start_and_partial_counts(self) -> None:
        """Acceptance criterion #1 verbatim: an in-window work dispatch is
        present, but prior_activity says the issue really started earlier —
        the row must not claim that in-window dispatch as the start."""
        entries = [
            _ev(100, "dispatch", "dispatched", repo="claude-coordinator", issue=1629,
                details={"type": "work"}),
            _ev(110, "review", "review_approve", repo="claude-coordinator", issue=1629),
        ]
        result = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("claude-coordinator", 1629)})
        )
        row = result.rows[0]
        assert row["started_at"] is None
        assert row["started_before_window"] is True
        assert row["counts_partial"] is True

    def test_empty_prior_activity_is_a_no_op(self) -> None:
        """Acceptance criterion #2: same entries, empty prior_activity (the
        default) — behaviour is exactly what it was before #1760."""
        entries = [
            _ev(100, "dispatch", "dispatched", repo="claude-coordinator", issue=1629,
                details={"type": "work"}),
            _ev(110, "review", "review_approve", repo="claude-coordinator", issue=1629),
        ]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["started_at"] == T0 + 100
        assert row["started_before_window"] is False
        assert row["counts_partial"] is False

    def test_every_in_window_dispatch_is_a_fix_when_prior_activity_is_known(self) -> None:
        """Acceptance criterion #3: with prior_activity set, ONE in-window
        work dispatch is already a re-dispatch (fix_iterations == 1), not
        the "first dispatch" that the no-prior-activity fold would treat it
        as (which would report fix_iterations == 0)."""
        entries = [
            _ev(100, "dispatch", "dispatched", details={"type": "work"}),
        ]
        row = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("api", 1)})
        ).rows[0]
        assert row["fix_iterations"] == 1

    def test_multiple_in_window_dispatches_all_count_as_fixes(self) -> None:
        entries = [
            _ev(100, "dispatch", "dispatched", details={"type": "work"}),
            _ev(200, "dispatch", "dispatched", details={"type": "work"}),
            _ev(300, "dispatch", "dispatched", details={"type": "work"}),
        ]
        row = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("api", 1)})
        ).rows[0]
        assert row["fix_iterations"] == 3

    def test_prior_activity_overrides_an_in_window_drive_started_too(self) -> None:
        """Prior activity must win even when the window DOES contain a
        drive_started event — the design says "regardless of whether an
        in-window dispatch exists"."""
        entries = [_ev(50, "drive", "drive_started")]
        row = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("api", 1)})
        ).rows[0]
        assert row["started_at"] is None
        assert row["started_before_window"] is True

    def test_prior_activity_only_applies_to_the_matching_issue(self) -> None:
        entries = [
            _ev(100, "dispatch", "dispatched", issue=1, details={"type": "work"}),
            _ev(100, "dispatch", "dispatched", issue=2, details={"type": "work"}),
        ]
        result = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("api", 1)})
        )
        by_issue = {r["issue"]: r for r in result.rows}
        assert by_issue[1]["counts_partial"] is True
        assert by_issue[2]["counts_partial"] is False
        assert by_issue[2]["started_at"] == T0 + 100


class TestOutcome:
    def test_merged(self) -> None:
        row = fold_issue_activity([_ev(10, "merge", "merged")], WINDOW).rows[0]
        assert row["outcome"] == "merged"

    def test_failed_on_nonzero_drive_exit(self) -> None:
        entries = [_ev(10, "drive", "drive_exited", details={"exit_code": 3, "reason": "deadline"})]
        assert fold_issue_activity(entries, WINDOW).rows[0]["outcome"] == "failed"

    def test_failed_on_crash_exit_with_no_code(self) -> None:
        entries = [_ev(10, "drive", "drive_exited", details={"exit_code": None, "error": "boom"})]
        row = fold_issue_activity(entries, WINDOW).rows[0]
        assert row["outcome"] == "failed"
        assert row["drive_exit"]["reason"] == "boom"

    def test_clean_drive_exit_without_merge_is_stalled(self) -> None:
        entries = [_ev(10, "drive", "drive_exited", details={"exit_code": 0, "reason": "ok"})]
        assert fold_issue_activity(entries, WINDOW).rows[0]["outcome"] == "stalled"

    def test_recent_activity_with_no_exit_is_in_flight(self) -> None:
        # Last event 10 minutes before the window end.
        entries = [_ev(13 * 3600 - 600, "test", "test_passed")]
        assert fold_issue_activity(entries, WINDOW).rows[0]["outcome"] == "in-flight"

    def test_quiet_since_the_start_of_a_long_window_is_stalled(self) -> None:
        entries = [_ev(60, "test", "test_passed")]
        assert fold_issue_activity(entries, WINDOW).rows[0]["outcome"] == "stalled"


class TestNotes:
    def test_nonzero_exit_but_merged_produces_a_note_naming_both_timestamps(self) -> None:
        """The #1631 case: the driver exited 1 with "merge attempted 3 times
        without landing", and the merge landed 13 minutes later anyway."""
        exit_ts_offset = 3600.0
        entries = [
            _ev(
                exit_ts_offset, "drive", "drive_exited", issue=1631,
                details={"exit_code": 1, "reason": "merge attempted 3 times without landing"},
            ),
            _ev(exit_ts_offset + 13 * 60, "merge", "merged", issue=1631),
        ]
        result = fold_issue_activity(entries, WINDOW)

        row = result.rows[0]
        assert row["outcome"] == "merged"

        note = next(n for n in result.notes if "1631" in n)
        assert "api#1631" in note
        assert "exit_code=1" in note
        # Both timestamps, spelled out.
        assert "2026-08-02" in note or "Z" in note
        from coord.reports import _iso

        assert _iso(T0 + exit_ts_offset) in note
        assert _iso(T0 + exit_ts_offset + 13 * 60) in note
        assert "merge attempted 3 times without landing" in note

    def test_clean_exit_and_merge_produces_no_anomaly_note(self) -> None:
        entries = [
            _ev(100, "drive", "drive_exited", details={"exit_code": 0, "reason": "ok"}),
            _ev(200, "merge", "merged"),
        ]
        assert fold_issue_activity(entries, WINDOW).notes == []

    def test_merged_with_a_failing_last_test_verdict_is_flagged(self) -> None:
        entries = [
            _ev(100, "test", "test_passed"),
            _ev(200, "test", "test_failed"),
            _ev(300, "merge", "merged"),
        ]
        notes = fold_issue_activity(entries, WINDOW).notes
        assert any("still 'failed'" in n for n in notes)

    def test_three_or_more_fix_iterations_is_flagged(self) -> None:
        entries = [
            _ev(10 * i, "dispatch", "dispatched", details={"type": "work"})
            for i in range(1, 5)
        ]
        notes = fold_issue_activity(entries, WINDOW).notes
        assert any("fix iterations" in n for n in notes)

    def test_truncation_note_is_explicit(self) -> None:
        result = fold_issue_activity([_ev(10, "merge", "merged")], WINDOW, truncated=True)
        assert result.notes
        assert result.notes[0].startswith("TRUNCATED:")

    def test_counts_partial_row_gets_a_lower_bound_note_naming_the_issue(self) -> None:
        """Acceptance criterion: every row with counts_partial produces a
        notes entry naming the issue and stating the counts are lower
        bounds."""
        entries = [
            _ev(100, "dispatch", "dispatched", repo="claude-coordinator", issue=1629,
                details={"type": "work"}),
        ]
        result = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("claude-coordinator", 1629)})
        )
        note = next(n for n in result.notes if "claude-coordinator#1629" in n)
        assert "lower bound" in note.lower()

    def test_request_changes_with_zero_fix_iterations_is_a_contradiction(self) -> None:
        """Acceptance criterion: a request-changes review verdict with
        fix_iterations == 0 and counts_partial False is not reachable in a
        correct fold — flag it rather than print it deadpan."""
        entries = [
            _ev(10, "review", "review_request-changes"),
        ]
        result = fold_issue_activity(entries, WINDOW)
        row = result.rows[0]
        assert row["fix_iterations"] == 0
        assert row["counts_partial"] is False
        note = next(n for n in result.notes if "api#1" in n)
        assert "inconsistent" in note or "should not happen" in note

    def test_request_changes_with_zero_fix_iterations_is_not_contradiction_when_partial(
        self,
    ) -> None:
        """The same shape is expected (not an error) when counts_partial is
        True — a review from before the window's known start doesn't
        contradict a fix count the row already admits is a lower bound."""
        entries = [
            _ev(10, "review", "review_request-changes"),
        ]
        result = fold_issue_activity(
            entries, WINDOW, prior_activity=frozenset({("api", 1)})
        )
        assert not any(
            "inconsistent" in n or "should not happen" in n for n in result.notes
        )

    def test_request_changes_with_a_real_fix_iteration_is_not_flagged(self) -> None:
        entries = [
            _ev(10, "dispatch", "dispatched", details={"type": "work"}),
            _ev(20, "review", "review_request-changes"),
            _ev(30, "dispatch", "dispatched", details={"type": "work"}),
        ]
        result = fold_issue_activity(entries, WINDOW)
        assert not any(
            "inconsistent" in n or "should not happen" in n for n in result.notes
        )


# ── pagination ─────────────────────────────────────────────────────────────


class _FakePagedSource:
    """A fake audit source that hands back fixed-size pages with a keyset
    cursor, exactly like ``query_audit_log``."""

    def __init__(self, entries: list[dict], page_size: int, *, drop_cursor: bool = False):
        self.entries = entries
        self.page_size = page_size
        self.drop_cursor = drop_cursor
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        cursor = kwargs.get("cursor")
        start = int(cursor) if cursor else 0
        page = self.entries[start : start + self.page_size]
        end = start + len(page)
        has_more = end < len(self.entries)
        return {
            "entries": page,
            "has_more": has_more,
            "next_cursor": None if (self.drop_cursor or not has_more) else str(end),
        }


class TestPagination:
    def test_window_spanning_multiple_pages_is_fully_covered(self) -> None:
        entries = [_ev(i, "test", "test_passed", entry_id=i) for i in range(1250)]
        source = _FakePagedSource(entries, page_size=500)

        fetched, truncated = fetch_audit_window(
            since=WINDOW[0], until=WINDOW[1], fetch=source
        )
        assert len(fetched) == 1250
        assert truncated is False
        # 3 pages: 500 + 500 + 250.
        assert len(source.calls) == 3
        assert source.calls[0]["cursor"] is None
        assert source.calls[1]["cursor"] == "500"
        assert source.calls[2]["cursor"] == "1000"

    def test_single_page_window_does_not_paginate(self) -> None:
        source = _FakePagedSource([_ev(1, "merge", "merged")], page_size=500)
        fetched, truncated = fetch_audit_window(since=WINDOW[0], until=WINDOW[1], fetch=source)
        assert len(fetched) == 1
        assert truncated is False
        assert len(source.calls) == 1

    def test_page_cap_sets_truncated(self) -> None:
        entries = [_ev(i, "test", "test_passed", entry_id=i) for i in range(50)]
        source = _FakePagedSource(entries, page_size=10)
        fetched, truncated = fetch_audit_window(
            since=WINDOW[0], until=WINDOW[1], fetch=source, max_pages=2
        )
        assert len(fetched) == 20
        assert truncated is True

    def test_has_more_without_a_cursor_sets_truncated(self) -> None:
        entries = [_ev(i, "test", "test_passed", entry_id=i) for i in range(50)]
        source = _FakePagedSource(entries, page_size=10, drop_cursor=True)
        fetched, truncated = fetch_audit_window(
            since=WINDOW[0], until=WINDOW[1], fetch=source
        )
        assert len(fetched) == 10
        assert truncated is True

    def test_repo_filter_is_pushed_down_to_the_source(self) -> None:
        source = _FakePagedSource([], page_size=500)
        fetch_audit_window(since=WINDOW[0], until=WINDOW[1], repo="api", fetch=source)
        assert source.calls[0]["repo"] == "api"
        assert source.calls[0]["since"] == WINDOW[0]
        assert source.calls[0]["until"] == WINDOW[1]

    def test_truncated_fetch_surfaces_as_a_note_in_the_run(self) -> None:
        from coord.reports import run_issue_activity

        entries = [_ev(i, "merge", "merged", issue=i, entry_id=i) for i in range(30)]
        source = _FakePagedSource(entries, page_size=10, drop_cursor=True)
        result = run_issue_activity(
            since="13h", now=WINDOW[1], fetch=source, title_lookup=lambda keys: {}
        )
        assert any(n.startswith("TRUNCATED:") for n in result.notes)


# ── prior-activity look-back (#1760) ────────────────────────────────────────


class _CountingIssueSource:
    """A fake audit source keyed by ``(repo, issue)`` — records every call so
    a test can assert the look-back issues exactly one query per issue, not
    one per event and not an unbounded scan."""

    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        repo = kwargs.get("repo")
        issue = kwargs.get("issue")
        until = kwargs.get("until")
        limit = kwargs.get("limit") or 500
        matches = [
            e
            for e in self.entries
            if e.get("repo") == repo
            and e.get("issue") == issue
            and (until is None or float(e["ts"]) <= until)
        ]
        matches.sort(key=lambda e: (-float(e["ts"]), -int(e["id"])))
        return {
            "entries": matches[:limit],
            "has_more": len(matches) > limit,
            "next_cursor": None,
        }


class TestDetectPriorActivity:
    def test_one_query_per_issue_not_per_event(self) -> None:
        # Four issues in the result set (the issue's own "4 issues for a 13h
        # window" example) — the look-back must not touch each issue's
        # events one at a time.
        prior_entries = [
            _ev(-100, "dispatch", "dispatched", issue=1, entry_id=901, details={"type": "work"}),
            _ev(-90, "dispatch", "dispatched", issue=1, entry_id=902, details={"type": "work"}),
            _ev(-50, "dispatch", "dispatched", issue=2, entry_id=903, details={"type": "work"}),
        ]
        source = _CountingIssueSource(prior_entries)
        keys = [("api", 1), ("api", 2), ("api", 3), ("api", 4)]

        result = detect_prior_activity(keys, until=T0, fetch=source)

        assert len(source.calls) == 4
        assert result == frozenset({("api", 1), ("api", 2)})

    def test_no_prior_activity_is_empty(self) -> None:
        source = _CountingIssueSource([])
        assert detect_prior_activity([("api", 1)], until=T0, fetch=source) == frozenset()
        assert len(source.calls) == 1

    def test_duplicate_keys_still_issue_one_query_each(self) -> None:
        source = _CountingIssueSource([])
        detect_prior_activity([("api", 1), ("api", 1), ("api", 2)], until=T0, fetch=source)
        assert len(source.calls) == 2

    def test_query_is_bounded_at_the_window_start_with_limit_one(self) -> None:
        source = _CountingIssueSource([])
        detect_prior_activity([("api", 1)], until=T0, fetch=source)
        call = source.calls[0]
        assert call["until"] == T0
        assert call["limit"] == 1
        assert call["repo"] == "api"
        assert call["issue"] == 1

    def test_events_after_the_window_start_do_not_count_as_prior(self) -> None:
        source = _CountingIssueSource(
            [_ev(50, "dispatch", "dispatched", issue=1, details={"type": "work"})]
        )
        # The only event for issue 1 is AFTER `until` (T0), so it must not
        # register as prior activity.
        assert detect_prior_activity([("api", 1)], until=T0, fetch=source) == frozenset()


# ── registry + parameter validation ────────────────────────────────────────


class TestCatalogue:
    def test_exactly_one_report(self) -> None:
        assert list(REPORTS) == ["issue-activity"]

    def test_catalogue_carries_full_param_metadata(self) -> None:
        cat = catalogue()
        assert [r["id"] for r in cat["reports"]] == ["issue-activity"]
        rep = cat["reports"][0]
        assert rep["title"] == "Issue Activity"
        assert rep["description"]
        params = {p["id"]: p for p in rep["params"]}
        assert set(params) == {"since", "until", "repo"}
        assert params["since"]["kind"] == "choice"
        assert params["since"]["choices"] == ["1h", "6h", "24h", "3d", "7d"]
        assert params["since"]["default"] == "24h"
        assert params["since"]["free_form"] is True
        assert params["repo"]["kind"] == "text"

    def test_catalogue_is_json_serialisable(self) -> None:
        json.dumps(catalogue())


class TestParams:
    def test_defaults_fill_in(self) -> None:
        resolved = resolve_params(REPORTS["issue-activity"], {})
        assert resolved == {"since": "24h", "until": "", "repo": ""}

    def test_preset_and_free_form_durations_both_accepted(self) -> None:
        report = REPORTS["issue-activity"]
        assert resolve_params(report, {"since": "24h"})["since"] == "24h"
        assert resolve_params(report, {"since": "13h"})["since"] == "13h"
        assert resolve_params(report, {"since": "90m"})["since"] == "90m"

    def test_bad_since_names_the_allowed_values(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_params(REPORTS["issue-activity"], {"since": "nonsense"})
        message = str(exc.value)
        assert "nonsense" in message
        for preset in ("1h", "6h", "24h", "3d", "7d"):
            assert preset in message

    def test_bad_until_is_a_clean_error(self) -> None:
        with pytest.raises(ReportError):
            resolve_params(REPORTS["issue-activity"], {"until": "not-a-time"})

    def test_unknown_param_names_the_known_ones(self) -> None:
        with pytest.raises(ReportError) as exc:
            resolve_params(REPORTS["issue-activity"], {"nope": "1"})
        assert "nope" in str(exc.value)
        assert "since" in str(exc.value)

    def test_unknown_report_id(self) -> None:
        with pytest.raises(UnknownReportError) as exc:
            run_report("no-such-report", {})
        assert "issue-activity" in str(exc.value)

    def test_parse_duration_units(self) -> None:
        assert parse_duration("30s") == 30
        assert parse_duration("90m") == 5400
        assert parse_duration("13h") == 46800
        assert parse_duration("3d") == 259200
        assert parse_duration("1w") == 604800
        with pytest.raises(ReportError):
            parse_duration("13 fortnights")


# ── CLI ────────────────────────────────────────────────────────────────────


def _seed_known_good_window(coord_db) -> None:
    """The issue's known-good cross-check, shrunk: four issues that all
    merged, one of which (#1631) had its driver exit 1 before the merge."""
    base = T0
    for issue, offset in ((1629, 100), (1729, 200)):
        record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=issue, machine="precision", details={"type": "work"}, ts=base + offset)
        record_audit(tier="business", category="review", event_type="review_request-changes", actor="reviewer", summary="r", repo="api", issue=issue, ts=base + offset + 10)
        record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=issue, machine="precision", details={"type": "work"}, ts=base + offset + 20)
        record_audit(tier="business", category="review", event_type="review_approve", actor="reviewer", summary="r", repo="api", issue=issue, ts=base + offset + 30)
        record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="m", repo="api", issue=issue, ts=base + offset + 40)
    # #1728: uneventful merge.
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1728, machine="dellserver", details={"type": "work"}, ts=base + 300)
    record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="m", repo="api", issue=1728, ts=base + 340)
    # #1631: driver gave up, merge landed anyway.
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1631, machine="dellserver", details={"type": "work"}, ts=base + 400)
    record_audit(tier="business", category="drive", event_type="drive_exited", actor="drive", summary="x", repo="api", issue=1631, details={"exit_code": 1, "reason": "merge attempted 3 times without landing"}, ts=base + 500)
    record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="m", repo="api", issue=1631, ts=base + 500 + 13 * 60)


def _seed_started_before_window_case(coord_db) -> None:
    """#1760's own reproduction, shrunk to its essential shape.

    #1629: the original dispatch lands at ``T0 - 3600`` — outside a 13h
    window (whose start IS ``T0``) but inside a 20h window (whose start is
    ``T0 - 25200``). The request-changes review, the fix-1 dispatch, its
    test and its approve review are all inside BOTH windows — mirroring the
    live case where only the original dispatch predates the window, not the
    whole review cycle around it.

    #1729 is the control: its entire history is inside both windows.

    #1631 is the already-covered "driver exited 1 but merged anyway"
    anomaly — reseeded here (independent DB per test) to assert it keeps
    firing in both windows once #1629's row stops being self-contradictory.
    """
    base = T0
    # #1629 — original dispatch OUTSIDE the 13h window, inside the 20h one.
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1629, machine="precision", details={"type": "work"}, ts=base - 3600)
    record_audit(tier="business", category="review", event_type="review_request-changes", actor="reviewer", summary="r", repo="api", issue=1629, ts=base + 10)
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1629, machine="precision", details={"type": "work"}, ts=base + 100)
    record_audit(tier="business", category="test", event_type="test_passed", actor="drive", summary="t", repo="api", issue=1629, ts=base + 110)
    record_audit(tier="business", category="review", event_type="review_approve", actor="reviewer", summary="r", repo="api", issue=1629, ts=base + 120)
    # #1729 — control: entirely in-window in both cases.
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1729, machine="precision", details={"type": "work"}, ts=base + 200)
    record_audit(tier="business", category="review", event_type="review_request-changes", actor="reviewer", summary="r", repo="api", issue=1729, ts=base + 210)
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1729, machine="precision", details={"type": "work"}, ts=base + 220)
    record_audit(tier="business", category="test", event_type="test_passed", actor="drive", summary="t", repo="api", issue=1729, ts=base + 230)
    record_audit(tier="business", category="review", event_type="review_approve", actor="reviewer", summary="r", repo="api", issue=1729, ts=base + 240)
    # #1631 — driver gave up, merge landed anyway; must fire in both windows.
    record_audit(tier="business", category="dispatch", event_type="dispatched", actor="drive", summary="d", repo="api", issue=1631, machine="dellserver", details={"type": "work"}, ts=base + 400)
    record_audit(tier="business", category="drive", event_type="drive_exited", actor="drive", summary="x", repo="api", issue=1631, details={"exit_code": 1, "reason": "merge attempted 3 times without landing"}, ts=base + 500)
    record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="m", repo="api", issue=1631, ts=base + 500 + 13 * 60)


@pytest.fixture(autouse=True)
def _frozen_now(monkeypatch):
    """Freeze the report engine's clock at the known-good window end so
    ``since=13h`` covers the seeded rows deterministically."""
    monkeypatch.setattr("coord.reports.time.time", lambda: WINDOW[1])


class TestCli:
    def test_report_list_prints_the_one_report_with_params(self, coord_db) -> None:
        result = CliRunner().invoke(main, ["report", "list"])
        assert result.exit_code == 0, result.output
        assert "issue-activity" in result.output
        assert "Issue Activity" in result.output
        assert "since" in result.output
        assert "24h" in result.output  # the default
        for preset in ("1h", "6h", "3d", "7d"):
            assert preset in result.output
        # Exactly one report in the catalogue.
        assert result.output.count("—  ") == 1

    def test_report_list_json(self, coord_db) -> None:
        result = CliRunner().invoke(main, ["report", "list", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert [r["id"] for r in body["reports"]] == ["issue-activity"]

    def test_report_run_json_shape(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=13h", "--json"]
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert set(body) == {
            "report_id", "generated_at", "window", "columns", "column_meta",
            "rows", "notes",
        }
        assert body["report_id"] == "issue-activity"
        assert body["window"] == [WINDOW[1] - 13 * 3600, WINDOW[1]]

        by_issue = {r["issue"]: r for r in body["rows"]}
        assert set(by_issue) == {1629, 1631, 1728, 1729}
        for row in body["rows"]:
            for key in (
                "started_at", "test_verdicts", "review_verdicts", "merged_at",
                "drive_exit", "outcome",
            ):
                assert key in row
            assert row["outcome"] == "merged"

        assert by_issue[1629]["fix_iterations"] == 1
        assert by_issue[1629]["review_verdicts"] == ["request-changes", "approve"]
        assert by_issue[1729]["fix_iterations"] == 1
        assert by_issue[1729]["review_verdicts"] == ["request-changes", "approve"]
        assert by_issue[1728]["fix_iterations"] == 0

        assert any("1631" in n and "exit_code=1" in n for n in body["notes"])

    def test_report_run_human_table(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=13h"]
        )
        assert result.exit_code == 0, result.output
        assert "OUTCOME" in result.output
        assert "1631" in result.output
        assert "merged" in result.output
        assert "notes" in result.output
        assert "request-changes,approve" in result.output

    def test_started_before_window_across_two_windows_agrees_with_itself(
        self, coord_db
    ) -> None:
        """Live acceptance criterion: at since=13h, #1629's row must no
        longer claim a start it can't support — no start time, at least one
        fix iteration, and a lower-bound note naming it.  At since=20h it is
        a complete row with no such note.  #1729 (the control) and #1631
        (the drive-exit-but-merged anomaly) are unaffected in both."""
        _seed_started_before_window_case(coord_db)

        result_13h = json.loads(
            CliRunner()
            .invoke(main, ["report", "run", "issue-activity", "--param", "since=13h", "--json"])
            .output
        )
        result_20h = json.loads(
            CliRunner()
            .invoke(main, ["report", "run", "issue-activity", "--param", "since=20h", "--json"])
            .output
        )

        row_13h = next(r for r in result_13h["rows"] if r["issue"] == 1629)
        assert row_13h["started_at"] is None
        assert row_13h["started_before_window"] is True
        assert row_13h["fix_iterations"] == 1
        assert row_13h["counts_partial"] is True
        note_13h = next(n for n in result_13h["notes"] if "api#1629" in n)
        assert "lower bound" in note_13h.lower()

        row_20h = next(r for r in result_20h["rows"] if r["issue"] == 1629)
        assert row_20h["started_at"] == T0 - 3600
        assert row_20h["started_before_window"] is False
        assert row_20h["fix_iterations"] == 1
        assert row_20h["counts_partial"] is False
        assert not any("api#1629" in n for n in result_20h["notes"])

        # #1729 (control) — unaffected in both windows.
        for result in (result_13h, result_20h):
            row_1729 = next(r for r in result["rows"] if r["issue"] == 1729)
            assert row_1729["started_before_window"] is False
            assert row_1729["fix_iterations"] == 1
            assert row_1729["counts_partial"] is False
            assert not any("api#1729" in n for n in result["notes"])

        # #1631's driver-exit-but-merged anomaly must still fire in both.
        for result in (result_13h, result_20h):
            assert any("1631" in n and "exit_code=1" in n for n in result["notes"])

    def test_column_meta_is_present_and_matches_columns(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=13h", "--json"]
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)

        assert [m["id"] for m in body["column_meta"]] == body["columns"]

        meta_by_id = {m["id"]: m for m in body["column_meta"]}
        assert meta_by_id["started_at"]["kind"] == "timestamp"
        assert meta_by_id["merged_at"]["kind"] == "timestamp"
        assert meta_by_id["machines"]["kind"] == "list"
        assert meta_by_id["test_verdicts"]["kind"] == "list"
        assert meta_by_id["review_verdicts"]["kind"] == "list"
        assert meta_by_id["fix_iterations"]["kind"] == "int"
        assert meta_by_id["fix_iterations"]["align"] == "right"
        assert meta_by_id["title"]["weight"] > meta_by_id["issue"]["weight"]

        # Row values are unchanged: started_at is still an epoch float,
        # machines still a list — presentation moved, data did not.
        row = next(r for r in body["rows"] if r["issue"] == 1629)
        assert isinstance(row["started_at"], float)
        assert isinstance(row["machines"], list)

    def test_report_run_empty_window(self, coord_db) -> None:
        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=1h"]
        )
        assert result.exit_code == 0, result.output
        assert "no activity in this window" in result.output

    def test_report_run_bad_param_value_exits_nonzero_naming_allowed_values(
        self, coord_db
    ) -> None:
        from tests.conftest import output_and_stderr

        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=nonsense"]
        )
        assert result.exit_code != 0
        text = output_and_stderr(result)
        assert "nonsense" in text
        assert "1h" in text and "7d" in text
        assert "Traceback" not in text

    def test_report_run_unknown_report_exits_nonzero(self, coord_db) -> None:
        from tests.conftest import output_and_stderr

        result = CliRunner().invoke(main, ["report", "run", "no-such-report"])
        assert result.exit_code != 0
        text = output_and_stderr(result)
        assert "no-such-report" in text
        assert "issue-activity" in text

    def test_report_run_unknown_param_exits_nonzero(self, coord_db) -> None:
        from tests.conftest import output_and_stderr

        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "bogus=1"]
        )
        assert result.exit_code != 0
        assert "bogus" in output_and_stderr(result)

    def test_report_run_malformed_param_exits_nonzero(self, coord_db) -> None:
        from tests.conftest import output_and_stderr

        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since"]
        )
        assert result.exit_code != 0
        assert "KEY=VALUE" in output_and_stderr(result)

    def test_repo_param_narrows(self, coord_db) -> None:
        _seed_known_good_window(coord_db)
        record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="m", repo="web", issue=9, ts=T0 + 600)

        result = CliRunner().invoke(
            main,
            ["report", "run", "issue-activity", "--param", "since=13h",
             "--param", "repo=web", "--json"],
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)["rows"]
        assert [r["issue"] for r in rows] == [9]

    def test_titles_come_from_the_local_board(self, coord_db) -> None:
        coord_db.execute(
            "INSERT INTO issues (repo_name, number, title, body, state, labels, "
            "synced_at) VALUES (?,?,?,?,?,?,?)",
            ("api", 1631, "Merge queue gives up early", "", "closed", "[]", 0.0),
        )
        coord_db.commit()
        _seed_known_good_window(coord_db)

        result = CliRunner().invoke(
            main, ["report", "run", "issue-activity", "--param", "since=13h", "--json"]
        )
        assert result.exit_code == 0, result.output
        rows = {r["issue"]: r for r in json.loads(result.output)["rows"]}
        assert rows[1631]["title"] == "Merge queue gives up early"
        assert rows[1728]["title"] is None


# ── daemon endpoints ───────────────────────────────────────────────────────


def _make_daemon_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    conn.close()


@pytest.fixture
def daemon_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    _make_daemon_db(p)
    return p


@pytest.fixture
def rw_db(tmp_path: Path):
    """Thread-safe file-backed ``coord.db`` override — the autouse
    ``coord_db`` fixture's thread-bound ``:memory:`` conn is unusable from
    the ASGI worker thread TestClient runs handlers on."""
    from coord import db as db_mod

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db_mod.override_connection(conn)
    yield conn
    db_mod.close()


@pytest.fixture
def report_client(daemon_db: Path, valid_config_path: Path, rw_db) -> TestClient:
    app = build_app(SqliteStore(daemon_db), load_config(valid_config_path))
    with TestClient(app) as cli:
        yield cli


class TestDaemonEndpoints:
    def test_get_report_returns_the_catalogue_with_param_metadata(
        self, report_client: TestClient
    ) -> None:
        resp = report_client.get("/report")
        assert resp.status_code == 200
        body = resp.json()
        assert [r["id"] for r in body["reports"]] == ["issue-activity"]
        params = {p["id"]: p for p in body["reports"][0]["params"]}
        assert params["since"]["choices"] == ["1h", "6h", "24h", "3d", "7d"]
        assert params["since"]["default"] == "24h"
        assert params["since"]["kind"] == "choice"
        assert params["repo"]["kind"] == "text"

    def test_get_report_run_returns_report_result(
        self, report_client: TestClient, rw_db
    ) -> None:
        _seed_known_good_window(rw_db)
        resp = report_client.get("/report/issue-activity", params={"since": "13h"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["report_id"] == "issue-activity"
        assert {r["issue"] for r in body["rows"]} == {1629, 1631, 1728, 1729}
        assert any("1631" in n for n in body["notes"])
        # #1760: additive display metadata, one entry per `columns` entry.
        assert [m["id"] for m in body["column_meta"]] == body["columns"]

    def test_column_meta_is_additive_columns_and_rows_are_unchanged(
        self, report_client: TestClient, rw_db
    ) -> None:
        """#1760 acceptance: a client that ignores `column_meta` entirely
        gets the v0.4.100 `columns`/`rows` shape byte-for-byte — `columns`
        stays a plain `list[str]`, and existing row keys keep their values."""
        _seed_known_good_window(rw_db)
        resp = report_client.get("/report/issue-activity", params={"since": "13h"})
        body = resp.json()

        assert body["columns"] == [
            "repo", "issue", "title", "started_at", "machines",
            "fix_iterations", "test_verdicts", "review_verdicts",
            "merged_at", "drive_exit", "outcome",
        ]
        assert all(isinstance(c, str) for c in body["columns"])

        by_issue = {r["issue"]: r for r in body["rows"]}
        assert by_issue[1629]["started_at"] == WINDOW[0] + 100
        assert by_issue[1629]["fix_iterations"] == 1
        assert by_issue[1629]["review_verdicts"] == ["request-changes", "approve"]
        assert by_issue[1728]["fix_iterations"] == 0

    def test_endpoint_and_cli_agree_byte_for_byte_on_the_same_window(
        self, report_client: TestClient, rw_db
    ) -> None:
        """The daemon's JSON and the CLI's ``--json`` are the same bytes for
        the same window — that is what makes the TUI panel (#1741) and a
        terminal answer the same question the same way."""
        _seed_known_good_window(rw_db)
        until = repr(WINDOW[1])

        endpoint = report_client.get(
            "/report/issue-activity", params={"since": "13h", "until": until}
        ).json()
        cli_result = CliRunner().invoke(
            main,
            ["report", "run", "issue-activity", "--param", "since=13h",
             "--param", f"until={until}", "--json"],
        )
        assert cli_result.exit_code == 0, cli_result.output
        cli_body = json.loads(cli_result.output)

        assert json.dumps(cli_body, sort_keys=True) == json.dumps(
            endpoint, sort_keys=True
        )

    def test_report_endpoints_require_auth_when_token_set(
        self, daemon_db: Path, valid_config_path: Path, rw_db
    ) -> None:
        app = build_app(SqliteStore(daemon_db), load_config(valid_config_path), token="s3cret")
        with TestClient(app) as cli:
            assert cli.get("/report").status_code == 401
            assert cli.get("/report/issue-activity").status_code == 401
            headers = {"Authorization": "Bearer s3cret"}
            assert cli.get("/report", headers=headers).status_code == 200
            assert cli.get(
                "/report/issue-activity", params={"since": "1h"}, headers=headers
            ).status_code == 200

    def test_unknown_report_is_404(self, report_client: TestClient) -> None:
        resp = report_client.get("/report/no-such-report")
        assert resp.status_code == 404
        assert "issue-activity" in resp.json()["error"]

    def test_bad_param_value_is_400_naming_allowed_values(
        self, report_client: TestClient
    ) -> None:
        resp = report_client.get(
            "/report/issue-activity", params={"since": "nonsense"}
        )
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert "nonsense" in error
        assert "1h" in error and "7d" in error

    def test_unknown_param_is_400(self, report_client: TestClient) -> None:
        resp = report_client.get("/report/issue-activity", params={"bogus": "1"})
        assert resp.status_code == 400
        assert "bogus" in resp.json()["error"]

    def test_report_run_makes_no_subprocess_calls(
        self, report_client: TestClient, rw_db, monkeypatch
    ) -> None:
        """Read-only means read-only: no ``gh``, no shell-out."""
        import subprocess

        def _no_subprocess(*args, **kwargs):  # noqa: ANN002, ANN003
            argv = args[0] if args else kwargs.get("args")
            raise AssertionError(f"subprocess spawned on a report run: {argv!r}")

        _seed_known_good_window(rw_db)
        monkeypatch.setattr(subprocess, "run", _no_subprocess)
        monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
        assert report_client.get(
            "/report/issue-activity", params={"since": "13h"}
        ).status_code == 200

    def test_running_a_report_does_not_mutate_the_board(
        self, report_client: TestClient, rw_db
    ) -> None:
        """The acceptance invariant: a report is a read.  Assert the board's
        ``updated`` timestamp and its assignment rows are untouched across a
        run — this repo's recurring failure mode is a read path quietly
        growing a write."""
        _seed_known_good_window(rw_db)
        rw_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "repo_github, issue_number, issue_title, status, type) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("work1", "laptop", "api", "acme/api", 1629, "t", "done", "work"),
        )
        rw_db.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('updated', '123.5')"
        )
        rw_db.commit()

        def _snapshot() -> tuple:
            updated = rw_db.execute(
                "SELECT value FROM board_meta WHERE key = 'updated'"
            ).fetchone()["value"]
            rows = rw_db.execute(
                "SELECT * FROM assignments ORDER BY assignment_id"
            ).fetchall()
            audit_count = rw_db.execute(
                "SELECT COUNT(*) AS c FROM audit_log"
            ).fetchone()["c"]
            return updated, [tuple(r) for r in rows], audit_count

        before = _snapshot()
        assert report_client.get(
            "/report/issue-activity", params={"since": "13h"}
        ).status_code == 200
        assert _snapshot() == before


def test_report_seam_routes_to_the_daemon_when_board_service_is_set(monkeypatch) -> None:
    """A thin client folds nothing locally — both the catalogue and the run
    go over HTTP, because the audit trail lives on the daemon host."""
    import coord.client as cc
    from coord import state

    monkeypatch.setattr(
        cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
    )
    calls: dict = {}
    monkeypatch.setattr(
        cc,
        "fetch_report_catalogue",
        lambda svc, **kw: calls.update(catalogue_url=svc.url) or {"reports": []},
    )
    monkeypatch.setattr(
        cc,
        "fetch_report",
        lambda svc, report_id, params, **kw: calls.update(
            report_id=report_id, params=params
        )
        or {"report_id": report_id, "rows": []},
    )

    assert state.list_reports() == {"reports": []}
    assert state.run_report("issue-activity", {"since": "13h"})["rows"] == []
    assert calls["catalogue_url"] == "http://d:7435"
    assert calls["report_id"] == "issue-activity"
    assert calls["params"] == {"since": "13h"}


# ── CLI human-table rendering from column_meta (#1760) ─────────────────────


class TestFormatCell:
    """``<window`` means "started_before_window" — it belongs to
    ``started_at`` alone.  A regression caught while smoke-testing #1760's
    own fix: generalising the ``<window`` marker to every ``kind:
    "timestamp"`` column made an unmerged, started-before-window issue's
    empty ``merged_at`` also render as ``<window``, which reads as "this
    merge happened before the window" — false."""

    def test_window_marker_is_scoped_to_started_at(self) -> None:
        from coord.commands.report import _format_cell

        row = {
            "started_at": None,
            "started_before_window": True,
            "merged_at": None,
        }
        started_meta = {"id": "started_at", "kind": "timestamp"}
        merged_meta = {"id": "merged_at", "kind": "timestamp"}

        assert _format_cell("started_at", row, started_meta) == "<window"
        assert _format_cell("merged_at", row, merged_meta) == "-"

    def test_render_table_never_prints_window_marker_for_merged_at(self) -> None:
        from coord.commands.report import _render_table
        from coord.reports import ISSUE_ACTIVITY_COLUMN_META, ISSUE_ACTIVITY_COLUMNS

        result = {
            "columns": ISSUE_ACTIVITY_COLUMNS,
            "column_meta": [m.to_dict() for m in ISSUE_ACTIVITY_COLUMN_META],
            "rows": [
                {
                    "repo": "api",
                    "issue": 1629,
                    "title": None,
                    "started_at": None,
                    "started_before_window": True,
                    "machines": ["precision"],
                    "fix_iterations": 1,
                    "counts_partial": True,
                    "test_verdicts": [],
                    "review_verdicts": ["request-changes", "approve"],
                    "merged_at": None,
                    "drive_exit": None,
                    "outcome": "stalled",
                }
            ],
        }
        lines = _render_table(result)
        data_line = lines[1]
        assert data_line.count("<window") == 1
