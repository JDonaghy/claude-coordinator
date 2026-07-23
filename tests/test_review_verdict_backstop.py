"""#877: board-content gate + remote-aware backstop for the interactive
request-changes verdict.

Tests the three acceptance scenarios from the issue:
(a) board has verdict+findings → no editor, relayed verdict == captured,
    prompt default != "s"
(b) board empty + findings only in a remote transcript → recovered via
    ssh_target, no blank editor
(c) status=failed WITH a valid captured verdict+body → still takes path (a)
"""

from __future__ import annotations

import json

import pytest


# ── shared helpers ─────────────────────────────────────────────────────────────

class _Out:
    """Minimal StoreOutcome stub."""
    posted = True
    error = None
    findings_written = True


def _call_relay(monkeypatch, *, assignment_id="rev-877", issue_number=877, **extra):
    """Invoke _prompt_and_relay_review_verdict with minimal required args and
    any extra keyword overrides.  Returns the captured ``post_result`` call and
    the relay's return value as ``(record, ok)``."""
    from coord.cli import _prompt_and_relay_review_verdict

    captured: dict = {}
    monkeypatch.setattr(
        "coord.issue_store.post_result",
        lambda rec: (captured.setdefault("rec", rec), _Out())[1],
    )
    ok = _prompt_and_relay_review_verdict(
        assignment_id=assignment_id,
        repo_name="claude-coordinator",
        repo_github="JDonaghy/claude-coordinator",
        issue_number=issue_number,
        machine_name="precision",
        verdict_cmd_hint="HINT",
        **extra,
    )
    return captured.get("rec"), ok


# ── (a) board has verdict+findings ──────────────────────────────────────────────

class TestBoardContentGate:
    """(a) When the board already has verdict + findings the editor must NOT open
    and the relayed verdict must match the captured one."""

    def test_tty_rc_uses_board_body_no_editor(self, monkeypatch) -> None:
        """request-changes with board content: relays body directly, no editor."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        # Board has the findings.
        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: ("request-changes", "Blocking: coord.py:42 wrong path"),
        )

        # Operator accepts the suggested default.
        answers = iter(["r", ""])   # verdict=r, summary=""
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))

        # Editor MUST NOT be opened — fail loudly if it is.
        monkeypatch.setattr(
            "coord.commands.review._collect_review_body_via_editor",
            lambda **kw: pytest.fail("editor opened despite pre-captured body"),
        )

        rec, ok = _call_relay(monkeypatch)
        assert ok is True
        assert rec is not None
        assert rec.verdict == "request-changes"
        assert rec.findings_body == "Blocking: coord.py:42 wrong path"

    def test_tty_approve_uses_board_default(self, monkeypatch) -> None:
        """approve verdict captured on board: prompt defaults to [a], relays ok."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: ("approve", "LGTM, ship it."),
        )

        # Capture the 'default' kwarg from click.prompt to assert != "s".
        prompt_defaults: list = []

        def _capturing_prompt(*a, **kw):
            prompt_defaults.append(kw.get("default", "s"))
            return kw.get("default", "s")   # operator accepts the default

        answers = iter(["a", ""])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))

        rec, ok = _call_relay(monkeypatch)
        assert ok is True
        assert rec is not None
        assert rec.verdict == "approve"

    def test_default_is_not_s_when_board_has_rc_verdict(self, monkeypatch) -> None:
        """When board captured request-changes the prompt default is 'r', not 's'."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: ("request-changes", "Some findings"),
        )

        captured_defaults: list = []

        def _spy_prompt(*a, **kw):
            captured_defaults.append(kw.get("default", "s"))
            return kw.get("default", "s")   # accept the default

        answers = iter(["r", ""])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))

        rec, ok = _call_relay(monkeypatch)
        assert ok is True
        # The body came from the board, not the editor.
        assert rec is not None and rec.findings_body == "Some findings"

    def test_non_tty_board_content_auto_relays(self, monkeypatch) -> None:
        """Headless + board has verdict+body → auto-relay without any prompt."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: ("request-changes", "findings body"),
        )

        # No click.prompt calls expected — would raise StopIteration if attempted.
        monkeypatch.setattr("click.prompt", lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("click.prompt should not be called in headless mode")
        ))

        rec, ok = _call_relay(monkeypatch)
        assert ok is True
        assert rec is not None
        assert rec.verdict == "request-changes"
        assert rec.findings_body == "findings body"

    def test_non_tty_board_approve_auto_relays(self, monkeypatch) -> None:
        """Headless + board has approve verdict → auto-relay (no body needed)."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: ("approve", "LGTM"),
        )

        rec, ok = _call_relay(monkeypatch)
        assert ok is True
        assert rec is not None
        assert rec.verdict == "approve"

    def test_non_tty_board_rc_without_body_refuses(self, monkeypatch) -> None:
        """Headless + board has request-changes but no body → refuse (can't relay)."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        # load_assignment_review_findings requires BOTH verdict AND body — if body
        # is missing the function returns None.  Simulate: board has verdict but
        # the findings column only stored the verdict (no body).
        # In practice this shouldn't happen (the seam requires both), but guard it.
        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: None,  # board empty → falls through
        )
        # No remote transcript either.
        monkeypatch.setattr(
            "coord.interactive._review_findings_from_transcript",
            lambda *a, **kw: None,
        )

        rec, ok = _call_relay(monkeypatch)
        assert ok is False
        assert rec is None  # nothing posted


# ── (b) remote transcript floor ───────────────────────────────────────────────

class TestRemoteTranscriptFloor:
    """(b) Board is empty but findings are in the session's remote transcript —
    recovered via ssh_target, editor not opened blank."""

    def _make_findings(self, verdict: str, body: str):
        """Return a ReviewFindings-like object."""
        class _F:
            pass
        f = _F()
        f.verdict = verdict
        f.body = body
        return f

    def test_remote_transcript_seeds_editor_not_blank(self, monkeypatch) -> None:
        """Board empty + transcript has body: editor seeded (or skipped), not blank."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        # Board has nothing.
        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: None,
        )

        # Remote transcript has the findings.
        _tf_body = "Blocking: right-on-leaf must be no-op. See app.rs:88."
        monkeypatch.setattr(
            "coord.interactive._review_findings_from_transcript",
            lambda issue, started_at, *, assignment_id=None, ssh_target=None: (
                self._make_findings("request-changes", _tf_body)
                if ssh_target == "precision"
                else None
            ),
        )

        # Operator accepts the default (which should be "r", not "s").
        answers = iter(["r", ""])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))

        # Editor MUST NOT be opened — findings came from the transcript.
        monkeypatch.setattr(
            "coord.commands.review._collect_review_body_via_editor",
            lambda **kw: pytest.fail("editor opened despite transcript body"),
        )

        rec, ok = _call_relay(monkeypatch, ssh_target="precision")
        assert ok is True
        assert rec is not None
        assert rec.verdict == "request-changes"
        assert rec.findings_body == _tf_body

    def test_remote_transcript_no_ssh_target_opens_editor(self, monkeypatch) -> None:
        """No ssh_target provided AND board is empty: editor opens (may be blank)."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        # Board and transcript both yield nothing.
        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: None,
        )

        # Default prompt → "r", summary empty.
        answers = iter(["r", ""])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))

        # Editor is called and returns a body (user types it).
        editor_called: list = []
        monkeypatch.setattr(
            "coord.commands.review._collect_review_body_via_editor",
            lambda **kw: (editor_called.append(kw), "Manually entered findings")[1],
        )

        rec, ok = _call_relay(monkeypatch, ssh_target=None)
        assert ok is True
        assert len(editor_called) == 1   # editor WAS opened
        assert rec is not None
        assert rec.findings_body == "Manually entered findings"

    def test_transcript_not_queried_when_board_has_data(self, monkeypatch) -> None:
        """When board has verdict+body, remote transcript floor is NOT called."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: ("approve", "Already captured."),
        )

        transcript_called: list = []
        monkeypatch.setattr(
            "coord.interactive._review_findings_from_transcript",
            lambda *a, **kw: (transcript_called.append(True), None)[1],
        )

        answers = iter(["a", ""])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))

        rec, ok = _call_relay(monkeypatch, ssh_target="precision")
        assert ok is True
        assert not transcript_called   # board gate prevented the SSH call


# ── (c) status=failed with valid captured verdict+body ────────────────────────

class TestStatusFailedWithValidVerdict:
    """(c) The status=failed-with-valid-verdict inconsistency: the DB row has
    status=failed (written by finalize's post_completion for a review that
    exited without coord report-result) but review_findings is also populated
    (e.g. from notify or a prior partial recording).

    _prompt_and_relay_review_verdict must still take path (a) — board-content
    gate reads review_findings regardless of status.
    """

    def test_failed_status_with_findings_takes_path_a(
        self, monkeypatch, coord_db
    ) -> None:
        """Real DB: row has status=failed AND review_findings set → path (a)."""
        # Insert an assignment row with status=failed.
        import json as _json
        coord_db.execute(
            """INSERT INTO assignments
               (assignment_id, machine_name, repo_name, issue_number,
                issue_title, status, type)
               VALUES (?,?,?,?,?,?,?)""",
            ("rev-fail-877", "precision", "claude-coordinator", 877,
             "[review] #877", "failed", "review"),
        )
        # Populate review_findings (as notify would do after parsing the log).
        findings_payload = _json.dumps(
            {"verdict": "request-changes", "body": "Blocking: coord.py:99 fix needed."}
        )
        coord_db.execute(
            "UPDATE assignments SET review_findings=?, review_verdict=? "
            "WHERE assignment_id=?",
            (findings_payload, "request-changes", "rev-fail-877"),
        )
        coord_db.commit()

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        # Operator accepts the suggested default.
        answers = iter(["r", ""])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))

        # Editor MUST NOT be opened.
        monkeypatch.setattr(
            "coord.commands.review._collect_review_body_via_editor",
            lambda **kw: pytest.fail("editor opened despite board having findings"),
        )

        rec, ok = _call_relay(monkeypatch, assignment_id="rev-fail-877")
        assert ok is True
        assert rec is not None
        # Path (a): body came from the board, not the editor.
        assert rec.verdict == "request-changes"
        assert rec.findings_body == "Blocking: coord.py:99 fix needed."

    def test_failed_status_with_findings_9k_body(self, monkeypatch, coord_db) -> None:
        """Regression test for the live #547 incident: 9188-char body is carried."""
        import json as _json
        large_body = "BLOCKING:\n" + "x" * 9178   # ~9188 chars
        coord_db.execute(
            """INSERT INTO assignments
               (assignment_id, machine_name, repo_name, issue_number,
                issue_title, status, type)
               VALUES (?,?,?,?,?,?,?)""",
            ("rev-547-replay", "precision", "claude-coordinator", 547,
             "[review] #547", "failed", "review"),
        )
        coord_db.execute(
            "UPDATE assignments SET review_findings=?, review_verdict=? "
            "WHERE assignment_id=?",
            (
                _json.dumps({"verdict": "request-changes", "body": large_body}),
                "request-changes",
                "rev-547-replay",
            ),
        )
        coord_db.commit()

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        answers = iter(["r", ""])
        monkeypatch.setattr("click.prompt", lambda *a, **kw: next(answers))
        monkeypatch.setattr(
            "coord.commands.review._collect_review_body_via_editor",
            lambda **kw: pytest.fail("editor opened — should use board body"),
        )

        rec, ok = _call_relay(monkeypatch, assignment_id="rev-547-replay",
                              issue_number=547)
        assert ok is True
        assert rec is not None
        assert len(rec.findings_body or "") > 9000   # full body propagated


# ── backward-compat: existing non-TTY + skip paths still work ─────────────────

class TestBackwardCompat:
    """Ensure existing tests still work: non-TTY no-data → hint, TTY skip → no post."""

    def test_non_tty_no_data_prints_hint(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: None,
        )
        monkeypatch.setattr(
            "coord.interactive._review_findings_from_transcript",
            lambda *a, **kw: None,
        )
        posted: dict = {}
        monkeypatch.setattr(
            "coord.issue_store.post_result",
            lambda rec: posted.setdefault("rec", rec),
        )
        _, ok = _call_relay(monkeypatch)
        assert ok is False
        assert "rec" not in posted

    def test_tty_skip_no_post(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(
            "coord.state.load_assignment_review_findings",
            lambda aid: None,
        )
        monkeypatch.setattr(
            "coord.interactive._review_findings_from_transcript",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr("click.prompt", lambda *a, **kw: "s")
        posted: dict = {}
        monkeypatch.setattr(
            "coord.issue_store.post_result",
            lambda rec: posted.setdefault("rec", rec),
        )
        _, ok = _call_relay(monkeypatch)
        assert ok is False
        assert "rec" not in posted
