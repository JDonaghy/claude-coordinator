"""Transcript-floor: recover a review's verdict + findings from the Claude
session transcript when the agent couldn't run `coord report-result` (#606)."""

import json
from pathlib import Path

from coord import interactive


def _write_transcript(path: Path, text: str) -> None:
    """Write a one-message Claude-transcript JSONL (assistant text block)."""
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n"
    )


def test_transcript_floor_recovers_request_changes(tmp_path: Path) -> None:
    proj = tmp_path / "-home-john-src-quadraui"
    proj.mkdir()
    _write_transcript(
        proj / "sess.jsonl",
        "The `coord` command isn't available. Outputting the verdict:\n\n"
        "REVIEW_VERDICT: request-changes\n"
        "REVIEW_BODY:\n"
        "# Re-review: issue-370 — submenu fix\n\nMissing keyboard-nav tests.\n"
        "END_REVIEW",
    )
    findings = interactive._review_findings_from_transcript(
        370, started_at=0.0, projects_dir=tmp_path
    )
    assert findings is not None
    assert findings.verdict == "request-changes"
    assert "Missing keyboard-nav tests." in findings.body


def test_transcript_floor_ignores_work_session_without_verdict(tmp_path: Path) -> None:
    # A work session emits no REVIEW_VERDICT block → no-op (falls through to git-floor).
    proj = tmp_path / "-home-john-src-quadraui"
    proj.mkdir()
    _write_transcript(proj / "sess.jsonl", "Implemented the feature and pushed a branch. Done.")
    assert (
        interactive._review_findings_from_transcript(370, started_at=0.0, projects_dir=tmp_path)
        is None
    )


def test_transcript_floor_prefers_issue_tagged_review(tmp_path: Path) -> None:
    # Two review transcripts in the window; the one naming THIS issue wins, so a
    # concurrent unrelated review can't be mis-attributed.
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-b").mkdir()
    _write_transcript(
        tmp_path / "proj-a" / "other.jsonl",
        "REVIEW_VERDICT: approve\nREVIEW_BODY:\n# Review: issue-999 unrelated\nlgtm\nEND_REVIEW",
    )
    _write_transcript(
        tmp_path / "proj-b" / "mine.jsonl",
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n# Review: issue-370 mine\nfix X\nEND_REVIEW",
    )
    findings = interactive._review_findings_from_transcript(
        370, started_at=0.0, projects_dir=tmp_path
    )
    assert findings is not None
    assert findings.verdict == "request-changes"
    assert "issue-370" in findings.body


def test_transcript_floor_untagged_review_returns_none(tmp_path: Path) -> None:
    # A review that doesn't name THIS issue is not trusted (no guess-the-only-one
    # fallback) — defer to the human-prompt backstop rather than mis-attribute.
    (tmp_path / "a").mkdir()
    _write_transcript(
        tmp_path / "a" / "x.jsonl",
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n# Review of issue-999\nnope\nEND_REVIEW",
    )
    assert (
        interactive._review_findings_from_transcript(370, started_at=0.0, projects_dir=tmp_path)
        is None
    )
