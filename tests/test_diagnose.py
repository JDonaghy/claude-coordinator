"""Tests for the per-stage doctor (coord/diagnose.py).

The side-effecting steps (session probe, finalize, transcript recovery, merge
reconcile, session kill) are factored into monkeypatchable module helpers so the
orchestration is exercised here without touching git/tmux/the network.
"""

from __future__ import annotations

import time
from pathlib import Path

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
    review_state: str | None = None,
    dispatched_at: float | None = None,
    failure_reason: str | None = None,
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
        review_state=review_state,
        dispatched_at=dispatched_at if dispatched_at is not None else time.time(),
        failure_reason=failure_reason,
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


# ── #812: review done but no verdict (failed-to-start / abandoned) ───────────


def test_done_review_without_verdict_offers_reset(monkeypatch, config) -> None:
    """#812: a review row that is status=done but has no verdict is permanently
    stuck (nothing is running, but TUI showed Active).  Diagnose must detect it
    and set needs_reset so the operator can re-dispatch a fresh review."""
    calls = _stub(monkeypatch, session="dead", recover_verdict=None)
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(aid="r812", typ="review", status="done", verdict=None)
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert res.needs_reset is True
    assert res.recovered is False
    assert any("#812" in f or "no verdict" in f or "verdict" in f for f in res.findings), (
        f"expected verdict-related finding, got: {res.findings}"
    )
    # Tried transcript recovery before giving up.
    assert "r812" in calls["recover"]


def test_done_review_without_verdict_recovered_from_transcript(monkeypatch, config) -> None:
    """#812: if the transcript contains the verdict (race between finalize and
    the transcript write), recover it and mark stage as recovered — no reset."""
    calls = _stub(monkeypatch, session="dead", recover_verdict="approve")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(aid="r812b", typ="review", status="done", verdict=None)
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert res.recovered is True
    assert res.needs_reset is False
    assert "r812b" in calls["recover"]
    assert any("recovered" in x for x in res.actions_taken)


def test_done_review_without_verdict_dry_run_does_not_write(monkeypatch, config) -> None:
    """#812: dry-run must not write anything — should report needs_reset only."""
    calls = _stub(monkeypatch, session="dead", recover_verdict=None)
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(aid="r812c", typ="review", status="done", verdict=None)
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(
        board, config, "api", 42, "review", dry_run=True
    )
    # dry_run: no actual writes; transcript recovery is skipped
    assert "r812c" not in calls["recover"]
    assert res.needs_reset is True


# ── #1180: review stage must see wedged test-author/mock-author rows ────────


def test_stage_assignment_types_includes_test_author_and_mock_author() -> None:
    """#1180(b): before this, `coord diagnose --stage review` only ever
    looked at type='review' rows, so a wedged test-author/mock-author row
    (review_state='done' via a work_is_terminal false positive, but no
    type='review' assignment ever dispatched) was invisible — the tool would
    report on whatever unrelated type='review' row happened to share the
    tracking issue number instead of flagging the real wedge."""
    assert "test-author" in diagnose.STAGE_ASSIGNMENT_TYPES["review"]
    assert "mock-author" in diagnose.STAGE_ASSIGNMENT_TYPES["review"]
    assert "review" in diagnose.STAGE_ASSIGNMENT_TYPES["review"]


def test_review_stage_flags_wedged_test_author_row(monkeypatch, config) -> None:
    """The #1180 repro: a test-author row false-positived work_is_terminal
    pre-#1150 (tracking-issue aliasing) and got stamped review_state='done'
    with no verdict, and no type='review' assignment ever ran for the issue.
    `coord diagnose <repo> <tracking-issue> --stage review` must find this
    row and flag it — not silently report "healthy" or "no review
    assignment"."""
    calls = _stub(monkeypatch, session="dead", recover_verdict=None)
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(
        aid="ta-wedged", typ="test-author", status="done", issue=1117,
        branch="test-author-ms-37-slice-1115", verdict=None,
        review_state="done",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 1117, "review")
    assert res.needs_reset is True
    assert res.recovered is False
    assert "ta-wedged" in calls["recover"]
    assert any("ta-wedged" in f for f in res.findings)


def test_review_stage_prefers_newer_real_review_over_wedged_test_author(
    monkeypatch, config,
) -> None:
    """When a genuine, more-recently-dispatched type='review' row also
    exists for the tracking issue, the doctor's "latest wins" heuristic picks
    that row — matching its pre-existing behavior for ordinary work/plan
    stages."""
    calls = _stub(monkeypatch, session="dead")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings",
        lambda aid: ("approve", "looks good"),
    )
    wedged = _assign(
        aid="ta-wedged", typ="test-author", status="done", issue=1117,
        branch="test-author-ms-37-slice-1115", verdict=None,
        review_state="done", dispatched_at=100.0,
    )
    real_review = _assign(
        aid="rev-real", typ="review", status="done", issue=1117,
        branch="issue-1117-other-slice", verdict="approve",
        dispatched_at=200.0,
    )
    board = Board(completed=[wedged, real_review])
    res = diagnose.diagnose_stage(board, config, "api", 1117, "review")
    assert any("rev-real" in f for f in res.findings)
    assert res.recovered is True
    assert calls["recover"] == []  # healthy path — no transcript recovery needed


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


def test_reset_review_wipes_rows_state_and_context(monkeypatch, config) -> None:
    # #607: resetting a COMPLETED review must delete the review rows (→ grey),
    # reset the work's review_state (→ re-reviewable), AND purge the #603 review
    # notes ("completely cleared") — not no-op because the session is dead.
    _stub(monkeypatch, session="dead")
    calls: dict = {}
    monkeypatch.setattr(
        "coord.state.delete_assignments_for_issue",
        lambda repo, issue, *, types, review_of_assignment_id=None: calls.setdefault(
            "delete", (repo, issue, types, review_of_assignment_id)
        )
        or 2,
    )
    monkeypatch.setattr(
        "coord.state.reset_work_review_state",
        lambda repo, issue, *, assignment_id=None: calls.setdefault(
            "reset_state", (repo, issue, assignment_id)
        )
        or 1,
    )
    monkeypatch.setattr(
        "coord.state.clear_issue_context_by_source",
        lambda repo, issue, source: calls.setdefault("purge", (repo, issue, source)) or 3,
    )
    a = _assign(aid="r1", typ="review", status="done", verdict="request-changes")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review", reset=True)
    assert res.reset_performed and res.recovered and res.branch_preserved
    # #1180: assignment_id (the stage's latest row) is threaded through to
    # both calls so a multi-slice tracking issue only touches the targeted
    # slice's review data.
    assert calls["delete"] == ("api", 42, ("review",), "r1")
    assert calls["reset_state"] == ("api", 42, "r1")
    assert calls["purge"] == ("api", 42, "review")


def test_reset_review_dry_run_does_not_wipe(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead")

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("dry-run review reset must not write")

    monkeypatch.setattr("coord.state.delete_assignments_for_issue", _boom)
    monkeypatch.setattr("coord.state.reset_work_review_state", _boom)
    monkeypatch.setattr("coord.state.clear_issue_context_by_source", _boom)
    a = _assign(aid="r1", typ="review", status="done", verdict="request-changes")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(
        board, config, "api", 42, "review", reset=True, dry_run=True
    )
    assert res.reset_performed is False


def test_reset_test_clears_test_state(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead")
    calls: dict = {}
    monkeypatch.setattr(
        "coord.state.reset_work_test_state",
        lambda repo, issue: calls.setdefault("test", (repo, issue)) or 1,
    )
    a = _assign(aid="w1", typ="work", status="done")  # test verdict rides the work row
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "test", reset=True)
    assert res.reset_performed is True
    assert calls["test"] == ("api", 42)


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


# ── #1083: current_stage / diagnose_stage on assignment types the doctor
# doesn't understand (test-author, mock-author, smoke, ...) ────────────────


def test_current_stage_returns_unrecognized_type_verbatim(config) -> None:
    """Before #1083, `current_stage` silently coerced any type outside
    plan/work/test/review/merge to "work" — so `coord diagnose` on a
    test-author assignment would resolve to the "work" stage and recover/
    report on a completely unrelated work row for the same issue instead of
    flagging the real (ignored) test-author assignment."""
    a = _assign(aid="ta1", typ="test-author", status="done", issue=1041)
    board = Board(completed=[a])
    assert diagnose.current_stage(board, "api", 1041) == "test-author"


def test_current_stage_still_defaults_to_work_with_no_assignments(config) -> None:
    board = Board()
    assert diagnose.current_stage(board, "api", 999) == "work"


def test_diagnose_stage_reports_no_diagnosis_for_unrecognized_type_with_row(
    monkeypatch, config,
) -> None:
    """`diagnose_stage` on an unrecognized type must explicitly say so —
    never silently fall through to `_recover_work_like` (untested for these
    types) or claim a healthy/recovered outcome it didn't actually check."""
    _stub(monkeypatch, session="dead")
    a = _assign(
        aid="ta1", typ="test-author", status="done", issue=1041,
        branch="issue-1041-test-author-ms-33-acceptance-suite",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 1041, "test-author")
    assert res.recovered is False
    assert res.needs_reset is False
    assert any(
        "no diagnosis available for assignment type 'test-author'" in f
        for f in res.findings
    )
    # The real assignment must be named, not silently ignored.
    assert any("ta1" in f for f in res.findings)


def test_diagnose_stage_reports_no_diagnosis_for_unrecognized_type_no_row(
    monkeypatch, config,
) -> None:
    _stub(monkeypatch, session="dead")
    board = Board()
    res = diagnose.diagnose_stage(board, config, "api", 1041, "test-author")
    assert res.recovered is False
    assert res.needs_reset is False
    assert any(
        "no diagnosis available for assignment type 'test-author'" in f
        for f in res.findings
    )


# ── #618: active_assignment_ids_for_repo ────────────────────────────────────


def test_active_assignment_ids_for_repo_returns_running(config) -> None:
    running = _assign(aid="w1", status="running")
    done = _assign(aid="w2", status="done")
    board = Board(active=[running], completed=[done])
    ids = diagnose._active_assignment_ids_for_repo(board, "api")
    assert ids == {"w1"}


def test_active_assignment_ids_for_repo_excludes_other_repos(config) -> None:
    a = _assign(aid="w1", status="running")
    board = Board(active=[a])
    ids = diagnose._active_assignment_ids_for_repo(board, "other-repo")
    assert ids == set()


def test_active_assignment_ids_for_repo_skips_none_ids(config) -> None:
    """Assignments without an assignment_id must be excluded."""
    a = Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=42,
        issue_title="t",
        assignment_id=None,  # type: ignore[arg-type]
        type="work",
        status="running",
    )
    board = Board(active=[a])
    ids = diagnose._active_assignment_ids_for_repo(board, "api")
    assert ids == set()


# ── #618: _find_orphaned_worktrees ──────────────────────────────────────────


def _make_porcelain_output(entries: list[dict]) -> str:
    """Build a fake ``git worktree list --porcelain`` output."""
    blocks = []
    for e in entries:
        lines = [f"worktree {e['path']}"]
        if "branch" in e:
            lines.append(f"branch refs/heads/{e['branch']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n\n"


def test_find_orphaned_worktrees_returns_orphan(tmp_path, monkeypatch) -> None:
    """A worktree under worktrees_dir with no active assignment is an orphan."""
    import subprocess

    worktrees_dir = tmp_path / "worktrees"
    orphan_path = worktrees_dir / "dead-aid" / "repo"
    orphan_path.mkdir(parents=True)

    porcelain = _make_porcelain_output([
        {"path": str(orphan_path), "branch": "issue-99-foo"},
    ])

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    result = diagnose._find_orphaned_worktrees(
        tmp_path / "repo",
        "issue-99-foo",
        active_assignment_ids=set(),
        worktrees_dir=worktrees_dir,
    )
    assert result == [orphan_path]


def test_find_orphaned_worktrees_skips_active(tmp_path, monkeypatch) -> None:
    """Active assignments are not reported as orphans."""
    import subprocess

    worktrees_dir = tmp_path / "worktrees"
    wt_path = worktrees_dir / "live-aid" / "repo"
    wt_path.mkdir(parents=True)

    porcelain = _make_porcelain_output([
        {"path": str(wt_path), "branch": "issue-99-foo"},
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    result = diagnose._find_orphaned_worktrees(
        tmp_path / "repo",
        "issue-99-foo",
        active_assignment_ids={"live-aid"},
        worktrees_dir=worktrees_dir,
    )
    assert result == []


def test_find_orphaned_worktrees_branch_none_matches_all(tmp_path, monkeypatch) -> None:
    """branch=None acts as a wildcard — both worktrees are found regardless of branch."""
    import subprocess

    worktrees_dir = tmp_path / "worktrees"
    wt1 = worktrees_dir / "aid-a" / "r"
    wt2 = worktrees_dir / "aid-b" / "r"
    wt1.mkdir(parents=True)
    wt2.mkdir(parents=True)

    porcelain = _make_porcelain_output([
        {"path": str(wt1), "branch": "issue-1-foo"},
        {"path": str(wt2), "branch": "issue-2-bar"},
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    result = diagnose._find_orphaned_worktrees(
        tmp_path / "repo",
        None,
        active_assignment_ids=set(),
        worktrees_dir=worktrees_dir,
    )
    assert set(result) == {wt1, wt2}


def test_find_orphaned_worktrees_filters_non_coord_paths(tmp_path, monkeypatch) -> None:
    """Worktrees outside ~/.coord/worktrees/ are ignored (not coordinator-managed)."""
    import subprocess

    worktrees_dir = tmp_path / "worktrees"
    outside = tmp_path / "other" / "checkout"
    outside.mkdir(parents=True)

    porcelain = _make_porcelain_output([
        {"path": str(outside), "branch": "issue-99-foo"},
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    result = diagnose._find_orphaned_worktrees(
        tmp_path / "repo",
        "issue-99-foo",
        active_assignment_ids=set(),
        worktrees_dir=worktrees_dir,
    )
    assert result == []


def test_find_orphaned_worktrees_git_failure_returns_empty(tmp_path, monkeypatch) -> None:
    """A non-zero git exit code returns an empty list gracefully."""
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 1, "stdout": ""}
    )())

    result = diagnose._find_orphaned_worktrees(
        tmp_path / "repo",
        "issue-99-foo",
        active_assignment_ids=set(),
    )
    assert result == []


# ── #618: _prune_orphaned_worktrees ─────────────────────────────────────────


def test_prune_orphaned_worktrees_removes_clean(tmp_path, monkeypatch) -> None:
    """Clean worktrees (no uncommitted changes) are removed."""
    import subprocess

    removed_paths: list = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return type("R", (), {"returncode": 0, "stdout": ""})()
        if cmd[:3] == ["git", "worktree", "remove"]:
            removed_paths.append(cmd[3])
            return type("R", (), {"returncode": 0})()
        return type("R", (), {"returncode": 0})()  # prune

    monkeypatch.setattr(subprocess, "run", fake_run)
    wt = tmp_path / "wt"
    wt.mkdir()
    removed, skipped = diagnose._prune_orphaned_worktrees(tmp_path, [wt])
    assert removed == [wt]
    assert skipped == []


def test_prune_orphaned_worktrees_skips_dirty(tmp_path, monkeypatch) -> None:
    """Worktrees with uncommitted changes are skipped (never deleted)."""
    import subprocess

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return type("R", (), {"returncode": 0, "stdout": "M changed.py\n"})()
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    wt = tmp_path / "wt"
    wt.mkdir()
    removed, skipped = diagnose._prune_orphaned_worktrees(tmp_path, [wt])
    assert removed == []
    assert skipped == [wt]


def test_prune_orphaned_worktrees_nonexistent_counted_as_removed(tmp_path, monkeypatch) -> None:
    """A worktree path that no longer exists is treated as already removed."""
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0}
    )())
    gone = tmp_path / "gone-wt"
    removed, skipped = diagnose._prune_orphaned_worktrees(tmp_path, [gone])
    assert removed == [gone]
    assert skipped == []


# ── #618: launch-failed branch in _recover_work_like ────────────────────────


def test_launch_failed_with_clean_orphan_is_recovered(monkeypatch, config) -> None:
    """A failed-at-launch assignment whose orphan can be pruned → recovered=True."""
    _stub(monkeypatch, session="dead")
    # Stub _prune_orphan_for_failed to do nothing (clean prune, no needs_reset).
    monkeypatch.setattr(diagnose, "_prune_orphan_for_failed", lambda *a, **k: None)

    a = _assign(
        aid="w-fail",
        status="failed",
        branch="issue-42-foo",
        failure_reason="branch already checked out at /some/path",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert res.recovered is True
    assert res.needs_reset is False
    assert any("launch-failed" in f for f in res.findings)


def test_launch_failed_with_dirty_orphan_not_recovered(monkeypatch, config) -> None:
    """A failed-at-launch assignment with dirty (unskippable) orphan → needs_reset=True,
    recovered=False (the contradictory state the reviewer flagged in the review)."""
    _stub(monkeypatch, session="dead")

    def _set_needs_reset(board, config, latest, res, *, dry_run):
        # Simulate dirty worktree: _prune_orphan_for_failed could not remove it.
        res.needs_reset = True

    monkeypatch.setattr(diagnose, "_prune_orphan_for_failed", _set_needs_reset)

    a = _assign(
        aid="w-fail",
        status="failed",
        branch="issue-42-foo",
        failure_reason="branch already checked out at /some/path",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    # needs_reset set by the stub → recovered must NOT also be True.
    assert res.needs_reset is True
    assert res.recovered is False


def test_launch_failed_no_branch_still_shows_finding(monkeypatch, config) -> None:
    """A failed assignment with no branch still reports the failure_reason finding."""
    _stub(monkeypatch, session="dead")
    prune_called: list = []
    monkeypatch.setattr(
        diagnose, "_prune_orphan_for_failed",
        lambda *a, **k: prune_called.append(True)
    )

    a = _assign(
        aid="w-fail",
        status="failed",
        branch=None,
        failure_reason="git error: no such branch",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    # No branch → _prune_orphan_for_failed should not be called.
    assert prune_called == []
    assert any("launch-failed" in f for f in res.findings)
    assert res.recovered is True


# ── #618: _prune_orphan_for_failed integration ──────────────────────────────


def test_prune_orphan_for_failed_no_repo_cfg(monkeypatch, config) -> None:
    """If repo is unknown in config, _prune_orphan_for_failed returns silently."""
    a = Assignment(
        machine_name="precision",
        repo_name="unknown-repo",  # not in config
        issue_number=42,
        issue_title="t",
        assignment_id="w1",
        type="work",
        status="failed",
        branch="issue-42-foo",
        dispatched_at=time.time(),
        failure_reason="some error",
    )
    res = diagnose.DiagnoseResult(repo_name="unknown-repo", issue_number=42, stage="work")
    board = Board()
    # Must not raise.
    diagnose._prune_orphan_for_failed(board, config, a, res, dry_run=False)
    # No findings added (exited early before finding orphans).
    assert not any("orphan" in f.lower() for f in res.findings)


def test_prune_orphan_for_failed_dry_run_reports_but_does_not_remove(
    monkeypatch, config, tmp_path
) -> None:
    """dry_run=True: orphans are listed but not removed."""
    import subprocess

    worktrees_dir = tmp_path / "worktrees"
    orphan = worktrees_dir / "dead-aid" / "r"
    orphan.mkdir(parents=True)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    # Stub COORD_DIR so _find_orphaned_worktrees uses our tmp worktrees_dir.
    import coord.state as state_mod
    monkeypatch.setattr(state_mod, "COORD_DIR", tmp_path)

    # Stub machine.repo_path to return our tmp repo.
    monkeypatch.setattr(
        config.machines[0], "repo_path", lambda repo_name: str(repo_path)
    )

    porcelain = _make_porcelain_output([{"path": str(orphan), "branch": "issue-42-foo"}])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    a = Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=42,
        issue_title="t",
        assignment_id="w1",
        type="work",
        status="failed",
        branch="issue-42-foo",
        dispatched_at=time.time(),
        failure_reason="branch already checked out",
    )
    board = Board()
    res = diagnose.DiagnoseResult(repo_name="api", issue_number=42, stage="work")
    diagnose._prune_orphan_for_failed(board, config, a, res, dry_run=True)
    assert any("dry-run" in f for f in res.findings)
    assert res.actions_taken == []  # nothing was actually removed


# ── #618: --orphan-worktrees CLI flag ────────────────────────────────────────


CONFIG_YAML_FOR_DIAGNOSE = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


def test_diagnose_orphan_worktrees_flag_dry_run(monkeypatch, tmp_path) -> None:
    """``coord diagnose --orphan-worktrees --dry-run`` runs the sweep without removing."""
    import subprocess

    from click.testing import CliRunner

    from coord.cli import main

    cfg_file = tmp_path / "coordinator.yml"
    cfg_file.write_text(CONFIG_YAML_FOR_DIAGNOSE)

    worktrees_dir = tmp_path / "coord_home" / "worktrees"
    orphan = worktrees_dir / "dead-aid" / "r"
    orphan.mkdir(parents=True)

    # Stub COORD_DIR so the sweep finds our tmp worktrees.
    monkeypatch.setattr("coord.state.COORD_DIR", tmp_path / "coord_home")

    # Stub build_board to return an empty board (no active assignments).
    monkeypatch.setattr("coord.state.build_board", lambda: Board())

    # Stub tmux so no sessions are considered live.
    monkeypatch.setattr("coord.interactive.tmux_available", lambda: False)

    # Stub git worktree list to return one orphan.
    repo_path = Path("/tmp/api")
    porcelain = _make_porcelain_output([{"path": str(orphan), "branch": "issue-1-foo"}])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    # Stub machine.repo_path in the loaded config so it resolves to tmp_path/api.
    api_path = tmp_path / "api"
    api_path.mkdir()

    def _patched_repo_path(self, repo_name):  # type: ignore[no-untyped-def]
        return str(api_path) if repo_name == "api" else None

    monkeypatch.setattr("coord.config.Machine.repo_path", _patched_repo_path)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["diagnose", "--config", str(cfg_file), "--orphan-worktrees", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    # Dry-run must mention the orphan but not remove it.
    assert "dry-run" in result.output
    assert orphan.exists(), "dry-run must not remove the orphan worktree"


def test_diagnose_missing_repo_and_issue_errors(monkeypatch, tmp_path) -> None:
    """``coord diagnose`` without REPO/ISSUE and without --orphan-worktrees exits 2."""
    from click.testing import CliRunner

    from coord.cli import main

    cfg_file = tmp_path / "coordinator.yml"
    cfg_file.write_text(CONFIG_YAML_FOR_DIAGNOSE)

    runner = CliRunner()
    result = runner.invoke(main, ["diagnose", "--config", str(cfg_file)])
    assert result.exit_code == 2


# ── #814: remote failed without failure_reason + base-checkout lock ──────────


def test_failed_without_failure_reason_not_healthy(monkeypatch, config) -> None:
    """A remote interactive failure sets status=failed but no failure_reason.
    _recover_work_like must NOT say 'stage looks healthy' (#814)."""
    _stub(monkeypatch, session="dead")
    # Stub _prune_orphan_for_failed to do nothing — we only care about the
    # branch in _recover_work_like, not about what the prune helper does.
    monkeypatch.setattr(diagnose, "_prune_orphan_for_failed", lambda *a, **k: None)

    a = _assign(
        aid="w-remote-fail",
        status="failed",
        branch="issue-42-foo",
        failure_reason=None,  # remote path doesn't set this
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    # Must NOT say "stage looks healthy" — the stage has failed.
    assert not any("looks healthy" in f for f in res.findings), (
        f"should not say 'looks healthy' for a failed stage; findings={res.findings}"
    )
    # Must report the failed state.
    assert any("failed" in f for f in res.findings), (
        f"expected a 'failed' finding; findings={res.findings}"
    )
    # recoverd=True is fine — the stage row is terminal.
    assert res.recovered is True


def test_maybe_fix_base_checkout_lock_reports_finding(monkeypatch, config) -> None:
    """When the base checkout on a remote machine holds the branch, diagnose
    reports a finding and fixes it via SSH (#814)."""
    _BASE = "/home/john/src/api"
    free_calls: list = []

    monkeypatch.setattr(
        "coord.interactive.find_remote_branch_holder",
        lambda *a, **kw: _BASE,
    )
    monkeypatch.setattr(
        "coord.interactive._remote_base_checkout_free_branch",
        lambda *a, **kw: free_calls.append(True) or True,
    )

    from coord.diagnose import DiagnoseResult, _maybe_fix_base_checkout_lock

    # Give the machine a repo_path so the helper can build the SSH path.
    config.machines[0].repo_paths["api"] = "~/src/api"

    a = _assign(
        aid="w-base-lock",
        status="failed",
        branch="issue-42-foo",
        failure_reason=None,
    )
    res = DiagnoseResult(repo_name="api", issue_number=42, stage="work")
    _maybe_fix_base_checkout_lock(a, config, "issue-42-foo", res, dry_run=False)

    # Must have reported a finding about the base checkout.
    assert any("base checkout" in f for f in res.findings), (
        f"expected 'base checkout' finding; got {res.findings}"
    )
    # Must have called the free function.
    assert len(free_calls) == 1, (
        f"expected _remote_base_checkout_free_branch called once; got {free_calls!r}"
    )
    # Must have recorded an action.
    assert any("freed" in act for act in res.actions_taken), (
        f"expected 'freed' action; got {res.actions_taken}"
    )


def test_maybe_fix_base_checkout_lock_dry_run(monkeypatch, config) -> None:
    """dry_run=True: reports finding but does not call the SSH free function."""
    _BASE = "/home/john/src/api"
    free_calls: list = []

    monkeypatch.setattr(
        "coord.interactive.find_remote_branch_holder",
        lambda *a, **kw: _BASE,
    )
    monkeypatch.setattr(
        "coord.interactive._remote_base_checkout_free_branch",
        lambda *a, **kw: free_calls.append(True) or True,
    )

    from coord.diagnose import DiagnoseResult, _maybe_fix_base_checkout_lock

    config.machines[0].repo_paths["api"] = "~/src/api"

    a = _assign(
        aid="w-base-dry",
        status="failed",
        branch="issue-42-foo",
        failure_reason=None,
    )
    res = DiagnoseResult(repo_name="api", issue_number=42, stage="work")
    _maybe_fix_base_checkout_lock(a, config, "issue-42-foo", res, dry_run=True)

    assert free_calls == [], "dry_run=True must not call the SSH free function"
    assert any("dry-run" in f for f in res.findings), (
        f"expected dry-run finding; got {res.findings}"
    )


def test_maybe_fix_base_checkout_lock_no_base_holder(monkeypatch, config) -> None:
    """When find_remote_branch_holder returns None, no finding is added."""
    monkeypatch.setattr(
        "coord.interactive.find_remote_branch_holder",
        lambda *a, **kw: None,
    )

    from coord.diagnose import DiagnoseResult, _maybe_fix_base_checkout_lock

    config.machines[0].repo_paths["api"] = "~/src/api"

    a = _assign(
        aid="w-no-holder",
        status="failed",
        branch="issue-42-foo",
        failure_reason=None,
    )
    res = DiagnoseResult(repo_name="api", issue_number=42, stage="work")
    _maybe_fix_base_checkout_lock(a, config, "issue-42-foo", res, dry_run=False)

    assert not res.findings, (
        f"no findings expected when holder is None; got {res.findings}"
    )
    assert not res.actions_taken, res.actions_taken


# ── #935 Part C: DiagnoseResult.to_json_dict + coord diagnose --json ─────────


def test_diagnose_result_to_json_dict_roundtrips_all_fields() -> None:
    """``to_json_dict`` must serialise all dataclass fields correctly."""
    import json
    from coord.diagnose import DiagnoseResult

    res = DiagnoseResult(
        repo_name="api",
        issue_number=42,
        stage="work",
        findings=["phantom running"],
        actions_taken=["finalized work assignment"],
        recovered=True,
        needs_reset=False,
        branch_preserved=True,
        reset_performed=False,
    )
    d = res.to_json_dict()

    # Verify JSON-serialisable (no TypeError on dump).
    serialised = json.dumps(d)
    roundtripped = json.loads(serialised)

    assert roundtripped["repo_name"] == "api"
    assert roundtripped["issue_number"] == 42
    assert roundtripped["stage"] == "work"
    assert roundtripped["findings"] == ["phantom running"]
    assert roundtripped["actions_taken"] == ["finalized work assignment"]
    assert roundtripped["recovered"] is True
    assert roundtripped["needs_reset"] is False
    assert roundtripped["branch_preserved"] is True
    assert roundtripped["reset_performed"] is False


def test_diagnose_result_to_json_dict_empty_lists() -> None:
    """Works with default empty lists (no findings or actions)."""
    import json
    from coord.diagnose import DiagnoseResult

    res = DiagnoseResult(repo_name="myrepo", issue_number=7, stage="review")
    d = res.to_json_dict()
    assert d["findings"] == []
    assert d["actions_taken"] == []
    # JSON-serialisable
    json.dumps(d)


def test_diagnose_json_flag_emits_json_line(monkeypatch) -> None:
    """``coord diagnose --json`` must print a ``DIAGNOSE_JSON:`` line containing
    a JSON-encoded DiagnoseResult before the ``DIAGNOSE_RESULT:`` trailer."""
    import json
    from click.testing import CliRunner
    from coord.commands.status import diagnose as diagnose_cmd

    # Stub out the heavy lifting so no DB / git is needed.
    monkeypatch.setattr("coord.board_service.daemon_reroute_target", lambda _: None)

    def _fake_build_board():
        from coord.models import Board
        return Board()

    monkeypatch.setattr("coord.commands.status.sys.exit", lambda c: None)

    from coord import diagnose as diag_mod

    # Stub out everything that touches the filesystem.
    monkeypatch.setattr(diag_mod, "_session_state", lambda a, c: "dead")
    monkeypatch.setattr(diag_mod, "_finalize_dead", lambda a, c: "advisory")
    monkeypatch.setattr(diag_mod, "_kill_session", lambda a, c: True)
    monkeypatch.setattr(diag_mod, "_recover_review_findings", lambda a, c: None)
    monkeypatch.setattr(diag_mod, "_reconcile_issue_merges",
                        lambda b, c, r, i, *, dry_run: [])

    # Provide a minimal config so _load_config doesn't error.
    from coord.config import Config
    from coord.models import Board, Repo, Machine

    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="p.tail", repos=["api"])],
    )
    monkeypatch.setattr("coord.commands.status._load_config", lambda p: cfg)

    # build_board is also a local import; patch at its canonical site.
    import coord.state as state_mod  # noqa: PLC0415
    monkeypatch.setattr(state_mod, "build_board", lambda: Board())

    # Patch the diagnose_stage + current_stage so we don't need a real board.
    # These are imported locally inside the diagnose() function, so patch at
    # their definition site in coord.diagnose.
    from coord.diagnose import DiagnoseResult
    fake_result = DiagnoseResult(
        repo_name="api",
        issue_number=99,
        stage="work",
        findings=["phantom work running"],
        actions_taken=["finalized it"],
        recovered=True,
        needs_reset=False,
    )
    monkeypatch.setattr(diag_mod, "diagnose_stage",
                        lambda *a, **kw: fake_result)
    monkeypatch.setattr(diag_mod, "current_stage",
                        lambda *a: "work")

    runner = CliRunner()
    result = runner.invoke(
        diagnose_cmd,
        ["api", "99", "--json", "--dry-run"],
        catch_exceptions=False,
    )

    output = result.output
    assert result.exit_code == 0, f"command failed:\n{output}"

    # Must contain a DIAGNOSE_JSON line
    json_lines = [l for l in output.splitlines() if l.startswith("DIAGNOSE_JSON:")]
    assert json_lines, f"DIAGNOSE_JSON line missing in output:\n{output}"

    payload = json.loads(json_lines[0][len("DIAGNOSE_JSON:"):])
    assert payload["repo_name"] == "api"
    assert payload["issue_number"] == 99
    assert payload["stage"] == "work"
    assert payload["recovered"] is True
    assert payload["findings"] == ["phantom work running"]
    assert payload["actions_taken"] == ["finalized it"]

    # DIAGNOSE_RESULT trailer must also still be present.
    trailer_lines = [l for l in output.splitlines() if l.startswith("DIAGNOSE_RESULT:")]
    assert trailer_lines, f"DIAGNOSE_RESULT trailer missing in output:\n{output}"


def test_diagnose_without_json_flag_no_json_line(monkeypatch) -> None:
    """Without ``--json``, no ``DIAGNOSE_JSON:`` line must appear."""
    from click.testing import CliRunner
    from coord.commands.status import diagnose as diagnose_cmd
    from coord.config import Config
    from coord.diagnose import DiagnoseResult
    from coord.models import Board, Repo, Machine
    import coord.diagnose as diag_mod  # noqa: PLC0415
    import coord.state as state_mod  # noqa: PLC0415

    monkeypatch.setattr("coord.board_service.daemon_reroute_target", lambda _: None)

    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="p.tail", repos=["api"])],
    )
    fake_result = DiagnoseResult(repo_name="api", issue_number=99, stage="work")
    monkeypatch.setattr("coord.commands.status._load_config", lambda p: cfg)
    monkeypatch.setattr(diag_mod, "diagnose_stage", lambda *a, **kw: fake_result)
    monkeypatch.setattr(diag_mod, "current_stage", lambda *a: "work")
    monkeypatch.setattr(state_mod, "build_board", lambda: Board())

    runner = CliRunner()
    result = runner.invoke(diagnose_cmd, ["api", "99", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "DIAGNOSE_JSON:" not in result.output
    assert "DIAGNOSE_RESULT:" in result.output
