"""Tests for coord/dead_end.py — the driver's dead-end predicate (#2019).

The bug this closes is not "the driver was slow". It is that ``coord drive``
had no way to say **"finished in a state I cannot act on"**, so it said
``no state change in 140.558m`` instead, forever, while holding a tmux
session, a drive-queue slot and a per-repo capacity lane (#1972).

The two properties under test, in priority order:

1. **It never fires on a healthy row.** A false positive escalates a
   legitimately quiet long-running stage, which is strictly worse than the
   bug. Hence the first block below, and hence `active_count > 0` being a
   hard precondition rather than one signal among several.
2. **It fires on the shapes the board makes PROVABLE**, with a reason naming
   the specific dead end and a recovery command — not on "looks quiet".
"""

from __future__ import annotations

import pytest

from coord.dead_end import DeadEnd, detect_dead_end
from coord.drive_state import IssueState
from coord.smoke import TEST_STATE_BLOCKED


REPO = "claude-coordinator"
ISSUE = 2019


def state(**kw) -> IssueState:
    base = dict(repo=REPO, issue=ISSUE, repo_github="john/claude-coordinator")
    base.update(kw)
    return IssueState(**base)


def healthy_review_dead_end_fields() -> dict:
    """The exact #1956 board shape: work done, test passed, a review row that
    reached ``done`` carrying no verdict, nothing active."""
    return dict(
        work_aid="w1956",
        work_status="done",
        work_branch="issue-1956",
        work_test_state="passed",
        review_aid="c9b489b2333e",
        review_status="done",
        review_verdict="",
        active_count=0,
    )


# ── property 1: never fire on a row that can still move ──────────────────────


@pytest.mark.parametrize("active", [1, 2, 7])
def test_anything_active_is_never_a_dead_end(active: int) -> None:
    """#2019 acceptance: "a genuinely long-running work stage (active=1) does
    NOT escalate, however long it runs."

    Elapsed time is not an input to the predicate at all, so "however long"
    is enforced structurally: this is the same call the driver makes on poll
    1 and on poll 10,000, and it returns ``None`` both times. The state here
    is otherwise the WORST case — the full #1956 dead-end shape — so the
    guard is doing all the work.
    """
    fields = healthy_review_dead_end_fields()
    fields["active_count"] = active
    assert detect_dead_end(state(**fields)) is None


def test_long_running_work_stage_is_not_a_dead_end() -> None:
    """The ordinary "quiet for 40 minutes" case: one work row in flight,
    nothing else on the board yet."""
    assert (
        detect_dead_end(
            state(
                work_aid="w1",
                work_status="running",
                work_branch="issue-2019",
                active_count=1,
            )
        )
        is None
    )


def test_review_in_flight_is_not_a_dead_end() -> None:
    """A dispatched review that has not reported yet is the NORMAL shape a
    verdict-less review row takes. Only a TERMINAL review status qualifies."""
    fields = healthy_review_dead_end_fields()
    fields.update(review_status="running", active_count=1)
    assert detect_dead_end(state(**fields)) is None


def test_review_status_failed_is_left_to_the_bounded_retry_arm() -> None:
    """#1584 owns ``review_status == "failed"`` with a bounded ``coord review``
    re-dispatch that genuinely can succeed. Claiming it as a dead end would
    steal a live move — the exact over-reach #2019's blast-radius note warns
    about."""
    fields = healthy_review_dead_end_fields()
    fields["review_status"] = "failed"
    fields["review_failure_reason"] = "terminal API error"
    assert detect_dead_end(state(**fields)) is None


def test_approved_review_is_not_a_dead_end() -> None:
    fields = healthy_review_dead_end_fields()
    fields["review_verdict"] = "approve"
    assert detect_dead_end(state(**fields)) is None


def test_request_changes_is_not_a_dead_end() -> None:
    """A request-changes verdict is the most ACTIONABLE state there is — it
    dispatches `coord fix` (#1692)."""
    fields = healthy_review_dead_end_fields()
    fields["review_verdict"] = "request-changes"
    assert detect_dead_end(state(**fields)) is None


def test_failed_test_outranks_a_verdictless_review_row() -> None:
    """A failed test still has a bounded fix loop to spend. Even with a
    verdict-less terminal review row alongside it, the row is not dead."""
    fields = healthy_review_dead_end_fields()
    fields["work_test_state"] = "failed"
    assert detect_dead_end(state(**fields)) is None


def test_no_review_row_at_all_is_not_a_dead_end() -> None:
    """``review_aid == ""`` means no review was ever dispatched — which is
    either "the daemon has not got to it yet" or #2024's missing-Test-gate
    shape. Neither is provable from board state, and both are deliberately
    out of scope (see the module docstring)."""
    assert (
        detect_dead_end(
            state(
                work_aid="w1",
                work_status="done",
                work_branch="issue-2019",
                work_test_state="",
                active_count=0,
            )
        )
        is None
    )


def test_empty_board_is_not_a_dead_end() -> None:
    """Nothing dispatched yet — the driver's job is to dispatch work, not to
    escalate."""
    assert detect_dead_end(state(active_count=0)) is None


def test_a_done_smoke_row_with_a_lagging_verdict_is_not_a_dead_end() -> None:
    """#1605 established that a fresh `done` smoke has an expected, bounded
    propagation lag before its verdict lands. That is not this bug, and
    claiming it would escalate a healthy pipeline once per issue."""
    assert (
        detect_dead_end(
            state(
                work_aid="w1",
                work_status="done",
                work_branch="issue-2019",
                work_test_state="running",
                smoke_aid="s1",
                smoke_status="done",
                active_count=0,
            )
        )
        is None
    )


# ── property 2: fire, with a specific reason, on the provable shapes ─────────


def test_review_done_with_no_verdict_is_a_dead_end() -> None:
    """#2019 acceptance: "a board state of work=done test=passed
    review=done/verdict=None drives to escalation, not to a `no state change`
    loop."

    This is the claude-coordinator#1956 incident verbatim.
    """
    found = detect_dead_end(state(**healthy_review_dead_end_fields()))
    assert isinstance(found, DeadEnd)
    assert found.kind == "review_terminal_no_verdict"
    assert found.stage == "review"
    assert found.assignment_id == "c9b489b2333e"


def test_the_reason_distinguishes_end_review_from_a_crash_and_drops_812() -> None:
    """#2019 acceptance: "the reason text distinguishes END_REVIEW-without-
    verdict from a crashed session, and does not cite closed issue #812 for a
    headless review."

    The operator-facing message that shipped with the incident said the
    session "likely failed to start or exited before recording one (#812)".
    The session had in fact run 392s, produced a complete 6.5KB review and
    exited 0 — and #812 is closed, and was about INTERACTIVE reviews. So the
    two assertions here are a floor, not decoration: name the real class,
    and never name the closed one.
    """
    found = detect_dead_end(state(**healthy_review_dead_end_fields()))
    assert found is not None
    assert "#812" not in found.reason
    assert "END_REVIEW" in found.reason
    # ...and says WHY this is not a crash, in terms of the board field that
    # actually distinguishes them.
    assert "failed" in found.reason


def test_the_recovery_is_the_documented_report_result_command() -> None:
    """#2019 ask 3: `no state change in 140.558m` is not actionable; the
    recovery command already documented in docs/OPERATING_GOTCHAS.md is.
    Including `--verdict-source recovered`, which #1956's second half made
    non-optional — an unattributed relayed verdict is indistinguishable from
    an earned one at every surface downstream.
    """
    found = detect_dead_end(state(**healthy_review_dead_end_fields()))
    assert found is not None
    assert found.recovery.startswith(
        "coord report-result --assignment c9b489b2333e"
    )
    assert "--verdict-source recovered" in found.recovery
    assert "--body-file" in found.recovery


def test_a_cancelled_review_is_a_distinct_dead_end_with_a_redispatch() -> None:
    """A cancelled review is terminal too, but it is NOT the #1956 class —
    nothing was concluded, so there is no transcript to rescue a verdict
    from. Re-dispatch is the right remedy, and the kind is distinct so the
    two never share a reason string."""
    fields = healthy_review_dead_end_fields()
    fields["review_status"] = "cancelled"
    found = detect_dead_end(state(**fields))
    assert found is not None
    assert found.kind == "review_cancelled_no_verdict"
    assert found.recovery == "coord review w1956"
    assert "#1956" not in found.reason


def test_a_blocked_test_stage_is_a_dead_end() -> None:
    """#1672 stamps ``test_state="blocked"`` when no capability-matched
    machine could run the suite, and then deliberately never re-probes. A
    driver polling for that verdict polls forever — the one variant of
    vimcode#635's "the Test stage never arrives" that the board makes
    provable."""
    found = detect_dead_end(
        state(
            work_aid="w1",
            work_status="done",
            work_branch="issue-2019",
            work_test_state=TEST_STATE_BLOCKED,
            work_test_reason="no machine advertises capability 'gtk'",
            active_count=0,
        )
    )
    assert found is not None
    assert found.kind == "test_stage_blocked"
    assert found.stage == "test"
    assert "no machine advertises capability 'gtk'" in found.reason
    assert found.recovery == (
        f"coord diagnose {REPO} {ISSUE} --stage test --reset"
    )


def test_gates_carry_the_readings_that_proved_the_dead_end() -> None:
    """The escalation record has to answer "how did you conclude that?"
    without the tmux pane, which is gone. ``active=0`` in particular is the
    tell the incident log printed on every one of its 140 minutes of lines
    and nothing ever read."""
    found = detect_dead_end(state(**healthy_review_dead_end_fields()))
    assert found is not None
    gates = dict(found.gates)
    assert gates["active"] == "0"
    assert gates["review_status"] == "done"
    assert gates["review_verdict"] == "(none)"
    assert gates["test_state"] == "passed"
