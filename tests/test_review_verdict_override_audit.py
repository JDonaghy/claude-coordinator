"""#1456: the historical-audit classifier for silently-rewritten verdicts.

`scripts/audit_review_verdict_overrides.py` recovers pre-#1456 overrides from
the one place the reviewer's own verdict survived — the `review_findings` JSON
blob — and cross-checks it against the effective `review_verdict` column.
These tests pin the classification so the audit can't quietly go blind.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit_review_verdict_overrides import (  # noqa: E402
    classify_review_row,
    scan,
)


def _findings(verdict: str | None, body: str) -> str:
    payload: dict[str, str] = {"body": body}
    if verdict is not None:
        payload["verdict"] = verdict
    return json.dumps(payload)


class TestClassifyReviewRow:
    def test_blob_request_changes_with_approve_row_is_an_override(self) -> None:
        """The #1445 shape: reviewer said request-changes, board says approve,
        nothing recorded the rewrite."""
        v = classify_review_row(
            review_verdict="approve",
            review_findings=_findings("request-changes", "The worktree leaks."),
            review_verdict_original=None,
        )
        assert v.level == "override"

    def test_recorded_override_is_clean(self) -> None:
        """A post-#1456 override is auditable by construction — that's the fix,
        not a finding."""
        v = classify_review_row(
            review_verdict="approve",
            review_findings=_findings("request-changes", "Only nits."),
            review_verdict_original="request-changes",
        )
        assert v.level == "clean"
        assert "request-changes" in v.detail

    def test_genuine_approval_is_clean(self) -> None:
        v = classify_review_row(
            review_verdict="approve",
            review_findings=_findings("approve", "Looks good, merging."),
            review_verdict_original=None,
        )
        assert v.level == "clean"

    def test_request_changes_row_is_clean(self) -> None:
        """Rows that still carry the rejection were never overridden."""
        v = classify_review_row(
            review_verdict="request-changes",
            review_findings=_findings("request-changes", "Blocking: broken."),
            review_verdict_original=None,
        )
        assert v.level == "clean"

    def test_rejection_prose_without_blob_verdict_is_suspect(self) -> None:
        v = classify_review_row(
            review_verdict="approve",
            review_findings=_findings(
                None, "Two problems block this; requesting changes."
            ),
            review_verdict_original=None,
        )
        assert v.level == "suspect"

    def test_explicitly_empty_blocking_section_is_clean(self) -> None:
        """The shape #476 is legitimately allowed to advance — mentioning the
        word "blocking" must not by itself raise a finding."""
        v = classify_review_row(
            review_verdict="approve",
            review_findings=_findings(
                None, "## Blocking findings\nNone.\n## Nits\n- typo\n"
            ),
            review_verdict_original=None,
        )
        assert v.level == "clean"

    def test_missing_findings_is_clean(self) -> None:
        v = classify_review_row(
            review_verdict="approve",
            review_findings=None,
            review_verdict_original=None,
        )
        assert v.level == "clean"

    def test_unparseable_findings_blob_does_not_raise(self) -> None:
        v = classify_review_row(
            review_verdict="approve",
            review_findings="not json at all, requesting changes",
            review_verdict_original=None,
        )
        assert v.level in ("suspect", "clean")


class TestScan:
    def test_scan_reports_only_non_clean_rows(self, tmp_path: Path) -> None:
        db = tmp_path / "coord.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE assignments ("
            "assignment_id TEXT, repo_name TEXT, issue_number INTEGER, "
            "machine_name TEXT, review_of_assignment_id TEXT, "
            "review_verdict TEXT, review_findings TEXT, "
            "review_verdict_original TEXT)"
        )
        conn.executemany(
            "INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("r-1", "api", 1445, "dellserver", "w-1", "approve",
                 _findings("request-changes", "The worktree leaks."), None),
                ("r-2", "api", 1400, "dellserver", "w-2", "approve",
                 _findings("approve", "Clean."), None),
                ("r-3", "api", 1401, "dellserver", "w-3", "request-changes",
                 _findings("request-changes", "Broken."), None),
            ],
        )
        conn.commit()
        conn.close()

        found = scan(db)
        assert [row["assignment_id"] for row, _ in found] == ["r-1"]
        assert found[0][1].level == "override"

    def test_scan_tolerates_pre_1456_schema(self, tmp_path: Path) -> None:
        """A DB that predates the audit columns must still be scannable — that
        is exactly the DB the audit is for."""
        db = tmp_path / "old.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE assignments ("
            "assignment_id TEXT, repo_name TEXT, issue_number INTEGER, "
            "machine_name TEXT, review_of_assignment_id TEXT, "
            "review_verdict TEXT, review_findings TEXT)"
        )
        conn.execute(
            "INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("r-1", "api", 1445, "dellserver", "w-1", "approve",
             _findings("request-changes", "The worktree leaks.")),
        )
        conn.commit()
        conn.close()

        found = scan(db)
        assert len(found) == 1
        assert found[0][1].level == "override"
