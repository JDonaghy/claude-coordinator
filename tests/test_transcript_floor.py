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
        "[Coordinator review assignment aaa111] "
        "The `coord` command isn't available. Outputting the verdict:\n\n"
        "REVIEW_VERDICT: request-changes\n"
        "REVIEW_BODY:\n"
        "# Re-review: issue-370 — submenu fix\n\nMissing keyboard-nav tests.\n"
        "END_REVIEW",
    )
    findings = interactive._review_findings_from_transcript(
        370, started_at=0.0, assignment_id="aaa111", projects_dir=tmp_path
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
        interactive._review_findings_from_transcript(
            370, started_at=0.0, assignment_id="aaa111", projects_dir=tmp_path
        )
        is None
    )


def test_transcript_floor_prefers_issue_tagged_review(tmp_path: Path) -> None:
    # Two review transcripts in the window; the one naming THIS issue AND this
    # assignment id wins, so a concurrent unrelated review can't be mis-attributed.
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-b").mkdir()
    _write_transcript(
        tmp_path / "proj-a" / "other.jsonl",
        "[Coordinator review assignment bbb222] "
        "REVIEW_VERDICT: approve\nREVIEW_BODY:\n# Review: issue-999 unrelated\nlgtm\nEND_REVIEW",
    )
    _write_transcript(
        tmp_path / "proj-b" / "mine.jsonl",
        "[Coordinator review assignment aaa111] "
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n# Review: issue-370 mine\nfix X\nEND_REVIEW",
    )
    findings = interactive._review_findings_from_transcript(
        370, started_at=0.0, assignment_id="aaa111", projects_dir=tmp_path
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


def test_transcript_floor_sibling_issue_cross_reference_not_mis_attributed(
    tmp_path: Path,
) -> None:
    """#989 regression: sibling issues under the same epic cross-reference each
    other's numbers in prose, so the issue-number substring check alone can be
    satisfied by a DIFFERENT, genuinely-dead assignment's own review transcript.

    Reproduces the real incident: #930's review transcript legitimately
    contains "issue-931" (cross-referenced in the shared epic docs), and is
    newer than #931's own (never-happened) session. Without the assignment_id
    gate this would wrongly recover #930's verdict for #931's assignment
    ``fb5f6ed35cc2``. With the gate, since #930's transcript does not carry
    ``fb5f6ed35cc2``, recovery must return None — falling through to the
    advisory/failure path instead of stamping the wrong verdict.
    """
    proj = tmp_path / "-home-john-src-claude-coordinator"
    proj.mkdir()
    _write_transcript(
        proj / "sess-930.jsonl",
        "[Coordinator review assignment 9f1c1ffabdf5] "
        "coord report-result --assignment 9f1c1ffabdf5 --status done "
        "--verdict request-changes\n\n"
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n"
        "# Review of #930\nSee the epic #929 — this also affects issue-931 "
        "downstream.\nEND_REVIEW",
    )
    # #931's own assignment (fb5f6ed35cc2) never produced a transcript — the
    # session was genuinely dead.
    findings = interactive._review_findings_from_transcript(
        931, started_at=0.0, assignment_id="fb5f6ed35cc2", projects_dir=tmp_path
    )
    assert findings is None, (
        "must NOT recover #930's transcript for #931's assignment just "
        "because #930's prose mentions issue-931"
    )


def test_transcript_floor_matches_own_assignment_even_if_issue_named_elsewhere(
    tmp_path: Path,
) -> None:
    # The flip side of the #989 regression: #931's OWN transcript (carrying
    # its own assignment id) must still recover normally even though a sibling
    # issue's transcript also happens to name #931 in this same window.
    proj = tmp_path / "-home-john-src-claude-coordinator"
    proj.mkdir()
    _write_transcript(
        proj / "sess-930.jsonl",
        "[Coordinator review assignment 9f1c1ffabdf5] "
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n"
        "# Review of #930\nThis also affects issue-931 downstream.\nEND_REVIEW",
    )
    _write_transcript(
        proj / "sess-931.jsonl",
        "[Coordinator review assignment fb5f6ed35cc2] "
        "REVIEW_VERDICT: approve\nREVIEW_BODY:\n# Review of #931\nlgtm\nEND_REVIEW",
    )
    findings = interactive._review_findings_from_transcript(
        931, started_at=0.0, assignment_id="fb5f6ed35cc2", projects_dir=tmp_path
    )
    assert findings is not None
    assert findings.verdict == "approve"
    assert "lgtm" in findings.body


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


def test_names_assignment_requires_exact_id() -> None:
    # #989: a high-entropy assignment id must match verbatim — no partial or
    # cross-assignment match, and an empty id never matches anything.
    names = interactive._transcript_names_assignment
    assert names("[Coordinator review assignment fb5f6ed35cc2] ...", "fb5f6ed35cc2") is True
    assert names("coord report-result --assignment fb5f6ed35cc2 --status done", "fb5f6ed35cc2") is True
    assert names("[Coordinator review assignment 9f1c1ffabdf5] ...", "fb5f6ed35cc2") is False
    assert names("nothing relevant here", "fb5f6ed35cc2") is False
    assert names("fb5f6ed35cc2 appears here", "") is False


def test_transcript_floor_matches_hash_form_in_body(tmp_path: Path) -> None:
    # Lock the real-world failure: a review whose prose uses "#607" must recover.
    proj = tmp_path / "-home-john-src-claude-coordinator"
    proj.mkdir()
    _write_transcript(
        proj / "sess.jsonl",
        "[Coordinator review assignment aaa111] "
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n"
        "## Review: #607 — pull-right submenus\nRight-on-leaf must be a no-op.\n"
        "END_REVIEW",
    )
    findings = interactive._review_findings_from_transcript(
        607, started_at=0.0, assignment_id="aaa111", projects_dir=tmp_path
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
                    {"type": "text", "text": "[Coordinator review assignment aaa111] "
                     "Review the PR for "
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
        362, started_at=0.0, assignment_id="aaa111", projects_dir=tmp_path
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
        "[Coordinator review assignment aaa111] "
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
        607, started_at=0.0, assignment_id="aaa111", ssh_target="precision"
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
            607, started_at=0.0, assignment_id="aaa111", ssh_target="precision"
        )
        is None
    )


def test_remote_transcript_floor_untagged_returns_none(monkeypatch) -> None:
    # A remote review that doesn't name THIS issue is not trusted (same
    # anti-misattribution rule as the local floor).
    import subprocess as _sp

    transcript = _transcript_jsonl(
        "[Coordinator review assignment aaa111] "
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
            607, started_at=0.0, assignment_id="aaa111", ssh_target="precision"
        )
        is None
    )


def test_remote_transcript_floor_wrong_assignment_id_returns_none(monkeypatch) -> None:
    # #989 remote twin: the transcript names THIS issue (cross-referenced by a
    # sibling issue's own review session) but carries a DIFFERENT assignment
    # id — must not be trusted.
    import subprocess as _sp

    transcript = _transcript_jsonl(
        "[Coordinator review assignment 9f1c1ffabdf5] "
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n"
        "# Review of #930\nAlso affects issue-931.\nEND_REVIEW"
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
            931, started_at=0.0, assignment_id="fb5f6ed35cc2", ssh_target="precision"
        )
        is None
    )


def test_transcript_floor_untagged_review_returns_none(tmp_path: Path) -> None:
    # A review that doesn't name THIS issue is not trusted (no guess-the-only-one
    # fallback) — defer to the human-prompt backstop rather than mis-attribute.
    (tmp_path / "a").mkdir()
    _write_transcript(
        tmp_path / "a" / "x.jsonl",
        "[Coordinator review assignment aaa111] "
        "REVIEW_VERDICT: request-changes\nREVIEW_BODY:\n# Review of issue-999\nnope\nEND_REVIEW",
    )
    assert (
        interactive._review_findings_from_transcript(
            370, started_at=0.0, assignment_id="aaa111", projects_dir=tmp_path
        )
        is None
    )


def test_remote_transcript_floor_crowded_out_target_still_found(monkeypatch) -> None:
    """#619: target transcript crowded out of top-6 by concurrent sessions.

    Under normal fleet load several sessions write transcripts simultaneously.
    The old max_candidates=6 cap meant the target review was silently skipped
    when 6+ unrelated transcripts had a newer mtime.  All candidates within the
    cutoff window must now be checked — no early break.
    """
    import subprocess as _sp

    # Build a listing with 8 unrelated (non-matching) transcripts ahead of the
    # target one; all have mtime far in the future so they pass the cutoff.
    unrelated = _transcript_jsonl("STATUS: working on something else entirely")
    target = _transcript_jsonl(
        "[Coordinator review assignment aaa111] "
        "REVIEW_VERDICT: approve\nREVIEW_BODY:\n"
        "# Review: issue-619 — concurrent-load fix\nLGTM.\nEND_REVIEW"
    )
    # Entries: 8 unrelated (paths 0..7) then the target (path 8)
    listing_lines = "\n".join(
        f"9999999999.0\t/h/.claude/projects/p{i}/s.jsonl" for i in range(9)
    )

    def fake_run(cmd, *args, **kwargs):
        remote_cmd = cmd[-1]
        if remote_cmd.startswith("find "):
            return _sp.CompletedProcess(cmd, 0, stdout=listing_lines + "\n", stderr="")
        if remote_cmd.startswith("cat "):
            # Last path (p8) is the target; everything else is unrelated
            if "/p8/" in remote_cmd:
                return _sp.CompletedProcess(cmd, 0, stdout=target, stderr="")
            return _sp.CompletedProcess(cmd, 0, stdout=unrelated, stderr="")
        return _sp.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr("coord.interactive.subprocess.run", fake_run)
    # Pass a large started_at so cutoff is way in the past and all 9 entries pass
    findings = interactive._review_findings_from_transcript(
        619, started_at=0.0, assignment_id="aaa111", ssh_target="dellserver"
    )
    assert findings is not None, (
        "target review crowded beyond position 6 must still be found (#619)"
    )
    assert findings.verdict == "approve"
    assert "LGTM" in findings.body


def test_remote_transcript_floor_retries_on_miss(monkeypatch) -> None:
    """#619: a settle-and-retry covers a transcript-flush blip.

    If the first scan finds nothing (e.g. the JSONL write hasn't been flushed
    yet), the floor sleeps 2 s and retries once.  The second attempt should
    recover the verdict without falling back to the operator prompt.
    """
    import subprocess as _sp

    transcript = _transcript_jsonl(
        "[Coordinator review assignment aaa111] "
        "REVIEW_VERDICT: approve\nREVIEW_BODY:\n"
        "# Review: issue-619 — retry\nAll good.\nEND_REVIEW"
    )
    call_count = {"find": 0}

    def fake_run(cmd, *args, **kwargs):
        remote_cmd = cmd[-1]
        if remote_cmd.startswith("find "):
            call_count["find"] += 1
            if call_count["find"] == 1:
                # First scan: nothing in the window yet
                return _sp.CompletedProcess(cmd, 0, stdout="", stderr="")
            # Second scan: transcript now visible
            return _sp.CompletedProcess(
                cmd, 0,
                stdout="9999999999.0\t/h/.claude/projects/p/s.jsonl\n",
                stderr="",
            )
        if remote_cmd.startswith("cat "):
            return _sp.CompletedProcess(cmd, 0, stdout=transcript, stderr="")
        return _sp.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr("coord.interactive.subprocess.run", fake_run)
    monkeypatch.setattr("coord.interactive.time.sleep", lambda _: None)  # no real delay
    findings = interactive._review_findings_from_transcript(
        619, started_at=0.0, assignment_id="aaa111", ssh_target="dellserver"
    )
    assert findings is not None, "retry must recover the verdict after initial miss"
    assert findings.verdict == "approve"
    assert call_count["find"] == 2, "exactly one retry (two find calls total)"
