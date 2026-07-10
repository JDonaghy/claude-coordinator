"""Tests for the #1037 read side of the audit trail: `coord.audit.query_audit_log`
— keyset pagination + filters over `audit_log`.

Scope per the issue's acceptance bar:
- filter params (since/until/type/category/repo/issue/assignment/tier) narrow
  correctly;
- keyset pagination on (ts, id) DESC returns stable, non-overlapping pages;
- has_more/next_cursor are correct across a multi-page fixture.
"""

from __future__ import annotations

from coord.audit import query_audit_log, record_audit


def _seed(coord_db, n: int, **overrides) -> None:
    """Insert *n* rows with strictly increasing ts (1000, 1001, ...)."""
    for i in range(n):
        kwargs = dict(
            tier="business",
            category="test",
            event_type="test_passed",
            actor="user",
            summary=f"row {i}",
            ts=1000.0 + i,
        )
        kwargs.update(overrides)
        record_audit(**kwargs)


class TestFilters:
    def test_no_filters_returns_all_newest_first(self, coord_db) -> None:
        _seed(coord_db, 3)
        result = query_audit_log()
        assert [e["summary"] for e in result["entries"]] == ["row 2", "row 1", "row 0"]
        assert result["has_more"] is False
        assert result["next_cursor"] is None

    def test_since_until_narrow(self, coord_db) -> None:
        _seed(coord_db, 5)  # ts 1000..1004
        result = query_audit_log(since=1001.0, until=1003.0)
        assert [e["summary"] for e in result["entries"]] == ["row 3", "row 2", "row 1"]

    def test_event_type_filter(self, coord_db) -> None:
        record_audit(tier="business", category="test", event_type="test_passed", actor="user", summary="a", ts=1000.0)
        record_audit(tier="business", category="test", event_type="test_failed", actor="user", summary="b", ts=1001.0)
        result = query_audit_log(event_type="test_failed")
        assert [e["summary"] for e in result["entries"]] == ["b"]

    def test_category_filter(self, coord_db) -> None:
        record_audit(tier="business", category="merge", event_type="merged", actor="coordinator", summary="a", ts=1000.0)
        record_audit(tier="business", category="test", event_type="test_passed", actor="user", summary="b", ts=1001.0)
        result = query_audit_log(category="merge")
        assert [e["summary"] for e in result["entries"]] == ["a"]

    def test_repo_and_issue_filter(self, coord_db) -> None:
        record_audit(tier="business", category="test", event_type="x", actor="user", summary="a", repo="api", issue=1, ts=1000.0)
        record_audit(tier="business", category="test", event_type="x", actor="user", summary="b", repo="api", issue=2, ts=1001.0)
        record_audit(tier="business", category="test", event_type="x", actor="user", summary="c", repo="web", issue=1, ts=1002.0)
        assert [e["summary"] for e in query_audit_log(repo="api")["entries"]] == ["b", "a"]
        assert [e["summary"] for e in query_audit_log(repo="api", issue=1)["entries"]] == ["a"]

    def test_assignment_and_tier_filter(self, coord_db) -> None:
        record_audit(tier="business", category="test", event_type="x", actor="user", summary="a", assignment_id="aid-1", ts=1000.0)
        record_audit(tier="operational", category="housekeeping", event_type="y", actor="daemon", summary="b", assignment_id="aid-2", ts=1001.0)
        assert [e["summary"] for e in query_audit_log(assignment_id="aid-1")["entries"]] == ["a"]
        assert [e["summary"] for e in query_audit_log(tier="operational")["entries"]] == ["b"]

    def test_details_decoded_into_dict(self, coord_db) -> None:
        record_audit(
            tier="business", category="test", event_type="x", actor="user",
            summary="a", ts=1000.0, details={"k": "v"},
        )
        entry = query_audit_log()["entries"][0]
        assert entry["details"] == {"k": "v"}

    def test_details_none_when_absent(self, coord_db) -> None:
        record_audit(tier="business", category="test", event_type="x", actor="user", summary="a", ts=1000.0)
        entry = query_audit_log()["entries"][0]
        assert entry["details"] is None


class TestPagination:
    def test_limit_caps_page_size_and_sets_has_more(self, coord_db) -> None:
        _seed(coord_db, 5)
        result = query_audit_log(limit=2)
        assert len(result["entries"]) == 2
        assert result["has_more"] is True
        assert result["next_cursor"] is not None

    def test_cursor_continues_without_overlap_or_gap(self, coord_db) -> None:
        _seed(coord_db, 5)  # rows "row 0".."row 4", newest first: 4,3,2,1,0
        page1 = query_audit_log(limit=2)
        assert [e["summary"] for e in page1["entries"]] == ["row 4", "row 3"]
        assert page1["has_more"] is True

        page2 = query_audit_log(limit=2, cursor=page1["next_cursor"])
        assert [e["summary"] for e in page2["entries"]] == ["row 2", "row 1"]
        assert page2["has_more"] is True

        page3 = query_audit_log(limit=2, cursor=page2["next_cursor"])
        assert [e["summary"] for e in page3["entries"]] == ["row 0"]
        assert page3["has_more"] is False
        assert page3["next_cursor"] is None

    def test_pages_are_stable_under_concurrent_insert(self, coord_db) -> None:
        """A new row landing between page fetches (with an OLDER ts than
        the cursor) must not appear in, or shift, the next page — the
        keyset cursor only looks strictly older than the last-seen (ts, id)."""
        _seed(coord_db, 3)  # ts 1000, 1001, 1002
        page1 = query_audit_log(limit=2)
        assert [e["summary"] for e in page1["entries"]] == ["row 2", "row 1"]

        # A late-arriving row with a NEWER ts than anything seen so far —
        # must not retroactively appear on the already-fetched page1, and
        # must not appear on page2 either (it's newer than the cursor).
        record_audit(
            tier="business", category="test", event_type="test_passed",
            actor="user", summary="row new", ts=2000.0,
        )
        page2 = query_audit_log(limit=2, cursor=page1["next_cursor"])
        assert [e["summary"] for e in page2["entries"]] == ["row 0"]
        assert "row new" not in [e["summary"] for e in page2["entries"]]

    def test_limit_hard_capped(self, coord_db) -> None:
        from coord.audit import MAX_LIMIT

        _seed(coord_db, MAX_LIMIT + 10)
        result = query_audit_log(limit=MAX_LIMIT * 10)
        assert len(result["entries"]) == MAX_LIMIT
        assert result["has_more"] is True

    def test_bad_cursor_degrades_to_first_page(self, coord_db) -> None:
        _seed(coord_db, 2)
        result = query_audit_log(cursor="not-a-cursor")
        assert len(result["entries"]) == 2
