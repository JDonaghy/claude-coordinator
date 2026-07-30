"""#1534: the coordinator must REFUSE an agent-reported ``done`` that also
carries a usage-limit-kill reason.

A worker killed by the Claude session usage limit was recorded on the board as
``status=done`` — no exit code, no failure reason, no distinguishing mark of
any kind. Because ``done`` is the *good* status, everything downstream then
behaved as if the slice existed: a metered review was auto-dispatched against
an empty branch, ``coord drive``'s acceptance gate waited forever for a slice
that could never land, and the Pipeline showed a completed test-author.

``AgentServer._reap`` refuses this at the source as of #1534, but that fix only
reaches the fleet after a PyPI release + ``coord agent update``. These tests
cover the coordinator-side backstop, which works against *any* agent build.
"""

from __future__ import annotations

from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import effective_agent_status, reconcile_completed_assignments

_KILL = "usage limit — resets 8:30pm (America/Chicago)"


def _config() -> Config:
    return Config(
        repos=[Repo(name="cc", github="acme/cc")],
        machines=[Machine(name="precision", host="precision", repos=["cc"])],
    )


def _running(aid: str = "b2d6b331616e", *, atype: str = "test-author") -> Assignment:
    return Assignment(
        machine_name="precision", repo_name="cc",
        issue_number=1124, issue_title="ms-38 slice",
        status="running", assignment_id=aid, type=atype,
        branch="test-author-ms-38-slice-1124",
    )


def _board(*assignments: Assignment) -> Board:
    return Board(
        repos=[Repo(name="cc", github="acme/cc")], machines=[],
        active=list(assignments),
    )


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(
        self, *, assignment_id, terminal_status, branch, review_state,
        failure_reason=None, exit_code=None,
    ) -> None:
        self.calls.append(
            {
                "assignment_id": assignment_id,
                "terminal_status": terminal_status,
                "branch": branch,
                "review_state": review_state,
                "failure_reason": failure_reason,
            }
        )


# ── effective_agent_status ──────────────────────────────────────────────────


def test_done_with_usage_limit_reason_is_refused() -> None:
    assert effective_agent_status(
        {"status": "done", "usage_limit_reason": _KILL}
    ) == "failed"


def test_plain_done_is_untouched() -> None:
    assert effective_agent_status({"status": "done"}) == "done"
    assert effective_agent_status(
        {"status": "done", "usage_limit_reason": None}
    ) == "done"
    assert effective_agent_status(
        {"status": "done", "usage_limit_reason": ""}
    ) == "done"


def test_non_done_statuses_pass_through_unchanged() -> None:
    """Only the ``done`` contradiction is rewritten — an advisory or failed
    row keeps its own status so the existing #448/#1461 handling still fires."""
    for raw in ("advisory", "failed", "cancelled", "running"):
        assert effective_agent_status(
            {"status": raw, "usage_limit_reason": _KILL}
        ) == raw


def test_status_is_case_normalised_and_missing_is_empty() -> None:
    assert effective_agent_status({"status": "DONE"}) == "done"
    assert effective_agent_status({"status": "DONE", "usage_limit_reason": _KILL}) == "failed"
    assert effective_agent_status({}) == ""


# ── the daemon's passive tick (the primary production path) ─────────────────


def test_daemon_tick_records_failed_not_done_for_a_usage_limit_kill() -> None:
    """The exact `b2d6b331616e` shape: the agent's own reap landed on ``done``
    but flagged the kill. The tick must persist ``failed`` + the reset time."""
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running()),
        agent_status_fn=lambda host: {"completed": [
            {"id": "b2d6b331616e", "status": "done", "usage_limit_reason": _KILL},
        ]},
        update_state_fn=rec,
        capture_plan=False,
    )
    assert out[0]["to_status"] == "failed"
    assert len(rec.calls) == 1
    assert rec.calls[0]["terminal_status"] == "failed"
    # "Surface the reset time" — the operator needs "budget exhausted, resets
    # 20:30", which is actionable, not "done".
    assert rec.calls[0]["failure_reason"] == _KILL
    assert "8:30pm" in rec.calls[0]["failure_reason"]


def test_reconcile_refuses_the_done_and_dispatches_no_review() -> None:
    """The `coord status --reconcile` / `coord resume` path, end to end.

    The full cascade the issue describes has to be broken here: the row must
    not land on `done`, and — the expensive half — no review may be
    auto-dispatched for it. In the incident this dispatched a metered review
    (`fad7c8b8bbd8`) against an empty branch whose verdict then came back null.
    """
    from unittest.mock import patch

    from coord.reconcile import reconcile

    board = _board(_running())
    fake_status = {
        "active": [],
        "completed": [{
            "id": "b2d6b331616e", "status": "done", "finished_at": 1.0,
            "branch": "test-author-ms-38-slice-1124",
            "usage_limit_reason": _KILL,
        }],
    }
    dispatched: list[str] = []
    stamped: list[tuple[str, str]] = []

    def _fake_dispatch_review(completed, board, config, **kwargs):
        dispatched.append(completed.assignment_id)
        return None

    with patch("coord.reconcile._query_agent", return_value=fake_status), \
         patch("coord.review.dispatch_review", _fake_dispatch_review), \
         patch("coord.state.set_assignment_failure_reason",
               lambda aid, reason: stamped.append((aid, reason))):
        reconcile(board, _config())

    row = board.completed[0]
    assert row.status == "failed", (
        f"a usage-limit kill must not be recorded `done` — got {row.status!r}"
    )
    assert dispatched == [], (
        "no review may be auto-dispatched for a usage-limit-killed assignment"
    )
    # The reset time reaches the persisted failure_reason, so the operator
    # sees "budget exhausted, resets 20:30" rather than "done".
    assert stamped == [("b2d6b331616e", _KILL)]


def test_daemon_tick_still_records_a_genuine_done() -> None:
    """The guard must not touch an ordinary successful completion."""
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running()),
        agent_status_fn=lambda host: {"completed": [
            {"id": "b2d6b331616e", "status": "done"},
        ]},
        update_state_fn=rec,
        capture_plan=False,
    )
    assert out[0]["to_status"] == "done"
    assert rec.calls[0]["terminal_status"] == "done"
    assert rec.calls[0]["failure_reason"] is None
