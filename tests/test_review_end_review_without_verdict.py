"""#1956 ask 3: END_REVIEW present, REVIEW_VERDICT absent entirely.

quadraui#533's live incident: a headless reviewer wrote a complete, well-
reasoned ~4.5KB review ending in `END_REVIEW` — but never emitted the
`REVIEW_VERDICT:` header ANYWHERE. Grepping the raw log found the string
`REVIEW_VERDICT` exactly once, and that occurrence was inside the *briefing
instructions* (the reviewer's own system/user prompt), never in an assistant
message. `_REVIEW_BLOCK_RE` (the strict parser) correctly finds nothing —
there is nothing to parse — but the row silently lands `status="done"` with
`review_verdict IS NULL`, indistinguishable from "review hasn't finished
yet" at every downstream reader.

This is a DIFFERENT failure signature from #1348 (`detect_unparsed_review_
marker`, which fires when a `REVIEW_VERDICT:` marker EXISTS but is
malformed/rejected): here the marker is missing entirely, and
`detect_unparsed_review_marker` can't see it — the two detectors are
mutually exclusive by construction (see the "review_verdict_present" test
below).
"""

from __future__ import annotations

import json

import pytest

from coord.review import (
    _DIAGNOSTIC_EXCERPT_MAX,
    detect_end_review_without_verdict,
    detect_unparsed_review_marker,
)

# The quadraui#533 shape: a full, well-formed review body ending in
# END_REVIEW, with no REVIEW_VERDICT: marker anywhere.
_QUADRAUI_533_TEXT = """\
## Review: PR #536 — fix the widget renderer

I read the whole diff carefully against the CLAUDE.md checklist.

## Blocking findings

None.

## Non-blocking concerns

None.

## Nits

None.

This is a clean, well-tested change. Approving.
END_REVIEW
"""

# A crashed/truncated session: no END_REVIEW at all. NOT the #1956 signature
# — this detector must stay silent so a genuinely dead session isn't
# misreported as "verdict recoverable".
_CRASHED_TEXT = "I started reading the diff and then the session died.\n"

# A marker IS present (even malformed) — #1348's territory, not #1956's.
_MARKER_PRESENT_TEXT = "**REVIEW_VERDICT: approve**\n\nLGTM.\n\n## End Review\n"

# Strict parse succeeds — the guard must return None (no double-report).
_CLEAN_TEXT = "REVIEW_VERDICT: approve\nREVIEW_BODY:\n\nLGTM.\n\nEND_REVIEW\n"


def _stream_json_line(text: str) -> str:
    msg = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    return json.dumps(msg)


class TestDetectEndReviewWithoutVerdict:
    def test_fires_on_complete_body_with_no_verdict_marker(self) -> None:
        result = detect_end_review_without_verdict(_QUADRAUI_533_TEXT)
        assert result is not None
        assert "Approving" in result.excerpt
        assert "Blocking findings" in result.excerpt

    def test_no_end_review_returns_none(self) -> None:
        """A crashed/truncated session (no END_REVIEW at all) is NOT this
        signature — must not be misreported as 'verdict recoverable'."""
        assert detect_end_review_without_verdict(_CRASHED_TEXT) is None

    def test_review_verdict_marker_present_returns_none(self) -> None:
        """A REVIEW_VERDICT: marker existing (even malformed) is #1348's
        territory — mutually exclusive with this detector."""
        assert detect_end_review_without_verdict(_MARKER_PRESENT_TEXT) is None
        # And the inverse holds too: #1348's detector fires on it.
        assert detect_unparsed_review_marker(_MARKER_PRESENT_TEXT) is not None

    def test_strict_parse_succeeds_returns_none(self) -> None:
        """A clean, fully-parseable review must not double-report."""
        assert detect_end_review_without_verdict(_CLEAN_TEXT) is None

    def test_no_marker_and_no_end_review_returns_none(self) -> None:
        assert detect_end_review_without_verdict("nothing review-shaped here") is None

    def test_excerpt_capped(self) -> None:
        long_body = "x" * (_DIAGNOSTIC_EXCERPT_MAX * 3)
        text = f"{long_body}\nEND_REVIEW\n"
        result = detect_end_review_without_verdict(text)
        assert result is not None
        assert len(result.excerpt) <= _DIAGNOSTIC_EXCERPT_MAX

    def test_excerpt_is_the_text_before_end_review(self) -> None:
        """Unlike detect_unparsed_review_marker (which anchors forward from
        REVIEW_VERDICT:), this anchors BACKWARD from END_REVIEW — there is
        no header line to anchor on instead."""
        result = detect_end_review_without_verdict(_QUADRAUI_533_TEXT)
        assert result is not None
        assert "END_REVIEW" not in result.excerpt

    def test_transcript_path_and_host_threaded_through(self) -> None:
        result = detect_end_review_without_verdict(
            _QUADRAUI_533_TEXT, transcript_path="/tmp/fb021a044a0e.log", host="elitebook",
        )
        assert result is not None
        assert result.transcript_path == "/tmp/fb021a044a0e.log"
        assert result.host == "elitebook"

    def test_multiple_end_review_uses_last(self) -> None:
        """A reviewer that second-guesses itself mid-session: use the LAST
        END_REVIEW, mirroring _parse_review_text's own matches[-1] convention."""
        text = (
            "Some early prose that also happens to say END_REVIEW mid-sentence.\n"
            "END_REVIEW\n\n"
            "Actually let me reconsider.\n\n"
            "Final answer: this looks correct.\n"
            "END_REVIEW\n"
        )
        result = detect_end_review_without_verdict(text)
        assert result is not None
        assert "Final answer" in result.excerpt

    def test_stream_json_log_header_template_does_not_defeat_detector(self) -> None:
        """The #1348-round-2 trap, replayed for this detector: the reviewer's
        OWN system-prompt argv header embeds the literal REVIEW_VERDICT:/
        REVIEW_BODY:/END_REVIEW template (see REVIEWER_SYSTEM_PROMPT), and
        that header is on a non-JSON comment line, not inside any
        "assistant"-typed event. Since `_decode_transcript_for_diagnostic`
        only concatenates assistant-typed text, the header's own
        REVIEW_VERDICT: occurrence must NOT suppress a real find in the
        actual (headerless) assistant text below — this is exactly the
        quadraui#533 shape: `REVIEW_VERDICT` grepped exactly once in the raw
        log, and that hit was inside the briefing, never emitted by the
        model.
        """
        from coord.review import REVIEWER_SYSTEM_PROMPT

        header_argv = (
            "claude -p --output-format stream-json --system-prompt "
            + REVIEWER_SYSTEM_PROMPT.replace("\n", "\\n")
            + " --model sonnet"
        )
        header = f"# agent=elitebook repo=quadraui issue=#533 argv={header_argv}\n"
        log_text = header + _stream_json_line(_QUADRAUI_533_TEXT) + "\n"

        result = detect_end_review_without_verdict(log_text)

        assert result is not None
        assert "Approving" in result.excerpt
        assert "your full review text in markdown" not in result.excerpt
