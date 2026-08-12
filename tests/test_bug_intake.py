"""Tests for coord/bug_intake.py — the bug-lane intake contract's four-field
format/parse round-trip (#1964, docs/TEST_FIRST_BUG_LANE.md)."""

from __future__ import annotations

from pathlib import Path

from coord.bug_intake import BugReport, FIELDS, format_bug_report, parse_bug_report

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestFormatBugReport:
    def test_renders_all_four_sections_in_order(self) -> None:
        body = format_bug_report(
            expected="full box border", actual="side-bars only",
            repro="open the extensions panel help popup",
            evidence="screenshots of both, plus develop as a reference",
        )
        assert body.index("## Expected behaviour") < body.index("## Actual behaviour")
        assert body.index("## Actual behaviour") < body.index("## Reproduction")
        assert body.index("## Reproduction") < body.index("## Evidence")
        assert "full box border" in body
        assert "side-bars only" in body
        assert "open the extensions panel help popup" in body
        assert "develop as a reference" in body

    def test_strips_surrounding_whitespace_per_field(self) -> None:
        body = format_bug_report(
            expected="  a  \n", actual="b", repro="c", evidence="d",
        )
        assert "## Expected behaviour\n\na" in body


class TestParseBugReport:
    def test_round_trips_through_format(self) -> None:
        body = format_bug_report(
            expected="a", actual="b", repro="c", evidence="d",
        )
        report = parse_bug_report(body)
        assert report == BugReport(expected="a", actual="b", repro="c", evidence="d")

    def test_none_when_no_headings_present(self) -> None:
        assert parse_bug_report("just some freeform text, no structure") is None

    def test_none_when_a_section_is_missing(self) -> None:
        body = "## Expected behaviour\n\na\n\n## Actual behaviour\n\nb\n"
        assert parse_bug_report(body) is None

    def test_none_when_a_section_is_empty(self) -> None:
        body = (
            "## Expected behaviour\n\na\n\n"
            "## Actual behaviour\n\nb\n\n"
            "## Reproduction\n\n\n\n"
            "## Evidence\n\nd\n"
        )
        assert parse_bug_report(body) is None

    def test_tolerant_of_heading_level_and_case(self) -> None:
        body = (
            "# expected BEHAVIOUR\na\n"
            "### actual behaviour\nb\n"
            "## reproduction\nc\n"
            "###### evidence\nd\n"
        )
        assert parse_bug_report(body) == BugReport(
            expected="a", actual="b", repro="c", evidence="d",
        )

    def test_tolerant_of_prose_before_and_after(self) -> None:
        body = (
            "Some intro context an operator added.\n\n"
            "## Expected behaviour\n\na\n\n"
            "## Actual behaviour\n\nb\n\n"
            "## Reproduction\n\nc\n\n"
            "## Evidence\n\nd\n\n"
            "Some trailing note.\n"
        )
        report = parse_bug_report(body)
        assert report.expected == "a"
        assert report.evidence == "d\n\nSome trailing note."


class TestIssueTemplateHeadingsMatchModule:
    """The GitHub-native template (.github/ISSUE_TEMPLATE/bug_report.md)
    must render exactly the same four headings as this module, or a
    hand-filed issue silently fails to `parse_bug_report` even though it
    looks identical to a `coord issue create --expected ...`-authored one."""

    def test_template_headings_match_and_parse(self) -> None:
        template = (REPO_ROOT / ".github/ISSUE_TEMPLATE/bug_report.md").read_text()
        for _, heading in FIELDS:
            assert f"## {heading}" in template, f"missing heading: {heading!r}"
