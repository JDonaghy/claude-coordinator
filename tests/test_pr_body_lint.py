"""Tests for coord.pr_body_lint — the pure PR-body closing-keyword scan/
rewrite logic behind #1196's hole 2 (GitHub's own `Closes #N` magic bypasses
`github_ops.close_issue`'s open-children guard, since it never calls it)."""

from __future__ import annotations

from coord.pr_body_lint import downgrade_closing_keywords, find_closing_references


class TestFindClosingReferences:
    def test_finds_closes(self) -> None:
        assert find_closing_references("Closes #1196") == [1196]

    def test_finds_all_keyword_variants(self) -> None:
        body = "Fixes #1\nClose #2\nResolved #3\nfix #4\nresolves #5\nclosed #6"
        assert find_closing_references(body) == [1, 2, 3, 4, 5, 6]

    def test_ignores_non_closing_references(self) -> None:
        assert find_closing_references("Refs #1196, see also #1195") == []

    def test_ignores_bare_hash_number(self) -> None:
        assert find_closing_references("See #1196 for context") == []

    def test_dedups_preserving_first_seen_order(self) -> None:
        assert find_closing_references("Closes #5\n\nCloses #5\n\nFixes #9") == [5, 9]

    def test_empty_body(self) -> None:
        assert find_closing_references("") == []
        assert find_closing_references(None) == []  # type: ignore[arg-type]


class TestDowngradeClosingKeywords:
    def test_downgrades_matching_number(self) -> None:
        new_body, downgraded = downgrade_closing_keywords("Closes #1041", {1041})
        assert new_body == "Refs #1041"
        assert downgraded == [1041]

    def test_leaves_non_matching_numbers_untouched(self) -> None:
        body = "Closes #55\n\nCloses #99"
        new_body, downgraded = downgrade_closing_keywords(body, {99})
        assert new_body == "Closes #55\n\nRefs #99"
        assert downgraded == [99]

    def test_no_matching_numbers_is_a_noop(self) -> None:
        body = "Closes #55"
        new_body, downgraded = downgrade_closing_keywords(body, {99})
        assert new_body == body
        assert downgraded == []

    def test_downgrades_every_keyword_variant(self) -> None:
        body = "Fixes #1\nResolved #1"
        new_body, downgraded = downgrade_closing_keywords(body, {1})
        assert new_body == "Refs #1\nRefs #1"
        assert downgraded == [1]

    def test_preserves_surrounding_text(self) -> None:
        body = "Automated PR opened by coordinator.\n\nCloses #1041\n\nSee also #1."
        new_body, downgraded = downgrade_closing_keywords(body, {1041})
        assert "Automated PR opened by coordinator." in new_body
        assert "Refs #1041" in new_body
        assert "See also #1." in new_body
        assert downgraded == [1041]
