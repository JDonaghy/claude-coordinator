"""Tests for coord.goal (#978): GOAL.md -> Plans-panel north-star header."""

from __future__ import annotations

from datetime import date

from coord.goal import parse_goal_header, read_goal_header


SAMPLE_GOAL_MD = """\
# Current Goal — North Star

> Some framing blockquote.
>
> _Last updated: 2026-07-04_ — near-term direction is the two-tier thing.

## 🎯 North star

**Make human-attended interactive `claude` sessions drivable end-to-end from the coord-tui board** —
run the full lifecycle Work -> Test -> Review -> Merge through interactive sessions.

## Why this matters

Some other section that should not leak into the headline.
"""


class TestParseGoalHeader:
    def test_happy_path_extracts_headline_and_date(self):
        result = parse_goal_header(SAMPLE_GOAL_MD)
        assert result["available"] is True
        assert result["last_updated"] == "2026-07-04"
        assert result["headline"] == (
            "Make human-attended interactive `claude` sessions drivable "
            "end-to-end from the coord-tui board"
        )
        assert isinstance(result["days_since_update"], int)
        # Sanity: computed from a real calendar diff against 2026-07-04, not hardcoded.
        expected_days = (date.today() - date(2026, 7, 4)).days
        assert result["days_since_update"] == expected_days

    def test_missing_last_updated_line_leaves_date_fields_none(self):
        text = "# Current Goal — North Star\n\n## 🎯 North star\n\n**Ship it.**\n"
        result = parse_goal_header(text)
        assert result["available"] is True
        assert result["last_updated"] is None
        assert result["days_since_update"] is None
        assert result["headline"] == "Ship it."

    def test_missing_north_star_section_falls_back_to_h1_title(self):
        text = "# Some Other Title\n\n_Last updated: 2020-01-01_\n\nNo north star heading here.\n"
        result = parse_goal_header(text)
        assert result["headline"] == "Some Other Title"
        assert result["last_updated"] == "2020-01-01"
        assert result["days_since_update"] == (date.today() - date(2020, 1, 1)).days

    def test_malformed_date_is_dropped_but_headline_still_resolves(self):
        text = "# Title\n\n_Last updated: not-a-date_\n\n## North star\n\n**Do the thing.**\n"
        result = parse_goal_header(text)
        assert result["last_updated"] is None
        assert result["days_since_update"] is None
        assert result["headline"] == "Do the thing."

    def test_no_content_at_all_still_returns_available_true_with_empty_headline(self):
        result = parse_goal_header("")
        assert result["available"] is True
        assert result["headline"] == ""
        assert result["last_updated"] is None

    def test_overlong_headline_is_truncated(self):
        long_sentence = "x" * 300
        text = f"## North star\n\n**{long_sentence}**\n"
        result = parse_goal_header(text)
        assert len(result["headline"]) == 220
        assert result["headline"].endswith("…")


class TestReadGoalHeader:
    def test_reads_this_repos_actual_goal_md(self):
        """Sanity/integration check: running from this editable checkout,
        GOAL.md is discoverable and parses to a populated header. Asserts on
        shape/types only (not exact text), since GOAL.md's content churns.
        """
        result = read_goal_header()
        assert result["available"] is True
        assert isinstance(result["headline"], str)
        assert result["headline"] != ""
        # This repo's GOAL.md always carries a `_Last updated:_` line.
        assert result["last_updated"] is not None
        assert isinstance(result["days_since_update"], int)
        assert result["days_since_update"] >= -1  # tolerate a same-day clock skew

    def test_fails_open_when_repo_root_resolution_raises(self, monkeypatch):
        import coord.goal as goal_mod

        def _boom():
            raise RuntimeError("no repo root")

        monkeypatch.setattr(goal_mod, "_resolve_goal_md_path", _boom)
        assert read_goal_header() == {"available": False}

    def test_fails_open_when_path_resolution_returns_none(self, monkeypatch):
        import coord.goal as goal_mod

        monkeypatch.setattr(goal_mod, "_resolve_goal_md_path", lambda: None)
        assert read_goal_header() == {"available": False}

    def test_fails_open_when_file_read_raises(self, monkeypatch, tmp_path):
        import coord.goal as goal_mod

        missing = tmp_path / "does-not-exist" / "GOAL.md"
        monkeypatch.setattr(goal_mod, "_resolve_goal_md_path", lambda: missing)
        assert read_goal_header() == {"available": False}


class TestResolveGoalMdPath:
    def test_site_packages_install_returns_none(self, monkeypatch):
        import coord.goal as goal_mod
        import coord as coord_mod

        monkeypatch.setattr(
            coord_mod, "__file__", "/opt/venv/lib/python3.12/site-packages/coord/__init__.py"
        )
        assert goal_mod._resolve_goal_md_path() is None
