"""#1348: strict-parse failure on a transcript that clearly contains a review
must be loud, not silent.

After the #1346 incident — a 6.2 KB request-changes verdict was silently
dropped because `**REVIEW_VERDICT: request-changes**` (bolded) did not match
`_REVIEW_BLOCK_RE` — a strict-parse failure on a transcript that passes the
attribution gates must:

(#1346 has since landed on `main` and made `_REVIEW_BLOCK_RE` itself tolerate
Markdown decoration directly around the `REVIEW_VERDICT:` / `REVIEW_BODY:`
markers, so bolding *only those two* no longer breaks the strict parser. The
fixtures below still use bolded markers — for realism, and because the
strict/diagnostic split must still cooperate correctly when decoration IS
tolerated elsewhere in the block — but pair them with a terminator shape
`_REVIEW_BLOCK_RE` does not, and should not, tolerate: `END_REVIEW` is a
literal machine token, not free text, so a natural-language stand-in like
`## End Review` still isn't a match. This keeps the "clearly a review,
strict parse fails" scenario alive post-#1346.)

* surface a greppable ``log.warning`` naming host + path,
* print operator-visible output clearly distinct from "no verdict reported",
* open the editor seeded with the recovered excerpt (never blank),
* default the verdict prompt to the detected verdict word.

The operator still confirms; nothing is auto-recorded.

Test coverage:
  1. ``detect_unparsed_review_marker`` unit tests
  2. Local floor (``_review_findings_from_transcript``) + diagnostic out-param
  3. Remote floor (``_fetch_remote_review_findings``) + diagnostic out-param
  4. End-to-end ``_prompt_and_relay_review_verdict`` with CLI output assertions
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coord.review import REVIEWER_SYSTEM_PROMPT

# ── Fixtures: real-world transcript shapes ───────────────────────────────────

# The #873 / #1346 shape, updated for post-#1346 `main`: bolded VERDICT/BODY
# markers (now tolerated on their own by `_REVIEW_BLOCK_RE`'s `_MD` class),
# combined with a terminator that ISN'T just decorated `END_REVIEW` but a
# different literal string entirely (a markdown heading, "## End Review",
# not the required `END_REVIEW` token). `_MD` only absorbs emphasis/code-span/
# heading punctuation immediately around a marker token — it can't turn one
# literal string into another, so this still fails the strict parser while
# remaining a realistic "reviewer wrote a plausible variant" shape.
_BOLDED_MARKERS_TEXT = """\
**REVIEW_VERDICT: request-changes**

**REVIEW_BODY:**

## Summary

This PR has critical issues.

### Blocking

- `coord/review.py:42`: wrong path

## End Review
"""

# A genuinely broken block: marker present but the terminator is absent.
# Must NOT contain the substring "END_REVIEW" anywhere — that would satisfy
# the _REVIEW_BLOCK_RE guard inside detect_unparsed_review_marker and cause
# the detector to return None (false negative).
_NO_TERMINATOR_TEXT = """\
REVIEW_VERDICT: request-changes
REVIEW_BODY:

Some findings here.  The block was truncated before the closing marker.
"""

# The #1427 canonical specimen (efc198d6475a.log): a COMPLETE, well-formed
# `approve` verdict with a full body that ends its prose naturally — the
# reviewer simply stopped after the last sentence instead of also writing
# `END_REVIEW`. Distinct from `_NO_TERMINATOR_TEXT` above (which reads as
# mid-body truncation): this one reads as "finished and forgot the
# terminator," the actual failure mode #1427 measured at 4% of emitted
# verdicts. Must still be rejected by the strict parser — a complete-looking
# body is not proof the session didn't die one line early — and must still
# surface via the diagnostic so it isn't silently dropped.
_COMPLETE_BODY_NO_TERMINATOR_TEXT = """\
REVIEW_VERDICT: approve
REVIEW_BODY:

Reviewed the diff against the checklist. Tests cover the new code path,
error handling matches project conventions, and the change stays within
the files listed in the issue.

No test-coverage gaps, scope violations, or security issues found. Approving.
"""

# A transcript where strict parsing SUCCEEDS (standard format).
_CLEAN_TEXT = """\
REVIEW_VERDICT: approve
REVIEW_BODY:

LGTM — well done.

END_REVIEW
"""

# A transcript with no REVIEW_VERDICT: marker at all.
_NO_MARKER_TEXT = "This is a work session with commits pushed.  No review here."

# The assignment_id and issue number embedded in the briefing — used in
# attribution-gate checks.
_AID = "aabbcc112233"
_ISSUE = 873

# Helper: wrap plain text in a minimal stream-json envelope (what Claude Code
# produces) so we can test the floor with jsonl-shaped transcripts too.
def _stream_json_line(text: str) -> str:
    msg = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    return json.dumps(msg)


# ── 1. detect_unparsed_review_marker unit tests ──────────────────────────────

class TestDetectUnparsedReviewMarker:
    """detect_unparsed_review_marker is a diagnostic, not a parser."""

    def _call(self, text: str, **kw):
        from coord.review import detect_unparsed_review_marker
        return detect_unparsed_review_marker(text, **kw)

    def test_bolded_markers_returns_marker(self) -> None:
        """Bolded VERDICT/BODY markers + a non-literal terminator variant.
        Strict parser fails; detector should fire."""
        result = self._call(_BOLDED_MARKERS_TEXT)
        assert result is not None
        assert result.verdict_word == "request-changes"
        assert "REVIEW_VERDICT" in result.excerpt

    def test_no_terminator_returns_marker(self) -> None:
        """Broken block (marker, no END_REVIEW): detector fires, verdict normalised."""
        result = self._call(_NO_TERMINATOR_TEXT)
        assert result is not None
        assert result.verdict_word == "request-changes"

    def test_complete_body_without_terminator_fires_with_excerpt(self) -> None:
        """#1427 canonical specimen: a complete, correct `approve` review that
        simply stops without `END_REVIEW`. Strict parse must reject it (see
        the paired `test_complete_body_without_terminator_returns_none` in
        `tests/test_review.py`) and — since #1348 — this diagnostic must fire
        with an excerpt containing the review's actual closing text, not just
        a bare "marker seen" flag, so the operator can salvage the verdict."""
        result = self._call(_COMPLETE_BODY_NO_TERMINATOR_TEXT)
        assert result is not None
        assert result.verdict_word == "approve"
        assert "No test-coverage gaps" in result.excerpt
        assert "Approving." in result.excerpt

    def test_no_marker_returns_none(self) -> None:
        """No REVIEW_VERDICT: → None (genuinely not a review)."""
        result = self._call(_NO_MARKER_TEXT)
        assert result is None

    def test_strict_parse_succeeds_returns_none(self) -> None:
        """Strict parse would succeed → None (no double-report).
        The guard inside detect_unparsed_review_marker prevents false alarms."""
        result = self._call(_CLEAN_TEXT)
        assert result is None

    def test_bolded_verdict_word_stripped(self) -> None:
        """Markdown decorators are stripped from the detected verdict word."""
        text = "**REVIEW_VERDICT: request-changes**\n\nSome findings.\n"
        result = self._call(text)
        assert result is not None
        assert result.verdict_word == "request-changes"

    def test_alias_pass_preserved(self) -> None:
        """Alias 'pass' is preserved as-is in verdict_word (normalisation is the
        caller's responsibility, not the detector's)."""
        text = "REVIEW_VERDICT: pass\nSome output.\n"
        result = self._call(text)
        assert result is not None
        assert result.verdict_word == "pass"

    def test_excerpt_capped(self) -> None:
        """Excerpt is capped at _DIAGNOSTIC_EXCERPT_MAX chars."""
        from coord.review import _DIAGNOSTIC_EXCERPT_MAX
        long_body = "x" * (_DIAGNOSTIC_EXCERPT_MAX * 3)
        text = f"REVIEW_VERDICT: request-changes\n{long_body}"
        result = self._call(text)
        assert result is not None
        assert len(result.excerpt) <= _DIAGNOSTIC_EXCERPT_MAX

    def test_transcript_path_and_host_threaded_through(self) -> None:
        """transcript_path and host metadata are passed through."""
        result = self._call(
            _BOLDED_MARKERS_TEXT,
            transcript_path="/tmp/test.jsonl",
            host="elitebook",
        )
        assert result is not None
        assert result.transcript_path == "/tmp/test.jsonl"
        assert result.host == "elitebook"

    def test_empty_verdict_line_gives_none_word(self) -> None:
        """REVIEW_VERDICT: with nothing after the colon → verdict_word=None."""
        text = "REVIEW_VERDICT:\n\nSome text.\n"
        result = self._call(text)
        # There IS a marker (no END_REVIEW, so strict parse fails).
        assert result is not None
        assert result.verdict_word is None

    def test_excerpt_starts_at_marker_line(self) -> None:
        """Excerpt starts at the beginning of the REVIEW_VERDICT: line."""
        preamble = "Lots of prose before the verdict.\n" * 20
        text = preamble + "REVIEW_VERDICT: approve\nSome body.\n"
        result = self._call(text)
        assert result is not None
        # Excerpt should start with REVIEW_VERDICT:, not include the preamble.
        assert result.excerpt.startswith("REVIEW_VERDICT:")

    def test_stream_json_log_with_system_prompt_header_defeats_naive_fix(self) -> None:
        """#1348 round 2 regression (efc198d6475a.log).

        A real ``claude -p --output-format stream-json`` log has TWO traps a
        naive fix must survive at once:

        1. Line 1 is the agent's non-JSON ``# argv=...`` header, which embeds
           the reviewer's OWN ``--system-prompt`` argument — newline-escaped
           onto that one physical line by ``agent.py``
           (``shlex.join(argv).replace("\\n", "\\\\n")``). That system prompt
           CONTAINS the literal ``REVIEW_VERDICT: approve\\nREVIEW_BODY:\\n
           <your full review text in markdown>\\nEND_REVIEW`` template.
        2. The real verdict, emitted later by the assistant, is itself a
           JSON-escaped string (real newlines stored as literal ``\\n``) and,
           in this fixture, uses a terminator variant (``## End Review``, not
           the literal ``END_REVIEW`` token) that ``_REVIEW_BLOCK_RE`` still
           legitimately rejects post-#1346 (bolded VERDICT/BODY markers alone
           are now tolerated — see ``_BOLDED_MARKERS_TEXT`` above), so the
           diagnostic must fire.

        A fix that only unescapes ``\\n`` across the whole raw log and then
        `.search()`es (first match) would match the HEADER's template, not
        the real verdict — "worse than failing" per #1348, because it looks
        like it worked. This fixture defeats that naive fix: the header must
        be excluded entirely (it isn't JSON), and only the real, later
        verdict may be reported.
        """
        header_argv = (
            "claude -p --output-format stream-json --system-prompt "
            + REVIEWER_SYSTEM_PROMPT.replace("\n", "\\n")
            + " --model sonnet"
        )
        header = f"# agent=elitebook repo=coord issue=#{_ISSUE} argv={header_argv}\n"
        real_verdict_text = (
            f"[Coordinator review assignment {_AID} for issue #{_ISSUE}]\n\n"
            "**REVIEW_VERDICT: request-changes**\n\n"
            "**REVIEW_BODY:**\n\n"
            "## Summary\n\nSomething is genuinely wrong here.\n\n## End Review"
        )
        log_text = header + _stream_json_line(real_verdict_text) + "\n"

        result = self._call(log_text)

        assert result is not None
        assert result.verdict_word == "request-changes"
        # The excerpt must come from the REAL (decoded, real-newline) verdict
        # — not the header's raw, still-escaped system-prompt template.
        assert "Something is genuinely wrong here" in result.excerpt
        assert "your full review text in markdown" not in result.excerpt
        assert "\\n" not in result.excerpt


# ── 2. Local floor diagnostic out-param tests ────────────────────────────────

class TestLocalFloorDiagnostic:
    """_review_findings_from_transcript populates _diagnostic when strict parse
    fails but attribution gates pass."""

    def _call(self, projects_dir, *, assignment_id=_AID, issue_number=_ISSUE,
              started_at=1_000_000.0, _diagnostic=None):
        from coord.interactive import _review_findings_from_transcript
        return _review_findings_from_transcript(
            issue_number,
            started_at,
            assignment_id=assignment_id,
            projects_dir=projects_dir,
            _diagnostic=_diagnostic,
        )

    def _write_transcript(self, projects_dir: Path, text: str, mtime: float = 2_000_000.0) -> Path:
        """Write a plain-text transcript that passes the attribution gates."""
        # Attribution requires: issue number AND assignment_id in the transcript.
        attributed = f"[Coordinator review assignment {_AID} for issue #{_ISSUE}]\n{text}"
        proj = projects_dir / "proj1"
        proj.mkdir(parents=True, exist_ok=True)
        p = proj / "session.jsonl"
        p.write_text(attributed, encoding="utf-8")
        # Set mtime past cutoff so the floor picks it up.
        import os
        os.utime(p, (mtime, mtime))
        return p

    def test_happy_path_parse_success_diagnostic_empty(self, tmp_path) -> None:
        """When strict parse succeeds, _diagnostic stays empty."""
        self._write_transcript(tmp_path, _CLEAN_TEXT)
        diagnostic: list = []
        result = self._call(tmp_path, _diagnostic=diagnostic)
        assert result is not None
        assert result.verdict == "approve"
        # Strict parse succeeded → no diagnostic.
        assert diagnostic == []

    def test_bolded_markers_populates_diagnostic(self, tmp_path) -> None:
        """The #1346 shape: bolded markers fail strict parse → marker appended."""
        self._write_transcript(tmp_path, _BOLDED_MARKERS_TEXT)
        diagnostic: list = []
        result = self._call(tmp_path, _diagnostic=diagnostic)
        assert result is None  # strict parse failed
        assert len(diagnostic) == 1
        assert diagnostic[0].verdict_word == "request-changes"
        assert "REVIEW_VERDICT" in diagnostic[0].excerpt

    def test_no_terminator_populates_diagnostic(self, tmp_path) -> None:
        """Marker without END_REVIEW → diagnostic fired."""
        self._write_transcript(tmp_path, _NO_TERMINATOR_TEXT)
        diagnostic: list = []
        result = self._call(tmp_path, _diagnostic=diagnostic)
        assert result is None
        assert len(diagnostic) == 1
        assert diagnostic[0].verdict_word == "request-changes"

    def test_no_marker_diagnostic_empty(self, tmp_path) -> None:
        """No REVIEW_VERDICT: in transcript → _diagnostic stays empty."""
        self._write_transcript(tmp_path, _NO_MARKER_TEXT)
        diagnostic: list = []
        result = self._call(tmp_path, _diagnostic=diagnostic)
        assert result is None
        assert diagnostic == []

    def test_diagnostic_none_no_error(self, tmp_path) -> None:
        """When _diagnostic=None (default), no error even when parse fails."""
        self._write_transcript(tmp_path, _BOLDED_MARKERS_TEXT)
        # Callers that don't pass _diagnostic get the old silent behaviour.
        result = self._call(tmp_path, _diagnostic=None)
        assert result is None  # no crash

    def test_attribution_gates_prevent_unrelated_transcript(self, tmp_path) -> None:
        """A transcript that doesn't name this assignment_id is NOT collected,
        even when it has an unparsed marker (#989)."""
        # Write transcript without the assignment_id.
        proj = tmp_path / "proj2"
        proj.mkdir(parents=True, exist_ok=True)
        p = proj / "session.jsonl"
        unrelated = f"For issue #{_ISSUE} only (no assignment id).\n{_BOLDED_MARKERS_TEXT}"
        p.write_text(unrelated, encoding="utf-8")
        import os; os.utime(p, (2_000_000.0, 2_000_000.0))

        diagnostic: list = []
        result = self._call(tmp_path, _diagnostic=diagnostic)
        assert result is None
        # No diagnostic because assignment_id gate failed.
        assert diagnostic == []

    def test_only_newest_marker_collected(self, tmp_path) -> None:
        """When multiple transcripts have unparsed markers, only the newest is
        appended (first in the newest-first iteration order)."""
        # Write two transcripts: newer has one path, older has another.
        proj1 = tmp_path / "proj1"
        proj1.mkdir(parents=True, exist_ok=True)
        proj2 = tmp_path / "proj2"
        proj2.mkdir(parents=True, exist_ok=True)

        newer = f"[Coordinator review assignment {_AID} for issue #{_ISSUE}]\n{_BOLDED_MARKERS_TEXT}"
        older = f"[Coordinator review assignment {_AID} for issue #{_ISSUE}]\n{_NO_TERMINATOR_TEXT}"

        p1 = proj1 / "newer.jsonl"
        p1.write_text(newer, encoding="utf-8")
        p2 = proj2 / "older.jsonl"
        p2.write_text(older, encoding="utf-8")

        import os
        os.utime(p1, (3_000_000.0, 3_000_000.0))  # newer
        os.utime(p2, (2_000_000.0, 2_000_000.0))  # older

        diagnostic: list = []
        result = self._call(tmp_path, _diagnostic=diagnostic)
        assert result is None
        # Only the first (newest) marker is collected.
        assert len(diagnostic) == 1
        assert diagnostic[0].transcript_path == str(p1)


# ── 3. Remote floor diagnostic out-param tests ───────────────────────────────

class TestRemoteFloorDiagnostic:
    """_fetch_remote_review_findings populates _diagnostic on parse failure."""

    def _call(self, monkeypatch, text: str, *, attribution: bool = True,
              _diagnostic=None, issue_number=_ISSUE, assignment_id=_AID,
              parse_succeeds: bool = False):
        """Drive _fetch_remote_review_findings with monkeypatched subprocess calls.

        *text* is the content 'cat' would return for the matched transcript.
        When *attribution* is True the text is wrapped with the issue/aid markers
        so the gates pass; when False they are omitted so the gates block.
        *parse_succeeds*: when True, the text is a well-formed review that strict-
        parses successfully (to test that diagnostic is NOT fired on a clean parse).
        """
        import subprocess as sp
        from coord.interactive import _fetch_remote_review_findings

        # listing: one candidate transcript active after cutoff.
        listing_output = f"2000000.0\t/remote/home/.claude/projects/p/s.jsonl\n"

        # cat output: the attributed transcript content.
        if attribution:
            cat_output = (
                f"[Coordinator review assignment {assignment_id} "
                f"for issue #{issue_number}]\n{text}"
            )
        else:
            cat_output = text

        call_n = 0

        def _fake_run(cmd, **kwargs):
            nonlocal call_n
            call_n += 1
            result = sp.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if call_n == 1:
                # listing call
                result.stdout = listing_output
            else:
                # cat call
                result.stdout = cat_output
            return result

        monkeypatch.setattr(sp, "run", _fake_run)

        return _fetch_remote_review_findings(
            issue_number,
            cutoff=1_000_000.0,
            ssh_target="elitebook",
            assignment_id=assignment_id,
            _diagnostic=_diagnostic,
        )

    def test_bolded_markers_populates_diagnostic(self, monkeypatch) -> None:
        """Remote: #1346 shape populates _diagnostic with host+path info."""
        diagnostic: list = []
        result = self._call(monkeypatch, _BOLDED_MARKERS_TEXT, _diagnostic=diagnostic)
        assert result is None
        assert len(diagnostic) == 1
        assert diagnostic[0].verdict_word == "request-changes"
        assert diagnostic[0].host == "elitebook"
        assert diagnostic[0].transcript_path == "/remote/home/.claude/projects/p/s.jsonl"

    def test_parse_success_diagnostic_empty(self, monkeypatch) -> None:
        """Remote: strict parse succeeds → _diagnostic empty, findings returned."""
        diagnostic: list = []
        result = self._call(monkeypatch, _CLEAN_TEXT, _diagnostic=diagnostic)
        assert result is not None
        assert result.verdict == "approve"
        assert diagnostic == []

    def test_attribution_gate_blocks_unrelated(self, monkeypatch) -> None:
        """Remote: transcript without attribution → _diagnostic not populated."""
        diagnostic: list = []
        result = self._call(
            monkeypatch, _BOLDED_MARKERS_TEXT, attribution=False, _diagnostic=diagnostic
        )
        assert result is None
        assert diagnostic == []

    def test_diagnostic_none_no_error(self, monkeypatch) -> None:
        """Remote: _diagnostic=None (default) → no crash when parse fails."""
        result = self._call(monkeypatch, _BOLDED_MARKERS_TEXT, _diagnostic=None)
        assert result is None


# ── 4. End-to-end _prompt_and_relay_review_verdict CLI tests ─────────────────

class _Out:
    """Minimal StoreOutcome stub."""
    posted = True
    error = None
    findings_written = True


def _relay(monkeypatch, *, assignment_id="rev-1348", issue_number=_ISSUE,
           is_tty: bool = True, answers: list | None = None,
           edit_returns: str | None = "Confirmed review body.",
           post_result_raises: Exception | None = None,
           **kw):
    """Drive _prompt_and_relay_review_verdict.

    Returns (recorded_record, relay_ok, all_output, editor_seed).
    *all_output*: everything echoed via click.echo, joined.
    *editor_seed*: the text passed to click.edit (None if editor never opened).
    """
    from coord.cli import _prompt_and_relay_review_verdict

    # Board empty.
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings",
        lambda aid: None,
    )

    recorded: dict = {}
    if post_result_raises:
        def _post(rec):
            raise post_result_raises
    else:
        def _post(rec):
            recorded["rec"] = rec
            return _Out()
    monkeypatch.setattr("coord.issue_store.post_result", _post)

    monkeypatch.setattr("sys.stdin.isatty", lambda: is_tty)

    outputs: list[str] = []
    monkeypatch.setattr(
        "click.echo",
        lambda msg="", **kwargs: outputs.append(str(msg or "")),
    )

    prompt_calls: list[dict] = []
    _answers_iter = iter(answers or [])

    def _fake_prompt(text, **kwargs):
        prompt_calls.append({"text": text, **kwargs})
        return next(_answers_iter)

    monkeypatch.setattr("click.prompt", _fake_prompt)

    editor_seed: list[str | None] = [None]

    def _fake_edit(seed, **kwargs):
        editor_seed[0] = seed
        return edit_returns

    monkeypatch.setattr("click.edit", _fake_edit)

    ok = _prompt_and_relay_review_verdict(
        assignment_id=assignment_id,
        repo_name="claude-coordinator",
        repo_github="JDonaghy/claude-coordinator",
        issue_number=issue_number,
        machine_name="elitebook",
        verdict_cmd_hint="HINT",
        started_at=1_000_000.0,
        **kw,
    )
    return recorded.get("rec"), ok, "\n".join(outputs), editor_seed[0]


def _stub_transcript_with_marker(monkeypatch, *, verdict_word="request-changes",
                                  excerpt="EXCERPT-TEXT", transcript_path="/tmp/t.jsonl",
                                  host=None):
    """Replace _review_findings_from_transcript so it returns None but populates
    _diagnostic with one UnparsedReviewMarker.  Both the remote and local paths
    call the same function."""
    from coord.review import UnparsedReviewMarker

    def _fake(*args, _diagnostic=None, **kwargs):
        if _diagnostic is not None and not _diagnostic:
            _diagnostic.append(UnparsedReviewMarker(
                verdict_word=verdict_word,
                excerpt=excerpt,
                transcript_path=transcript_path,
                host=host,
            ))
        return None

    monkeypatch.setattr("coord.interactive._review_findings_from_transcript", _fake)


class TestPromptAndRelayVerdictParseFailed:
    """_prompt_and_relay_review_verdict when parse failed but marker was found."""

    def test_parse_failure_message_distinct_from_no_verdict(self, monkeypatch) -> None:
        """The 'REVIEW PARSE FAILED' message MUST appear; the generic
        'no verdict reported' standalone message must NOT — they indicate two
        completely different failure modes with different recovery actions."""
        _stub_transcript_with_marker(monkeypatch, excerpt="The review body here.")
        _, ok, output, _ = _relay(
            monkeypatch,
            answers=["r", ""],  # verdict=request-changes, summary=
        )
        assert "REVIEW PARSE FAILED" in output
        # The standalone "no verdict reported" message (the generic path, used
        # when there is truly no review) must not appear as a lead.
        # Note: the explanation text says "This is NOT 'no verdict reported'",
        # so we guard against the *lead* form that ends with " — record it with:"
        assert "no verdict reported — record it with:" not in output

    def test_non_tty_parse_failure_message_distinct(self, monkeypatch) -> None:
        """In headless (non-TTY) mode the parse-failure message is also distinct."""
        _stub_transcript_with_marker(monkeypatch, excerpt="Body text.")
        _, ok, output, _ = _relay(monkeypatch, is_tty=False)
        assert "parse FAILED" in output or "PARSE FAILED" in output
        # "no verdict reported — record it with:" is the generic-path lead; it
        # must not appear when a REVIEW_VERDICT: marker was found.
        assert "no verdict reported — record it with:" not in output
        assert ok is False  # can't open editor without TTY

    def test_editor_seeded_with_excerpt(self, monkeypatch) -> None:
        """The editor opens pre-seeded with the recovered excerpt (#1348 / #877)."""
        excerpt = "**REVIEW_VERDICT: request-changes**\n\nSpecific findings here."
        _stub_transcript_with_marker(monkeypatch, excerpt=excerpt)
        _, ok, _, editor_seed = _relay(
            monkeypatch,
            answers=["r", ""],
            edit_returns="Operator cleaned up body.",
        )
        # click.edit was called (editor opened for request-changes without _pre_body).
        assert editor_seed is not None
        # The excerpt must be part of the seed passed to click.edit.
        assert "REVIEW_VERDICT" in editor_seed

    def test_prompt_defaults_to_detected_verdict(self, monkeypatch) -> None:
        """When marker line carries a recognisable verdict, prompt defaults to it
        (not [s]kip)."""
        _stub_transcript_with_marker(monkeypatch, verdict_word="request-changes")

        prompt_defaults: list = []

        def _spy_prompt(text, **kwargs):
            prompt_defaults.append(kwargs.get("default", "s"))
            return kwargs.get("default", "s")  # accept the default

        monkeypatch.setattr("click.prompt", _spy_prompt)
        monkeypatch.setattr("click.echo", lambda *a, **k: None)
        monkeypatch.setattr("click.edit", lambda *a, **k: "Some body.")

        from coord.cli import _prompt_and_relay_review_verdict
        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings", lambda aid: None
        )
        captured: dict = {}
        monkeypatch.setattr(
            "coord.issue_store.post_result",
            lambda rec: (captured.setdefault("rec", rec), _Out())[1],
        )
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        _prompt_and_relay_review_verdict(
            assignment_id="rev-1348",
            repo_name="c-coord",
            repo_github="acme/coord",
            issue_number=_ISSUE,
            machine_name="elitebook",
            verdict_cmd_hint="HINT",
            started_at=1_000_000.0,
        )

        # First click.prompt call is the verdict prompt.
        assert prompt_defaults, "click.prompt was never called"
        # Default should be "r" (request-changes), not "s" (skip).
        assert prompt_defaults[0] == "r", (
            f"expected prompt default 'r' for detected 'request-changes', got {prompt_defaults[0]!r}"
        )

    def test_prompt_defaults_to_s_when_verdict_unrecognised(self, monkeypatch) -> None:
        """Unrecognised verdict word → default stays [s]kip."""
        _stub_transcript_with_marker(monkeypatch, verdict_word="unknown-word")

        prompt_defaults: list = []

        def _spy_prompt(text, **kwargs):
            prompt_defaults.append(kwargs.get("default", "s"))
            return "s"  # skip

        monkeypatch.setattr("click.prompt", _spy_prompt)
        monkeypatch.setattr("click.echo", lambda *a, **k: None)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings", lambda aid: None
        )

        from coord.cli import _prompt_and_relay_review_verdict
        _prompt_and_relay_review_verdict(
            assignment_id="rev-1348",
            repo_name="c-coord",
            repo_github="acme/coord",
            issue_number=_ISSUE,
            machine_name="elitebook",
            verdict_cmd_hint="HINT",
            started_at=1_000_000.0,
        )

        assert prompt_defaults and prompt_defaults[0] == "s"

    def test_confirmed_verdict_reaches_post_result(self, monkeypatch) -> None:
        """After operator confirms, the verdict reaches issue_store.post_result."""
        _stub_transcript_with_marker(
            monkeypatch, excerpt="Blocking finding here.", verdict_word="request-changes"
        )
        rec, ok, _, _ = _relay(
            monkeypatch,
            answers=["r", ""],
            edit_returns="Confirmed findings body.",
        )
        assert ok is True
        assert rec is not None
        assert rec.verdict == "request-changes"
        assert rec.findings_body == "Confirmed findings body."

    def test_approve_detected_verdict_relayed_when_confirmed(self, monkeypatch) -> None:
        """When the marker says 'approve' and operator confirms, approve is recorded."""
        _stub_transcript_with_marker(
            monkeypatch, excerpt="LGTM.", verdict_word="approve"
        )
        rec, ok, _, editor_seed = _relay(
            monkeypatch,
            answers=["a", ""],  # operator confirms approve
        )
        assert ok is True
        assert rec is not None
        assert rec.verdict == "approve"
        # No editor for approve.
        assert editor_seed is None

    def test_alias_fail_normalised_to_request_changes_default(self, monkeypatch) -> None:
        """Alias 'fail' → 'request-changes' as the prompt default (#1348)."""
        _stub_transcript_with_marker(monkeypatch, verdict_word="fail")

        prompt_defaults: list = []

        def _spy_prompt(text, **kwargs):
            prompt_defaults.append(kwargs.get("default", "s"))
            return kwargs.get("default", "s")

        monkeypatch.setattr("click.prompt", _spy_prompt)
        monkeypatch.setattr("click.echo", lambda *a, **k: None)
        monkeypatch.setattr("click.edit", lambda *a, **k: "Some body.")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings", lambda aid: None
        )
        monkeypatch.setattr(
            "coord.issue_store.post_result",
            lambda rec: _Out(),
        )

        from coord.cli import _prompt_and_relay_review_verdict
        _prompt_and_relay_review_verdict(
            assignment_id="rev-1348-fail",
            repo_name="c-coord",
            repo_github="acme/coord",
            issue_number=_ISSUE,
            machine_name="elitebook",
            verdict_cmd_hint="HINT",
            started_at=1_000_000.0,
        )

        # 'fail' alias → normalised to 'request-changes' → default 'r'.
        assert prompt_defaults and prompt_defaults[0] == "r"


class TestPromptAndRelayVerdictNoParseFailure:
    """When no marker is found at all, the original "no verdict reported" path
    is unchanged — regression guard."""

    def test_no_marker_shows_no_verdict_message(self, monkeypatch) -> None:
        """No REVIEW_VERDICT: anywhere → the generic 'no verdict reported' lead
        appears (headless can't open an editor); 'PARSE FAILED' must NOT appear."""
        # Transcript floor returns None with empty _diagnostic (no marker).
        def _fake_floor(*args, _diagnostic=None, **kwargs):
            return None  # empty _diagnostic

        monkeypatch.setattr(
            "coord.interactive._review_findings_from_transcript", _fake_floor
        )
        _, ok, output, _ = _relay(monkeypatch, is_tty=False)
        assert ok is False
        assert "no verdict reported — record it with:" in output
        assert "PARSE FAILED" not in output

    def test_successful_parse_not_treated_as_failure(self, monkeypatch) -> None:
        """When transcript floor returns ReviewFindings, the diagnostic path
        is skipped entirely."""
        from types import SimpleNamespace
        good_findings = SimpleNamespace(verdict="approve", body="LGTM")

        def _fake_floor(*args, _diagnostic=None, **kwargs):
            return good_findings  # parse succeeded

        monkeypatch.setattr(
            "coord.interactive._review_findings_from_transcript", _fake_floor
        )
        _, ok, output, _ = _relay(
            monkeypatch,
            answers=["a", ""],
        )
        assert ok is True
        assert "PARSE FAILED" not in output
        assert "no verdict reported" not in output
