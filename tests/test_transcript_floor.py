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


def test_with_coord_on_path_prefixes_agent_venv_bin() -> None:
    # #606 PATH-fix: the session command is prefixed so `coord` resolves (agent
    # self-report path), preserving the original command and the session's PATH.
    out = interactive._with_coord_on_path("claude --interactive")
    assert out.endswith("claude --interactive")  # original command preserved
    assert out.startswith("export PATH=")
    assert "$HOME/.coord-venv/bin" in out  # agent coord bin prepended (literal $HOME)
    assert "$PATH" in out  # session's own PATH preserved for runtime expansion


def _transcript_jsonl(text: str) -> str:
    """A one-message Claude-transcript JSONL string (assistant text block)."""
    return (
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


def test_names_issue_accepts_hash_and_issue_forms() -> None:
    # #617: the real #607 review body said "#607", not "issue-607" — the
    # literal-issue-N gate silently dropped it. Accept BOTH forms, but not a
    # different issue or a longer number.
    names = interactive._transcript_names_issue
    assert names("## Review: #607 — pull-right submenus", 607) is True
    assert names("# Re-review: issue-370 submenu fix", 370) is True
    assert names("fixed on branch issue-607-foo", 607) is True
    assert names("see #6070 for the other thing", 607) is False
    assert names("this is about #608 not ours", 607) is False
    assert names("nothing relevant here", 607) is False


def test_transcript_floor_matches_hash_form_in_body(tmp_path: Path) -> None:
    # Lock the real-world failure: a review whose prose uses "#607" must recover.
    proj = tmp_path / "-home-john-src-claude-coordinator"
    proj.mkdir()
    _write_transcript(
        proj / "sess.jsonl",
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n"
        "## Review: #607 — pull-right submenus\nRight-on-leaf must be a no-op.\n"
        "END_REVIEW",
    )
    findings = interactive._review_findings_from_transcript(
        607, started_at=0.0, projects_dir=tmp_path
    )
    assert findings is not None
    assert findings.verdict == "request-changes"
    assert "Right-on-leaf" in findings.body


def test_transcript_floor_matches_issue_named_outside_body(tmp_path: Path) -> None:
    # #362: the review PROSE describes the code and never names the issue, but
    # the BRIEFING (a user message in the transcript) seeds `issue-362`. The
    # gate must match the WHOLE transcript, not just the parsed review body —
    # otherwise a perfectly good review falls back to the operator prompt.
    proj = tmp_path / "-home-john-src-quadraui"
    proj.mkdir()
    briefing = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Review the PR for "
                     "issue-362-reusable-board-kanban-component."}
                ],
            },
        }
    )
    review = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text":
                        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n"
                        "## Summary\nThe Board primitive needs keyboard-nav tests.\n"
                        "END_REVIEW"}
                ],
            },
        }
    )
    (proj / "sess.jsonl").write_text(briefing + "\n" + review + "\n")
    f = interactive._review_findings_from_transcript(
        362, started_at=0.0, projects_dir=tmp_path
    )
    assert f is not None
    assert f.verdict == "request-changes"
    assert "keyboard-nav tests" in f.body
    assert "362" not in f.body  # the body never names the issue — matched the briefing


def test_remote_transcript_floor_recovers_over_ssh(monkeypatch) -> None:
    """#617/#607: a review that ran on a REMOTE host, reattached + exited from
    another machine, must recover from the SESSION'S OWN host over ssh.  The
    local-only floor was blind to it — leaving the verdict-less operator prompt
    that stranded #607."""
    import subprocess as _sp

    transcript = _transcript_jsonl(
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n"
        "# Review: issue-607 — reattach\nRight-on-leaf must be a no-op.\nEND_REVIEW"
    )

    def fake_run(cmd, *args, **kwargs):
        remote_cmd = cmd[-1]  # ["ssh", *mux_opts, target, <remote_cmd>]
        if remote_cmd.startswith("find "):
            # one candidate, mtime far in the future so it clears the cutoff
            return _sp.CompletedProcess(
                cmd, 0,
                stdout="9999999999.0\t/home/john/.claude/projects/p/s.jsonl\n",
                stderr="",
            )
        if remote_cmd.startswith("cat "):
            return _sp.CompletedProcess(cmd, 0, stdout=transcript, stderr="")
        return _sp.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr("coord.interactive.subprocess.run", fake_run)
    findings = interactive._review_findings_from_transcript(
        607, started_at=0.0, ssh_target="precision"
    )
    assert findings is not None
    assert findings.verdict == "request-changes"
    assert "Right-on-leaf must be a no-op." in findings.body


def test_remote_transcript_floor_ssh_failure_returns_none(monkeypatch) -> None:
    # ssh unreachable → return None so the caller falls through to the operator
    # prompt (which now collects the body) rather than silently losing it.
    import subprocess as _sp

    monkeypatch.setattr(
        "coord.interactive.subprocess.run",
        lambda cmd, *a, **k: _sp.CompletedProcess(cmd, 255, stdout="", stderr="x"),
    )
    assert (
        interactive._review_findings_from_transcript(
            607, started_at=0.0, ssh_target="precision"
        )
        is None
    )


def test_remote_transcript_floor_untagged_returns_none(monkeypatch) -> None:
    # A remote review that doesn't name THIS issue is not trusted (same
    # anti-misattribution rule as the local floor).
    import subprocess as _sp

    transcript = _transcript_jsonl(
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n# issue-999 other\nnope\nEND_REVIEW"
    )

    def fake_run(cmd, *args, **kwargs):
        remote_cmd = cmd[-1]
        if remote_cmd.startswith("find "):
            return _sp.CompletedProcess(
                cmd, 0, stdout="9999999999.0\t/h/.claude/projects/p/s.jsonl\n", stderr=""
            )
        if remote_cmd.startswith("cat "):
            return _sp.CompletedProcess(cmd, 0, stdout=transcript, stderr="")
        return _sp.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr("coord.interactive.subprocess.run", fake_run)
    assert (
        interactive._review_findings_from_transcript(
            607, started_at=0.0, ssh_target="precision"
        )
        is None
    )


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
