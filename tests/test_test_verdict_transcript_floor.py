"""#1351: the Test-gate transcript-floor — recover a human-attended smoke
session's TEST_VERDICT block from the Claude session transcript when the
agent couldn't (or didn't) run `coord test`.

Mirrors tests/test_transcript_floor.py (the #606 review floor) exactly:
same local-scan shape, same #989 issue-number + assignment-id double-gate.
"""

from __future__ import annotations

import json
from pathlib import Path

from coord import interactive
from coord.review import TestVerdictFindings


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


def test_test_verdict_floor_recovers_failed(tmp_path: Path) -> None:
    proj = tmp_path / "-home-john-src-quadraui"
    proj.mkdir()
    _write_transcript(
        proj / "sess.jsonl",
        "[Coordinator smoke assignment smoke111] HUMAN-ATTENDED interactive "
        "smoke test. `coord` isn't available here. Outputting the verdict:\n\n"
        "TEST_VERDICT: failed\n"
        "TEST_REASON:\n"
        "Checked the submenu on issue-370; arrow-down closes the menu "
        "instead of moving focus.\n"
        "END_TEST",
    )
    findings = interactive._test_verdict_from_transcript(
        370, started_at=0.0, assignment_id="smoke111", projects_dir=tmp_path
    )
    assert findings is not None
    assert isinstance(findings, TestVerdictFindings)
    assert findings.verdict == "failed"
    assert "arrow-down closes the menu" in findings.reason


def test_test_verdict_floor_recovers_passed(tmp_path: Path) -> None:
    proj = tmp_path / "-home-john-src-quadraui"
    proj.mkdir()
    _write_transcript(
        proj / "sess.jsonl",
        "[Coordinator smoke assignment smoke111] HUMAN-ATTENDED interactive "
        "smoke test for issue-370.\n\n"
        "TEST_VERDICT: passed\n"
        "TEST_REASON:\n"
        "All checks passed.\n"
        "END_TEST",
    )
    findings = interactive._test_verdict_from_transcript(
        370, started_at=0.0, assignment_id="smoke111", projects_dir=tmp_path
    )
    assert findings is not None
    assert findings.verdict == "passed"


def test_test_verdict_floor_ignores_session_without_verdict(tmp_path: Path) -> None:
    # A work (non-smoke) session's transcript carries no TEST_VERDICT block →
    # no-op, same self-gating the review floor relies on.
    proj = tmp_path / "-home-john-src-quadraui"
    proj.mkdir()
    _write_transcript(proj / "sess.jsonl", "Implemented the feature and pushed a branch. Done.")
    assert (
        interactive._test_verdict_from_transcript(
            370, started_at=0.0, assignment_id="smoke111", projects_dir=tmp_path
        )
        is None
    )


def test_test_verdict_floor_double_gate_issue_and_assignment(tmp_path: Path) -> None:
    # #989 for the Test gate: two smoke transcripts in the window: only the
    # one naming THIS issue AND this exact assignment id wins.
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-b").mkdir()
    _write_transcript(
        tmp_path / "proj-a" / "other.jsonl",
        "[Coordinator smoke assignment smoke222] "
        "TEST_VERDICT: passed\nTEST_REASON:\nfor issue-999, unrelated.\nEND_TEST",
    )
    _write_transcript(
        tmp_path / "proj-b" / "mine.jsonl",
        "[Coordinator smoke assignment smoke111] "
        "TEST_VERDICT: failed\nTEST_REASON:\nfor issue-370, broken build.\nEND_TEST",
    )
    findings = interactive._test_verdict_from_transcript(
        370, started_at=0.0, assignment_id="smoke111", projects_dir=tmp_path
    )
    assert findings is not None
    assert findings.verdict == "failed"
    assert "broken build" in findings.reason


def test_test_verdict_floor_no_started_at_is_noop() -> None:
    assert (
        interactive._test_verdict_from_transcript(
            370, started_at=None, assignment_id="smoke111"
        )
        is None
    )


def test_test_verdict_floor_remote(monkeypatch, tmp_path: Path) -> None:
    """The ssh-remote variant (#617's analog): list + cat the session's own
    host, same as the review floor's ``_fetch_remote_review_findings``."""
    listing_out = "12345.0\t/home/op/.claude/projects/foo/sess.jsonl\n"
    transcript_text = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "text",
                        "text": (
                            "[Coordinator smoke assignment smoke111] "
                            "TEST_VERDICT: failed\nTEST_REASON:\n"
                            "issue-370 crashes on launch.\nEND_TEST"
                        ),
                    }],
                },
            }
        )
        + "\n"
    )

    class _Result:
        def __init__(self, stdout: str, returncode: int = 0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    calls = {"n": 0}

    def _fake_run(argv, **kwargs):
        calls["n"] += 1
        joined = " ".join(argv)
        if "find " in joined:
            return _Result(listing_out)
        if "cat " in joined:
            return _Result(transcript_text)
        return _Result("", returncode=1)

    monkeypatch.setattr(interactive.subprocess, "run", _fake_run)

    findings = interactive._test_verdict_from_transcript(
        370, started_at=0.0, assignment_id="smoke111", ssh_target="build-box",
    )
    assert findings is not None
    assert findings.verdict == "failed"
    assert "crashes on launch" in findings.reason
