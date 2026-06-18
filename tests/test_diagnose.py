"""Tests for the per-stage doctor (coord/diagnose.py).

The side-effecting steps (session probe, finalize, transcript recovery, merge
reconcile, session kill) are factored into monkeypatchable module helpers so the
orchestration is exercised here without touching git/tmux/the network.
"""

from __future__ import annotations

import time

import pytest

from coord import diagnose
from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="precision.tailnet", repos=["api"])],
    )


def _assign(
    *,
    aid: str,
    typ: str = "work",
    status: str = "running",
    issue: int = 42,
    branch: str | None = "issue-42-foo",
    verdict: str | None = None,
    dispatched_at: float | None = None,
) -> Assignment:
    return Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=issue,
        issue_title="t",
        assignment_id=aid,
        type=typ,
        status=status,
        branch=branch,
        review_verdict=verdict,
        dispatched_at=dispatched_at if dispatched_at is not None else time.time(),
    )


def _stub(monkeypatch, *, session="dead", recover_verdict=None, merge_actions=None):
    """Stub every side-effecting wrapper; return a record of calls."""
    calls: dict[str, list] = {"finalize": [], "kill": [], "recover": [], "reconcile": []}

    monkeypatch.setattr(diagnose, "_session_state", lambda a, c: (
        session(a) if callable(session) else session
    ))
    monkeypatch.setattr(diagnose, "_finalize_dead", lambda a, c: (
        calls["finalize"].append(a.assignment_id) or "advisory"
    ))
    monkeypatch.setattr(diagnose, "_kill_session", lambda a, c: (
        calls["kill"].append(a.assignment_id) or True
    ))
    monkeypatch.setattr(diagnose, "_recover_review_findings", lambda a, c: (
        calls["recover"].append(a.assignment_id) or recover_verdict
    ))
    monkeypatch.setattr(diagnose, "_reconcile_issue_merges", lambda b, c, r, i, *, dry_run: (
        calls["reconcile"].append((r, i)) or list(merge_actions or [])
    ))
    return calls


# ── healthy / no-op ─────────────────────────────────────────────────────────


def test_no_assignment_is_healthy(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead")
    board = Board()
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert res.recovered is True
    assert res.needs_reset is False
    assert any("no review assignment" in f for f in res.findings)


# ── phantom running ──────────────────────────────────────────────────────────


def test_phantom_running_work_is_finalized(monkeypatch, config) -> None:
    calls = _stub(monkeypatch, session="dead")
    a = _assign(aid="w1", typ="work", status="running")
    board = Board(active=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert res.recovered is True
    assert res.needs_reset is False
    assert "w1" in calls["finalize"]
    assert any("phantom" in f for f in res.findings)


def test_phantom_is_finalized_once_not_twice(monkeypatch, config) -> None:
    # The stage step finalizes `latest`; the issue-wide cleanup must NOT
    # re-finalize the same row (it's skipped via handled-ids).
    calls = _stub(monkeypatch, session="dead")
    a = _assign(aid="w1", typ="work", status="running")
    board = Board(active=[a])
    diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert calls["finalize"].count("w1") == 1


# ── review findings recovery (#607 class) ────────────────────────────────────


def test_review_missing_findings_recovered_from_transcript(monkeypatch, config) -> None:
    calls = _stub(monkeypatch, session="live", recover_verdict="request-changes")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(aid="r1", typ="review", status="done", verdict="request-changes")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert "r1" in calls["recover"]
    assert res.recovered is True
    assert res.needs_reset is False
    assert any("recovered review findings" in x for x in res.actions_taken)


def test_review_findings_unrecoverable_needs_reset(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead", recover_verdict=None)  # transcript yields nothing
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(aid="r1", typ="review", status="done", verdict="request-changes")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert res.needs_reset is True
    assert res.recovered is False


def test_review_with_findings_is_healthy(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings",
        lambda aid: ("request-changes", "real findings body"),
    )
    a = _assign(aid="r1", typ="review", status="done", verdict="request-changes")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert res.recovered is True
    assert res.needs_reset is False


# ── stale-but-live → needs reset ─────────────────────────────────────────────


def test_stale_live_work_session_needs_reset(monkeypatch, config) -> None:
    _stub(monkeypatch, session="live")
    old = time.time() - 3 * 24 * 3600  # 3 days ago
    a = _assign(aid="w1", typ="work", status="running", dispatched_at=old)
    board = Board(active=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert res.needs_reset is True


def test_recent_live_work_session_is_left_running(monkeypatch, config) -> None:
    _stub(monkeypatch, session="live")
    a = _assign(aid="w1", typ="work", status="running", dispatched_at=time.time())
    board = Board(active=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert res.needs_reset is False
    assert res.recovered is True


# ── merge reconcile ──────────────────────────────────────────────────────────


def test_merge_stage_reconciles(monkeypatch, config) -> None:
    calls = _stub(monkeypatch, session="dead", merge_actions=["mark merged w1 (#42)"])
    a = _assign(aid="w1", typ="work", status="done")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "merge")
    assert ("api", 42) in calls["reconcile"]
    assert any("mark merged" in x for x in res.actions_taken)
    assert res.recovered is True


# ── reset is non-destructive (keeps the branch) ──────────────────────────────


def test_reset_keeps_branch_and_stops_live_session(monkeypatch, config) -> None:
    calls = _stub(monkeypatch, session="live")
    a = _assign(aid="w1", typ="work", status="running", branch="issue-42-foo")
    board = Board(active=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work", reset=True)
    assert res.reset_performed is True
    assert res.branch_preserved is True
    assert res.recovered is True
    # Stopped the live session + finalized, but the branch field is untouched.
    assert "w1" in calls["kill"]
    assert "w1" in calls["finalize"]
    assert a.branch == "issue-42-foo"  # branch preserved
    assert any("branch preserved" in x for x in res.actions_taken)


def test_reset_dry_run_does_nothing(monkeypatch, config) -> None:
    calls = _stub(monkeypatch, session="live")
    a = _assign(aid="w1", typ="work", status="running")
    board = Board(active=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work", reset=True, dry_run=True)
    assert calls["kill"] == [] and calls["finalize"] == []
    assert res.reset_performed is False


# ── issue-wide cleanup ───────────────────────────────────────────────────────


def test_cleanup_finalizes_other_phantom_rows(monkeypatch, config) -> None:
    # Diagnosing the review stage should still clean up a separate phantom WORK
    # row for the same issue (the "db world cleaned up" requirement).
    calls = _stub(monkeypatch, session="dead")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings",
        lambda aid: ("approve", "ok"),
    )
    review = _assign(aid="r1", typ="review", status="done", verdict="approve")
    phantom_work = _assign(aid="w1", typ="work", status="running")
    board = Board(active=[phantom_work], completed=[review])
    diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert "w1" in calls["finalize"]  # the OTHER phantom row got cleaned up


# ── result trailer ───────────────────────────────────────────────────────────


def test_summary_line_format() -> None:
    res = diagnose.DiagnoseResult(repo_name="api", issue_number=42, stage="review")
    res.recovered = True
    line = res.summary_line()
    assert line.startswith("DIAGNOSE_RESULT:")
    assert "stage=review" in line
    assert "recovered=true" in line
    assert "needs_reset=false" in line


def test_stage_assignments_newest_first(config) -> None:
    old = _assign(aid="r-old", typ="review", dispatched_at=100.0)
    new = _assign(aid="r-new", typ="review", dispatched_at=200.0)
    board = Board(completed=[old, new])
    rows = diagnose.stage_assignments(board, "api", 42, "review")
    assert [a.assignment_id for a in rows] == ["r-new", "r-old"]
