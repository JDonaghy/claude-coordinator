"""#1351: parse_test_verdict_from_log — the Test-gate's PATH-independent
verdict channel.

Mirrors tests/test_review.py::TestParseReviewFromLog, one case per shape
that the review parser already handles, since parse_test_verdict_from_log is
explicitly a sibling of parse_review_from_log sharing the same tolerant
grammar (marker-tolerant, stream-json aware, last-block-wins).
"""

from __future__ import annotations

import json
from pathlib import Path

from coord.review import TestVerdictFindings, parse_test_verdict_from_log


def _write_plain_log(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _write_stream_json_log(path: Path, assistant_texts: list[str]) -> Path:
    lines = []
    for text in assistant_texts:
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }
        lines.append(json.dumps(event))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestParseTestVerdictFromLog:
    def test_plain_text_passed(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
Walked the operator through the checklist. Everything looked good.

TEST_VERDICT: passed
TEST_REASON:
None — all checks passed.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert isinstance(result, TestVerdictFindings)
        assert result.verdict == "passed"
        assert "all checks passed" in result.reason

    def test_plain_text_failed_with_full_brief(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
TEST_VERDICT: failed
TEST_REASON:
Checked the submenu keyboard nav. Expected arrow-down to move focus;
instead it closed the menu. Repro: open Settings, press Down twice.
Suspected file: tui/src/app/menu.rs.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "failed"
        assert "arrow-down to move focus" in result.reason
        assert "menu.rs" in result.reason

    def test_plain_text_last_block_wins(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
TEST_VERDICT: passed
TEST_REASON:
First pass looked fine.
END_TEST

Actually, running it again turned up a bug.

TEST_VERDICT: failed
TEST_REASON:
Crashes on launch with an empty config.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "failed"
        assert "Crashes on launch" in result.reason

    def test_plain_text_no_reason_marker(self, tmp_path: Path) -> None:
        """Mirrors #608 for reviews: the TEST_REASON: header is optional —
        everything between the verdict line and END_TEST is the reason."""
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
TEST_VERDICT: failed

The build fails to start; stack trace points at config parsing.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "failed"
        assert "stack trace" in result.reason
        assert "TEST_REASON:" not in result.reason

    def test_markdown_decoration_tolerated(self, tmp_path: Path) -> None:
        """Mirrors #1346 for reviews: a bolded marker must still parse."""
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
**TEST_VERDICT: failed**
**TEST_REASON:**
Login button is unresponsive after the redesign.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "failed"
        assert "Login button" in result.reason

    def test_pass_fail_aliases(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
TEST_VERDICT: pass
TEST_REASON:
Looks good.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "passed"

    def test_stream_json_no_reason_marker(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_stream_json_log(log, [
            "Running through the checklist...",
            "TEST_VERDICT: failed\n\nThe artifact never launches.\nEND_TEST",
        ])
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "failed"
        assert "never launches" in result.reason

    def test_no_block_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, "Just chatting with the operator, no verdict yet.")
        assert parse_test_verdict_from_log(log) is None

    def test_missing_end_test_returns_none(self, tmp_path: Path) -> None:
        """END_TEST is a hard requirement — an unterminated block doesn't parse."""
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
TEST_VERDICT: passed
TEST_REASON:
Looks fine.
""")
        assert parse_test_verdict_from_log(log) is None

    def test_nonexistent_file_returns_none(self, tmp_path: Path) -> None:
        assert parse_test_verdict_from_log(tmp_path / "nonexistent.log") is None
