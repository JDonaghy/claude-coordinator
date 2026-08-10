"""Tests for #2048's cheap, independent, per-turn liveness auditor.

Covers the pure logic (context-isolation string transforms, verdict
parsing, debounce, strike-streak bookkeeping) with no subprocess involved,
plus :func:`run_audit` with the subprocess mocked out. The black-box
end-to-end path (through ``coord.notify.detect_liveness_stall``, DB
persistence, GitHub comment posting) is covered separately in
``tests/test_liveness_notify.py``.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from coord.liveness_auditor import (
    BLOCKED,
    CONTINUE,
    DONE,
    AuditOutcome,
    AuditState,
    apply_verdict,
    build_audit_user_message,
    parse_verdict,
    run_audit,
    should_audit,
    strip_self_report_lines,
)


# ── strip_self_report_lines ─────────────────────────────────────────────────


class TestStripSelfReportLines:
    def test_strips_status_line(self) -> None:
        text = "did some work\nSTATUS: making great progress → next step → high\nmore work"
        result = strip_self_report_lines(text)
        assert "STATUS:" not in result
        assert "did some work" in result
        assert "more work" in result

    def test_strips_stuck_line(self) -> None:
        text = "tried X\nSTUCK: nothing works, need guidance\ntried Y"
        result = strip_self_report_lines(text)
        assert "STUCK:" not in result

    def test_case_insensitive(self) -> None:
        text = "status: fine\nstuck: nope"
        assert strip_self_report_lines(text) == ""

    def test_no_self_report_lines_unchanged_content(self) -> None:
        text = "just doing normal work, editing files"
        assert strip_self_report_lines(text) == text

    def test_empty_string(self) -> None:
        assert strip_self_report_lines("") == ""

    def test_does_not_strip_status_mentioned_mid_line(self) -> None:
        """Only a line that STARTS with STATUS:/STUCK: is a self-report —
        prose that merely mentions the word must survive untouched."""
        text = "the build status: green, everything fine"
        assert strip_self_report_lines(text) == text


# ── build_audit_user_message ────────────────────────────────────────────────


class TestBuildAuditUserMessage:
    def test_includes_objective_and_turn(self) -> None:
        msg = build_audit_user_message("Fix the flaky test", "edited test_foo.py")
        assert "Fix the flaky test" in msg
        assert "edited test_foo.py" in msg

    def test_blank_objective_gets_placeholder(self) -> None:
        msg = build_audit_user_message("", "did something")
        assert "no objective provided" in msg

    def test_blank_turn_gets_placeholder(self) -> None:
        msg = build_audit_user_message("objective", "")
        assert "no output on this turn" in msg

    def test_never_contains_more_than_the_two_inputs(self) -> None:
        """Context isolation, made concrete: the rendered message is a pure
        function of (objective, turn_text) — nothing else can leak in."""
        msg1 = build_audit_user_message("obj", "turn")
        msg2 = build_audit_user_message("obj", "turn")
        assert msg1 == msg2

    def test_long_objective_is_excerpted(self) -> None:
        """A real GitHub-issue briefing regularly runs 5,000-15,000+
        tokens — the auditor's cost model budgets an ~300-token excerpt,
        not the whole document (#2048 review). A raw multi-KB briefing
        passed straight through must not blow past that budget."""
        huge_briefing = "x" * 50_000
        msg = build_audit_user_message(huge_briefing, "did some work")
        # The full 50k-char briefing must not appear verbatim.
        assert huge_briefing not in msg
        # But the message is still small — bounded, not merely "smaller".
        assert len(msg) < 5_000
        assert "[truncated]" in msg

    def test_long_turn_text_is_excerpted(self) -> None:
        huge_turn = "y" * 50_000
        msg = build_audit_user_message("objective", huge_turn)
        assert huge_turn not in msg
        assert len(msg) < 5_000
        assert "[truncated]" in msg

    def test_short_objective_and_turn_not_marked_truncated(self) -> None:
        msg = build_audit_user_message("short objective", "short turn")
        assert "[truncated]" not in msg


# ── parse_verdict ────────────────────────────────────────────────────────────


class TestParseVerdict:
    @pytest.mark.parametrize(
        "reply,expected",
        [
            ("continue", CONTINUE),
            ("Continue", CONTINUE),
            ("  continue  ", CONTINUE),
            ("done", DONE),
            ("DONE.", DONE),
            ("blocked", BLOCKED),
            ("blocked\n", BLOCKED),
            ("The verdict is: blocked", BLOCKED),
        ],
    )
    def test_recognises_verdict_words(self, reply: str, expected: str) -> None:
        assert parse_verdict(reply) == expected

    def test_empty_reply_is_none(self) -> None:
        assert parse_verdict("") is None

    def test_unparseable_reply_is_none(self) -> None:
        assert parse_verdict("I'm not sure what to say") is None


# ── should_audit (debounce) ─────────────────────────────────────────────────


class TestShouldAudit:
    def test_never_audited_always_true(self) -> None:
        assert should_audit(last_audit_at=None, now=1000.0, debounce_seconds=60.0) is True

    def test_within_debounce_window_false(self) -> None:
        assert (
            should_audit(last_audit_at=1000.0, now=1030.0, debounce_seconds=60.0)
            is False
        )

    def test_at_exactly_debounce_boundary_true(self) -> None:
        assert (
            should_audit(last_audit_at=1000.0, now=1060.0, debounce_seconds=60.0)
            is True
        )

    def test_past_debounce_window_true(self) -> None:
        assert (
            should_audit(last_audit_at=1000.0, now=2000.0, debounce_seconds=60.0)
            is True
        )


# ── apply_verdict (strike streak) ───────────────────────────────────────────


class TestApplyVerdict:
    def test_blocked_increments_streak(self) -> None:
        state = AuditState()
        state, raised = apply_verdict(state, BLOCKED, now=100.0, strikes=3)
        assert state.consecutive_blocked == 1
        assert raised is False

    def test_continue_resets_streak(self) -> None:
        state = AuditState(consecutive_blocked=2)
        state, raised = apply_verdict(state, CONTINUE, now=100.0, strikes=3)
        assert state.consecutive_blocked == 0
        assert raised is False

    def test_done_resets_streak(self) -> None:
        state = AuditState(consecutive_blocked=2)
        state, raised = apply_verdict(state, DONE, now=100.0, strikes=3)
        assert state.consecutive_blocked == 0

    def test_unparseable_verdict_leaves_streak_untouched(self) -> None:
        """A None verdict (subprocess failure / unparseable reply) must
        never count as evidence in either direction — see #2048's
        acceptance bar on audit reliability."""
        state = AuditState(consecutive_blocked=2, last_verdict="blocked")
        new_state, raised = apply_verdict(state, None, now=100.0, strikes=3)
        assert new_state.consecutive_blocked == 2
        assert new_state.last_verdict == "blocked"  # unchanged, not overwritten with None
        assert raised is False
        # But the debounce clock still advances — a failed audit still
        # "used up" its debounce window, so we don't retry every poll.
        assert new_state.last_audit_at == 100.0

    def test_two_consecutive_blocked_does_not_raise(self) -> None:
        """One bad turn is normal; two in a row still isn't the strike
        threshold at the default of 3."""
        state = AuditState()
        state, raised1 = apply_verdict(state, BLOCKED, now=1.0, strikes=3)
        assert raised1 is False
        state, raised2 = apply_verdict(state, BLOCKED, now=2.0, strikes=3)
        assert raised2 is False
        assert state.consecutive_blocked == 2
        assert state.raised is False

    def test_third_consecutive_blocked_raises_exactly_once(self) -> None:
        """The three-strike boundary: 2 strikes → silent, 3 → raises. This
        is the acceptance-criteria boundary test for #2048."""
        state = AuditState()
        state, r1 = apply_verdict(state, BLOCKED, now=1.0, strikes=3)
        state, r2 = apply_verdict(state, BLOCKED, now=2.0, strikes=3)
        state, r3 = apply_verdict(state, BLOCKED, now=3.0, strikes=3)
        assert (r1, r2, r3) == (False, False, True)
        assert state.consecutive_blocked == 3
        assert state.raised is True

    def test_further_blocked_verdicts_after_raise_do_not_reraise(self) -> None:
        """Edge-triggered: once raised, a streak that stays at/above the
        threshold must not fire the event again on every subsequent poll."""
        state = AuditState()
        for now in (1.0, 2.0, 3.0):
            state, _ = apply_verdict(state, BLOCKED, now=now, strikes=3)
        assert state.raised is True
        state, raised_again = apply_verdict(state, BLOCKED, now=4.0, strikes=3)
        assert raised_again is False
        assert state.raised is True

    def test_streak_reset_then_re_reaching_strikes_can_raise_again(self) -> None:
        """A worker that recovers (continue) and later stalls again for a
        FRESH streak is a new incident — apply_verdict's edge-trigger logic
        keys only on `state.raised`, so this documents that once raised, an
        assignment's ledger entry stays "raised" until the notify layer
        stops auditing it (see coord.notify.detect_liveness_stall, which
        skips any assignment whose state.raised is already True)."""
        state = AuditState()
        for now in (1.0, 2.0, 3.0):
            state, _ = apply_verdict(state, BLOCKED, now=now, strikes=3)
        assert state.raised is True
        # Even a CONTINUE after raising doesn't un-raise it — the ledger
        # entry is a one-shot "this assignment already got a comment", not
        # a live gauge.
        state, _ = apply_verdict(state, CONTINUE, now=4.0, strikes=3)
        assert state.consecutive_blocked == 0
        assert state.raised is True


# ── run_audit (subprocess mocked) ───────────────────────────────────────────


class TestRunAudit:
    def _mock_result(self, *, result_text: str, cost: float = 0.001, returncode: int = 0):
        stdout = json.dumps({"result": result_text, "total_cost_usd": cost})
        return subprocess.CompletedProcess(
            args=["claude"], returncode=returncode, stdout=stdout, stderr="",
        )

    def test_happy_path_parses_verdict_and_cost(self) -> None:
        with patch(
            "subprocess.run",
            return_value=self._mock_result(result_text="blocked", cost=0.00115),
        ) as mock_run:
            outcome = run_audit("objective", "turn text")
        assert outcome.verdict == BLOCKED
        assert outcome.cost_usd == pytest.approx(0.00115)
        assert outcome.error is None
        # No transcript, no --resume — a fresh, independent one-shot call.
        argv = mock_run.call_args.args[0]
        assert "--resume" not in argv
        user_message = mock_run.call_args.kwargs["input"]
        assert "objective" in user_message
        assert "turn text" in user_message

    def test_command_uses_configured_model(self) -> None:
        with patch(
            "subprocess.run", return_value=self._mock_result(result_text="continue"),
        ) as mock_run:
            run_audit("objective", "turn", model="claude-haiku-4-5")
        argv = mock_run.call_args.args[0]
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"

    def test_nonzero_exit_returns_none_verdict_with_error(self) -> None:
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["claude"], returncode=1, stdout="", stderr="boom",
            ),
        ):
            outcome = run_audit("objective", "turn")
        assert outcome.verdict is None
        assert outcome.error is not None

    def test_timeout_returns_none_verdict_not_raise(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30),
        ):
            outcome = run_audit("objective", "turn")
        assert outcome.verdict is None
        assert outcome.error is not None

    def test_missing_binary_returns_none_verdict_not_raise(self) -> None:
        with patch("subprocess.run", side_effect=OSError("no such file")):
            outcome = run_audit("objective", "turn")
        assert outcome.verdict is None
        assert outcome.error is not None

    def test_malformed_json_falls_back_to_raw_stdout(self) -> None:
        with patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["claude"], returncode=0, stdout="blocked", stderr="",
            ),
        ):
            outcome = run_audit("objective", "turn")
        assert outcome.verdict == BLOCKED
        assert outcome.cost_usd is None
