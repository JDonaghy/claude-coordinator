"""Tests for coord.forge_availability (#1896 Phase 0: forge/CI availability
measurement).

Scope per the issue's acceptance bar:
- one audit_log row per observation at each of the three seams, with
  timestamp/outcome/duration;
- capture is strictly best-effort — a failure to record never raises;
- the read-out (`availability_report`) computes uptime %, longest
  contiguous unavailable stretch, and refusal counts by reason correctly
  over a seeded set of observations;
- the retention sweep bounds growth without deleting recent data.
"""

from __future__ import annotations

import json
import time

import pytest

from coord.forge_availability import (
    CATEGORY,
    MERGE_GATE_REFUSAL_KINDS,
    RETENTION_DAYS,
    availability_report,
    format_report_lines,
    record_ci_check_fetch,
    record_gh_call,
    record_merge_gate_refusal,
    summary_line,
    _maybe_prune,
)


def _rows(coord_db, *, event_type: str | None = None) -> list:
    if event_type is None:
        return coord_db.execute(
            "SELECT * FROM audit_log WHERE category=? ORDER BY id", (CATEGORY,)
        ).fetchall()
    return coord_db.execute(
        "SELECT * FROM audit_log WHERE category=? AND event_type=? ORDER BY id",
        (CATEGORY, event_type),
    ).fetchall()


class TestRecordGhCall:
    def test_records_one_row_with_outcome_and_duration(self, coord_db) -> None:
        record_gh_call(("pr", "view", "1"), outcome="ok", duration_s=0.42)

        rows = _rows(coord_db, event_type="gh_call")
        assert len(rows) == 1
        assert rows[0]["tier"] == "operational"
        details = json.loads(rows[0]["details_json"])
        assert details["outcome"] == "ok"
        assert details["duration_s"] == 0.42
        assert details["argv0"] == "pr"

    def test_records_unreachable_outcome(self, coord_db) -> None:
        record_gh_call(("pr", "view"), outcome="unreachable", duration_s=30.0,
                        detail="timed out")

        details = json.loads(_rows(coord_db, event_type="gh_call")[0]["details_json"])
        assert details["outcome"] == "unreachable"
        assert details["detail"] == "timed out"

    def test_never_raises_when_the_underlying_store_always_throws(
        self, coord_db, monkeypatch
    ) -> None:
        """Acceptance bar: 'Assert this with a store that always throws.'"""
        def _boom(*a, **k):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr("coord.forge_availability.record_audit", _boom)

        record_gh_call(("pr", "view"), outcome="ok", duration_s=0.1)  # must not raise

        assert _rows(coord_db) == []


class TestRecordCiCheckFetch:
    def test_records_conclusions_distribution(self, coord_db) -> None:
        record_ci_check_fetch(
            "acme/api", 42, outcome="ok", duration_s=0.9,
            conclusions={"success": 2, "failure": 1},
        )

        row = _rows(coord_db, event_type="ci_check_fetch")[0]
        assert row["repo"] == "acme/api"
        assert row["issue"] == 42
        details = json.loads(row["details_json"])
        assert details["outcome"] == "ok"
        assert details["conclusions"] == {"success": 2, "failure": 1}

    def test_never_raises_when_the_underlying_store_always_throws(
        self, coord_db, monkeypatch
    ) -> None:
        def _boom(*a, **k):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr("coord.forge_availability.record_audit", _boom)

        record_ci_check_fetch("acme/api", 1, outcome="unreachable", duration_s=30.0)


class TestRecordMergeGateRefusal:
    def test_records_reason_and_message(self, coord_db) -> None:
        record_merge_gate_refusal(
            repo="api", issue=7, reason="checks_failed", message="build (failure)",
        )

        row = _rows(coord_db, event_type="merge_gate_refusal")[0]
        assert row["repo"] == "api"
        assert row["issue"] == 7
        details = json.loads(row["details_json"])
        assert details == {"reason": "checks_failed", "message": "build (failure)"}

    def test_scope_is_exactly_the_three_named_kinds(self) -> None:
        assert MERGE_GATE_REFUSAL_KINDS == {
            "checks_failed", "checks_pending", "checks_stale",
        }

    def test_never_raises_when_the_underlying_store_always_throws(
        self, coord_db, monkeypatch
    ) -> None:
        def _boom(*a, **k):
            raise RuntimeError("disk I/O error")

        monkeypatch.setattr("coord.forge_availability.record_audit", _boom)

        record_merge_gate_refusal(repo="api", issue=1, reason="checks_failed", message="x")


class TestAvailabilityReport:
    def test_empty_window_reports_no_observations(self, coord_db) -> None:
        report = availability_report(window_days=7.0, now=1_000_000.0)

        assert report.uptime_pct is None
        assert report.total_observations == 0
        assert report.refusals_by_reason == {}

    def test_uptime_pct_over_mixed_outcomes(self, coord_db) -> None:
        now = time.time()
        # 3 available, 1 unavailable => 75% uptime.
        record_gh_call(("a",), outcome="ok", duration_s=0.1)
        record_gh_call(("b",), outcome="app_error", duration_s=0.1)
        record_ci_check_fetch("api", 1, outcome="ok", duration_s=0.1, conclusions={})
        record_gh_call(("c",), outcome="unreachable", duration_s=1.0)

        report = availability_report(window_days=7.0, now=now + 10)

        assert report.gh_calls == 3
        assert report.ci_fetches == 1
        assert report.available == 3
        assert report.unavailable == 1
        assert report.uptime_pct == pytest.approx(75.0)

    def test_excludes_observations_outside_the_window(self, coord_db) -> None:
        now = time.time()
        old_ts = now - 40 * 86400.0  # 40 days ago
        record_gh_call(("old",), outcome="unreachable", duration_s=1.0)
        # Force the row's ts to be outside a 30-day window.
        coord_db.execute("UPDATE audit_log SET ts=? WHERE category=?", (old_ts, CATEGORY))
        coord_db.commit()
        record_gh_call(("new",), outcome="ok", duration_s=0.1)

        report = availability_report(window_days=30.0, now=time.time() + 10)

        assert report.total_observations == 1
        assert report.available == 1

    def test_longest_unavailable_stretch_is_contiguous_run_span(self, coord_db) -> None:
        now = 1_000_000.0
        # Three consecutive unavailable observations, 100s apart, the last
        # one itself taking 5s -- span = (t0 + 200 + 5) - t0 = 205s.
        for i, ts_offset in enumerate((0.0, 100.0, 200.0)):
            record_gh_call((f"x{i}",), outcome="unreachable", duration_s=5.0)
            coord_db.execute(
                "UPDATE audit_log SET ts=? WHERE category=? AND id="
                "(SELECT MAX(id) FROM audit_log WHERE category=?)",
                (now + ts_offset, CATEGORY, CATEGORY),
            )
        coord_db.commit()
        # A later, available observation ends the run.
        record_gh_call(("ok",), outcome="ok", duration_s=0.1)
        coord_db.execute(
            "UPDATE audit_log SET ts=? WHERE category=? AND id="
            "(SELECT MAX(id) FROM audit_log WHERE category=?)",
            (now + 500.0, CATEGORY, CATEGORY),
        )
        coord_db.commit()

        report = availability_report(window_days=30.0, now=now + 1000.0)

        assert report.longest_unavailable_stretch_s == pytest.approx(205.0)

    def test_refusals_by_reason_counts(self, coord_db) -> None:
        record_merge_gate_refusal(repo="api", issue=1, reason="checks_failed", message="x")
        record_merge_gate_refusal(repo="api", issue=2, reason="checks_failed", message="y")
        record_merge_gate_refusal(repo="api", issue=3, reason="checks_pending", message="z")

        report = availability_report(window_days=7.0, now=time.time() + 10)

        assert report.refusals_by_reason == {"checks_failed": 2, "checks_pending": 1}

    def test_format_report_lines_and_summary_line_render(self, coord_db) -> None:
        record_gh_call(("a",), outcome="ok", duration_s=0.1)
        record_merge_gate_refusal(repo="api", issue=1, reason="checks_stale", message="x")

        report = availability_report(window_days=7.0, now=time.time() + 10)
        lines = format_report_lines(report)
        line = summary_line(report)

        assert any("uptime" in l for l in lines)
        assert any("checks_stale: 1" in l for l in lines)
        assert line.startswith("FORGE_AVAILABILITY: ")
        assert "uptime_pct=100.00" in line
        assert "refusals_total=1" in line


class TestRetentionSweep:
    def test_prune_deletes_rows_older_than_retention_but_keeps_recent(
        self, coord_db
    ) -> None:
        now = time.time()
        record_gh_call(("old",), outcome="ok", duration_s=0.1)
        old_ts = now - (RETENTION_DAYS + 1) * 86400.0
        coord_db.execute("UPDATE audit_log SET ts=? WHERE category=?", (old_ts, CATEGORY))
        coord_db.commit()
        record_gh_call(("new",), outcome="ok", duration_s=0.1)

        _maybe_prune(force=True)

        rows = _rows(coord_db)
        assert len(rows) == 1
        assert json.loads(rows[0]["details_json"])["argv0"] == "new"

    def test_prune_never_touches_other_categories(self, coord_db) -> None:
        from coord.audit import record_audit

        old_ts = time.time() - (RETENTION_DAYS + 1) * 86400.0
        record_audit(
            tier="business", category="merge", event_type="merged",
            actor="system", summary="unrelated", ts=old_ts,
        )

        _maybe_prune(force=True)

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE category='merge'"
        ).fetchall()
        assert len(rows) == 1

    def test_prune_sweep_failure_never_raises(self, coord_db, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.db.get_connection",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        _maybe_prune(force=True)  # must not raise
