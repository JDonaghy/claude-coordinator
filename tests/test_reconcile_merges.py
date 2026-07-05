"""Tests for reconcile_board_merges: branch backfill (#611) + record merged (#609) + close stale PRs (#721) + prune stale queue (#732) + settle sibling ghosts (#894)."""

from __future__ import annotations

import pytest

from coord.config import Config
from coord.models import Assignment, Board, Repo
from coord.reconcile import close_stale_prs, reconcile_board_merges


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[],
    )


def _done_work(
    *,
    assignment_id: str = "abc",
    issue_number: int = 42,
    branch: str | None = None,
    status: str = "done",
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="t",
        status=status,
        assignment_id=assignment_id,
        branch=branch,
        type="work",
    )


def _patch_probes(
    monkeypatch,
    *,
    remote_branches: set[str] | None = None,
    terminal: bool = False,
):
    """Stub the git/gh probes + record state writes; never hit the network.

    Also stubs ``list_open_prs`` to return an empty list so the stale-PR
    sweep (#721) does not fire for tests that only care about the earlier
    reconcile sweeps (branch backfill, record-merged).

    Now also stubs ``mark_sibling_review_done`` and ``mark_advisory_settled``
    (#894) so sweep (e) never touches the DB in these tests.
    """
    from coord import github_ops, state

    monkeypatch.setattr(
        github_ops, "list_remote_branch_names",
        lambda repo: set(remote_branches or set()),
    )
    monkeypatch.setattr(
        github_ops, "work_is_terminal",
        lambda repo, issue, branch, cache=None: terminal,
    )
    # Suppress the stale-PR sweep for tests that don't need it.
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        state, "update_assignment_branch",
        lambda aid, branch: writes.append(("branch", aid)),
    )
    monkeypatch.setattr(
        state, "mark_assignment_merged",
        lambda aid: writes.append(("merged", aid)),
    )
    # #894: stub sibling-settling functions so sweep (e) never touches the DB.
    monkeypatch.setattr(
        state, "mark_sibling_review_done",
        lambda aid: writes.append(("sibling_review_done", aid)),
    )
    monkeypatch.setattr(
        state, "mark_advisory_settled",
        lambda aid: writes.append(("advisory_settled", aid)),
    )
    # #951: stub the type=work review_state settle so sweep (b) never touches
    # the DB in these tests.
    monkeypatch.setattr(
        state, "mark_work_review_settled",
        lambda aid: writes.append(("work_review_settled", aid)),
    )
    return writes


# ── #611 branch backfill ──────────────────────────────────────────────────


def test_backfills_branch_from_single_matching_remote(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(
        monkeypatch, remote_branches={"issue-42-fix", "main"}, terminal=False
    )

    actions = reconcile_board_merges(board, config)

    assert a.branch == "issue-42-fix"
    assert ("branch", "abc") in writes
    assert any("backfill branch" in s for s in actions)


def test_no_backfill_when_branch_candidate_ambiguous(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(
        monkeypatch,
        remote_branches={"issue-42-fix", "issue-42-other"},
        terminal=False,
    )

    actions = reconcile_board_merges(board, config)

    assert a.branch is None
    assert ("branch", "abc") not in writes
    assert any("ambiguous" in s for s in actions)


def test_no_backfill_when_no_matching_remote(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, remote_branches={"main"}, terminal=False)

    actions = reconcile_board_merges(board, config)

    assert a.branch is None
    assert writes == []
    assert any("no remote branch" in s for s in actions)


# ── #609 record out-of-band merges ─────────────────────────────────────────


def test_marks_merged_when_branch_is_terminal(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch="issue-42-fix")
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert ("merged", "abc") in writes
    assert any("mark merged" in s for s in actions)


def test_does_not_mark_merged_when_not_terminal(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch="issue-42-fix")
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=False)

    actions = reconcile_board_merges(board, config)

    assert a.status == "done"
    assert writes == []


def test_backfill_then_mark_merged_in_one_sweep(monkeypatch, config) -> None:
    """A done-no-branch row that is also merged: backfill then flip in one pass."""
    a = _done_work(issue_number=42, branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(
        monkeypatch, remote_branches={"issue-42-fix"}, terminal=True
    )

    reconcile_board_merges(board, config)

    assert a.branch == "issue-42-fix"
    assert a.status == "merged"
    assert ("branch", "abc") in writes
    assert ("merged", "abc") in writes


# ── #951 settle type=work review-stage ghost rows ──────────────────────────
#
# `mark_assignment_merged` (#609, sweep b above) only flips `status`, it never
# touches `review_state`. Every finished work assignment defaults to
# `review_state='pending'` (reconcile Pass 1 sets it unconditionally), so that
# ghost survives the status flip to 'merged' and the row keeps surfacing as
# "[awaiting review]" in `coord status` / the TUI forever — the display tag
# (coord/commands/status.py) is keyed on review_state independent of status.
# These rows also fell outside sweep (e) #894's sibling settle, which
# explicitly excludes type='work'. See issue #951.


def test_settles_work_review_state_when_terminal(monkeypatch, config) -> None:
    """A type=work done+review_state=pending row for a closed/merged issue
    must be settled: status flips to merged AND the review_state='pending'
    ghost clears, so it stops surfacing as "[awaiting review]" (#951)."""
    a = _done_work(issue_number=42, branch="issue-42-fix")
    a.review_state = "pending"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert a.status == "merged"
    assert a.review_state == "done"
    assert ("merged", "abc") in writes
    assert ("work_review_settled", "abc") in writes
    assert any("mark merged" in s for s in actions)


def test_settles_work_review_state_without_branch_when_issue_closed(
    monkeypatch, config
) -> None:
    """#951: the issue-closed fast path must not require a resolvable branch.

    A done+pending work row whose branch could not be backfilled (no
    matching remote branch at all) still must settle when work_is_terminal
    is confirmed — e.g. via issue_is_closed, which needs no branch. Before
    the fix, the backfill's `continue` on a failed match stranded the row
    before sweep (b) ever ran the terminality check.
    """
    a = _done_work(issue_number=42, branch=None)
    a.review_state = "pending"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, remote_branches=set(), terminal=True)

    actions = reconcile_board_merges(board, config)

    assert a.branch is None  # never resolved — the fast path needs none
    assert a.status == "merged"
    assert a.review_state == "done"
    assert ("merged", "abc") in writes
    assert ("work_review_settled", "abc") in writes
    assert any("mark merged" in s for s in actions)


def test_work_review_state_left_untouched_when_issue_open(monkeypatch, config) -> None:
    """Fail-open: a still-OPEN issue's done+pending work row must be left alone."""
    a = _done_work(issue_number=42, branch="issue-42-fix")
    a.review_state = "pending"
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=False)

    actions = reconcile_board_merges(board, config)

    assert a.status == "done"
    assert a.review_state == "pending"
    assert writes == []
    assert not any("mark merged" in s for s in actions)


# ── dry_run + filters ──────────────────────────────────────────────────────


def test_dry_run_makes_no_writes(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch=None)
    board = Board(completed=[a])
    writes = _patch_probes(
        monkeypatch, remote_branches={"issue-42-fix"}, terminal=True
    )

    actions = reconcile_board_merges(board, config, dry_run=True)

    # No board mutation and no state writes.
    assert a.branch is None
    assert a.status == "done"
    assert writes == []
    # The actions still describe what WOULD change.
    assert any("[dry-run]" in s for s in actions)


def test_repo_filter_skips_other_repos(monkeypatch, config) -> None:
    a = _done_work(issue_number=42, branch="issue-42-fix")
    board = Board(completed=[a])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config, repo="other-repo")

    assert a.status == "done"
    assert writes == []
    assert actions == []


def test_skips_non_work_and_non_done(monkeypatch, config) -> None:
    review = _done_work(assignment_id="rev", branch=None)
    review.type = "review"
    running = _done_work(assignment_id="run", status="running", branch=None)
    board = Board(active=[running], completed=[review])
    writes = _patch_probes(
        monkeypatch, remote_branches={"issue-42-fix"}, terminal=True
    )

    actions = reconcile_board_merges(board, config)

    assert writes == []
    assert actions == []


# ── #721 close stale PRs ──────────────────────────────────────────────────────


def _patch_stale_pr_probes(
    monkeypatch,
    *,
    open_prs: list[dict] | None = None,
    issue_closed: bool = False,
    fully_merged: bool = False,
) -> list[tuple]:
    """Stub the github_ops probes for the stale-PR sweep; record close calls."""
    from coord import github_ops

    monkeypatch.setattr(
        github_ops, "list_open_prs",
        lambda repo: list(open_prs or []),
    )
    monkeypatch.setattr(
        github_ops, "issue_is_closed",
        lambda repo, num: issue_closed,
    )
    monkeypatch.setattr(
        github_ops, "branch_is_fully_merged",
        lambda repo, branch, default_branch: fully_merged,
    )

    closed: list[tuple] = []
    monkeypatch.setattr(
        github_ops, "close_pr",
        lambda repo, number, comment=None: closed.append((repo, number)),
    )
    return closed


def test_stale_pr_closed_when_issue_is_closed(monkeypatch, config) -> None:
    """A PR linked to a closed issue must be closed by the sweep."""
    prs = [{"number": 99, "headRefName": "issue-42-the-fix"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=True, fully_merged=False
    )

    actions = close_stale_prs(config)

    assert ("acme/api", 99) in closed
    assert any("close PR #99" in s and "issue #42 is closed" in s for s in actions)


def test_stale_pr_closed_when_branch_fully_merged(monkeypatch, config) -> None:
    """A PR whose branch is fully on the default branch must be closed."""
    prs = [{"number": 77, "headRefName": "issue-10-feature"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=False, fully_merged=True
    )

    actions = close_stale_prs(config)

    assert ("acme/api", 77) in closed
    assert any("close PR #77" in s and "already on" in s for s in actions)


def test_live_pr_not_closed(monkeypatch, config) -> None:
    """A PR whose issue is open and branch still has commits ahead must be left alone."""
    prs = [{"number": 55, "headRefName": "issue-7-wip"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=False, fully_merged=False
    )

    actions = close_stale_prs(config)

    assert closed == []
    assert not any("close PR" in s for s in actions)


def test_stale_pr_dry_run_no_close(monkeypatch, config) -> None:
    """dry_run=True must list stale PRs without closing them."""
    prs = [{"number": 33, "headRefName": "issue-5-done"}]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=True, fully_merged=False
    )

    actions = close_stale_prs(config, dry_run=True)

    assert closed == []
    assert any("[dry-run]" in s for s in actions)
    assert any("close PR #33" in s for s in actions)


def test_non_coord_branch_skipped(monkeypatch, config) -> None:
    """PRs whose head branch does not follow issue-{N}-* must be ignored."""
    prs = [
        {"number": 11, "headRefName": "feature/some-thing"},
        {"number": 12, "headRefName": "dependabot/pip/requests-2.32"},
    ]
    closed = _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=True, fully_merged=True
    )

    actions = close_stale_prs(config)

    assert closed == []
    assert not any("close PR" in s for s in actions)


def test_stale_pr_sweep_integrated_into_reconcile_board_merges(
    monkeypatch, config
) -> None:
    """reconcile_board_merges must include stale-PR actions in its output."""
    # Empty board — all three earlier sweeps produce nothing.
    board = Board(completed=[], active=[])
    # Patch the board-level probes so reconcile_board_merges doesn't error.
    _patch_probes(monkeypatch, remote_branches=set(), terminal=False)

    prs = [{"number": 44, "headRefName": "issue-9-old-work"}]
    _patch_stale_pr_probes(
        monkeypatch, open_prs=prs, issue_closed=True, fully_merged=False
    )

    actions = reconcile_board_merges(board, config)

    assert any("close PR #44" in s for s in actions)


# ── #732 prune stale merge_queue entries ─────────────────────────────────────


def test_reconcile_prunes_conflict_entry_for_closed_issue(
    monkeypatch, config, coord_db
) -> None:
    """Acceptance test: a CONFLICT entry whose issue is closed is pruned by
    reconcile_board_merges and no longer appears in pending_summary (#732)."""
    from coord import github_ops
    from coord import merge_queue as mq
    from coord.merge_queue import CONFLICT, QueuedMerge, save_queue

    # Seed a stale conflict entry (mirrors the #217 incident: assignment
    # id=60275968b733, issue=#217, state=conflict, issue now closed).
    save_queue([
        QueuedMerge(
            assignment_id="60275968b733",
            repo_name="api",
            repo_github="acme/api",
            branch="issue-217-old-work",
            target_branch="main",
            issue_number=217,
            issue_title="Old closed issue",
            state=CONFLICT,
        )
    ])

    # Stub network calls: issue 217 is closed; no open PRs.
    monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: n == 217)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: False)
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **kw: False)
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    board = Board(completed=[], active=[])
    actions = reconcile_board_merges(board, config)

    # The prune action must be reported.
    assert any("prune queue entry 60275968b733" in s for s in actions)

    # The queue must now be empty.
    remaining = mq.load_queue()
    assert remaining == [], f"Expected empty queue, got {remaining}"

    # pending_summary must no longer report a conflict.
    summary = mq.pending_summary(mq.load_queue())
    assert summary == {}, f"Expected no pending entries, got {summary}"


def test_reconcile_prunes_conflict_entry_for_merged_pr(
    monkeypatch, config, coord_db
) -> None:
    """A CONFLICT entry whose PR is already merged is pruned."""
    from coord import github_ops
    from coord import merge_queue as mq
    from coord.merge_queue import CONFLICT, QueuedMerge, save_queue

    save_queue([
        QueuedMerge(
            assignment_id="aid-merged-pr",
            repo_name="api",
            repo_github="acme/api",
            branch="issue-50-feature",
            target_branch="main",
            issue_number=50,
            issue_title="Already merged",
            state=CONFLICT,
        )
    ])

    monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: False)
    monkeypatch.setattr(
        github_ops, "pr_is_merged",
        lambda repo, b: b == "issue-50-feature",
    )
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **kw: False)
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    board = Board(completed=[], active=[])
    actions = reconcile_board_merges(board, config)

    assert any("prune queue entry aid-merged-pr" in s for s in actions)
    assert mq.load_queue() == []


def test_reconcile_prune_dry_run_does_not_remove_entry(
    monkeypatch, config, coord_db
) -> None:
    """dry_run=True reports what would be pruned without modifying the queue."""
    from coord import github_ops
    from coord import merge_queue as mq
    from coord.merge_queue import CONFLICT, QueuedMerge, save_queue

    save_queue([
        QueuedMerge(
            assignment_id="dry-stale",
            repo_name="api",
            repo_github="acme/api",
            branch="issue-99-stale",
            target_branch="main",
            issue_number=99,
            issue_title="Stale",
            state=CONFLICT,
        )
    ])

    monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: True)
    monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: False)
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **kw: False)
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])

    board = Board(completed=[], active=[])
    actions = reconcile_board_merges(board, config, dry_run=True)

    assert any("dry-stale" in s and "dry-run" in s for s in actions)
    assert len(mq.load_queue()) == 1  # still there


# ── #894 settle sibling ghost rows ────────────────────────────────────────────


def _ghost_sibling(
    *,
    assignment_id: str,
    issue_number: int = 42,
    sibling_type: str = "review",
    status: str = "done",
    review_state: str | None = "pending",
    branch: str | None = None,
) -> Assignment:
    """Build a completed sibling assignment (review/smoke/conflict-fix/advisory)."""
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="t",
        status=status,
        assignment_id=assignment_id,
        branch=branch,
        type=sibling_type,
        review_state=review_state,
    )


def test_settles_review_sibling_when_issue_terminal(monkeypatch, config) -> None:
    """A type=review done+review_state=pending row must be settled when work_is_terminal."""
    work = _done_work(assignment_id="work-1", issue_number=42, branch="issue-42-fix")
    review = _ghost_sibling(assignment_id="rev-1", sibling_type="review", review_state="pending")
    board = Board(completed=[work, review])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    # work row: flipped to merged
    assert work.status == "merged"
    assert ("merged", "work-1") in writes
    # review sibling: review_state cleared
    assert review.review_state == "done"
    assert ("sibling_review_done", "rev-1") in writes
    assert any("settle sibling" in s and "rev-1" in s for s in actions)


def test_settles_smoke_sibling_when_issue_terminal(monkeypatch, config) -> None:
    """A type=smoke done+review_state=pending row must be settled when work_is_terminal."""
    work = _done_work(assignment_id="work-2", issue_number=42, branch="issue-42-fix")
    smoke = _ghost_sibling(assignment_id="smk-1", sibling_type="smoke", review_state="pending")
    board = Board(completed=[work, smoke])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert smoke.review_state == "done"
    assert ("sibling_review_done", "smk-1") in writes
    assert any("settle sibling" in s and "smk-1" in s for s in actions)


def test_settles_advisory_row_when_issue_terminal(monkeypatch, config) -> None:
    """A status=advisory row must be flipped to merged when work_is_terminal."""
    work = _done_work(assignment_id="work-3", issue_number=42, branch="issue-42-fix")
    advisory = _ghost_sibling(
        assignment_id="adv-1", sibling_type="work",
        status="advisory", review_state=None,
    )
    board = Board(completed=[work, advisory])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config)

    assert advisory.status == "merged"
    assert ("advisory_settled", "adv-1") in writes
    assert any("settle advisory" in s and "adv-1" in s for s in actions)


def test_settles_all_ghost_types_in_one_pass(monkeypatch, config) -> None:
    """All three ghost-row types — review, smoke, advisory — are settled together.

    Acceptance criterion: a merged+closed issue with leftover type=review/smoke/
    advisory rows → after reconcile_board_merges, NONE remain non-terminal.
    """
    work = _done_work(assignment_id="w0", issue_number=42, branch="issue-42-fix")
    review = _ghost_sibling(assignment_id="rv0", sibling_type="review", review_state="pending")
    smoke = _ghost_sibling(assignment_id="sk0", sibling_type="smoke", review_state="pending")
    conflict_fix = _ghost_sibling(
        assignment_id="cf0", sibling_type="conflict-fix", review_state="pending"
    )
    advisory = _ghost_sibling(
        assignment_id="ad0", sibling_type="work", status="advisory", review_state=None,
    )
    board = Board(completed=[work, review, smoke, conflict_fix, advisory])
    writes = _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config)

    # All ghost rows settled — none remain non-terminal.
    assert review.review_state == "done", "review sibling must have review_state='done'"
    assert smoke.review_state == "done", "smoke sibling must have review_state='done'"
    assert conflict_fix.review_state == "done", "conflict-fix sibling must be settled"
    assert advisory.status == "merged", "advisory row must be flipped to 'merged'"
    # Work row also settled.
    assert work.status == "merged"


def test_ghost_rows_not_settled_when_issue_not_terminal(monkeypatch, config) -> None:
    """Ghost rows for a still-open/unmerged issue must be left untouched.

    Acceptance criterion: a still-open/unmerged issue's rows are left untouched.
    """
    work = _done_work(assignment_id="w-live", issue_number=7, branch="issue-7-fix")
    review = _ghost_sibling(
        assignment_id="rv-live", sibling_type="review",
        issue_number=7, review_state="pending",
    )
    advisory = _ghost_sibling(
        assignment_id="ad-live", sibling_type="work",
        issue_number=7, status="advisory", review_state=None,
    )
    board = Board(completed=[work, review, advisory])
    writes = _patch_probes(monkeypatch, terminal=False)  # issue NOT terminal

    reconcile_board_merges(board, config)

    assert work.status == "done"            # work untouched
    assert review.review_state == "pending" # sibling untouched
    assert advisory.status == "advisory"    # advisory untouched
    assert not any("settle" in s for s in [])
    assert ("sibling_review_done", "rv-live") not in writes
    assert ("advisory_settled", "ad-live") not in writes


def test_sibling_settle_dry_run_makes_no_writes(monkeypatch, config) -> None:
    """dry_run=True describes what would settle without mutating anything."""
    work = _done_work(assignment_id="w-dry", issue_number=42, branch="issue-42-fix")
    review = _ghost_sibling(assignment_id="rv-dry", sibling_type="review", review_state="pending")
    advisory = _ghost_sibling(
        assignment_id="ad-dry", sibling_type="work", status="advisory", review_state=None,
    )
    board = Board(completed=[work, review, advisory])
    writes = _patch_probes(monkeypatch, terminal=True)

    actions = reconcile_board_merges(board, config, dry_run=True)

    # No in-memory mutations.
    assert review.review_state == "pending"
    assert advisory.status == "advisory"
    assert work.status == "done"
    # No state writes.
    assert ("sibling_review_done", "rv-dry") not in writes
    assert ("advisory_settled", "ad-dry") not in writes
    # Actions describe what WOULD happen.
    assert any("settle sibling" in s and "[dry-run]" in s for s in actions)
    assert any("settle advisory" in s and "[dry-run]" in s for s in actions)


def test_sibling_settle_respects_issue_filter(monkeypatch, config) -> None:
    """The --issue filter scopes sibling settling to the targeted issue."""
    # Issue 42: terminal, has ghost review sibling.
    work42 = _done_work(assignment_id="w42", issue_number=42, branch="issue-42-fix")
    review42 = _ghost_sibling(assignment_id="rv42", sibling_type="review", issue_number=42)
    # Issue 7: also terminal, has ghost review sibling — but outside the filter.
    work7 = _done_work(assignment_id="w7", issue_number=7, branch="issue-7-fix")
    review7 = _ghost_sibling(assignment_id="rv7", sibling_type="review", issue_number=7)
    board = Board(completed=[work42, review42, work7, review7])
    writes = _patch_probes(monkeypatch, terminal=True)

    reconcile_board_merges(board, config, issue=42)

    # Issue 42 ghost settled; issue 7 ghost untouched.
    assert review42.review_state == "done"
    assert ("sibling_review_done", "rv42") in writes
    assert review7.review_state == "pending"
    assert ("sibling_review_done", "rv7") not in writes


def test_sibling_already_merged_branch_used_for_terminality(monkeypatch, config) -> None:
    """When the sibling row has no branch, the work row's branch is used for
    the work_is_terminal probe so the pr_is_merged fast-path can fire."""
    # Work row has a branch; sibling has none.
    work = _done_work(assignment_id="w-nb", issue_number=42, branch="issue-42-fix")
    # Sibling row has no branch — relies on work row's branch for the probe.
    review = _ghost_sibling(
        assignment_id="rv-nb", sibling_type="review",
        review_state="pending", branch=None,
    )
    board = Board(completed=[work, review])

    probed_branches: list[str | None] = []

    from coord import github_ops, state  # noqa: PLC0415

    def _track_terminal(repo, issue, branch, cache=None):
        probed_branches.append(branch)
        return True  # always terminal for this test

    monkeypatch.setattr(github_ops, "work_is_terminal", _track_terminal)
    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())
    monkeypatch.setattr(github_ops, "list_open_prs", lambda repo: [])
    monkeypatch.setattr(state, "update_assignment_branch", lambda *a: None)
    monkeypatch.setattr(state, "mark_assignment_merged", lambda *a: None)
    monkeypatch.setattr(state, "mark_sibling_review_done", lambda *a: None)
    monkeypatch.setattr(state, "mark_advisory_settled", lambda *a: None)

    reconcile_board_merges(board, config)

    # The sweep should have used the work row's branch for the sibling probe.
    assert "issue-42-fix" in probed_branches, (
        f"Expected 'issue-42-fix' in probed branches; got {probed_branches}"
    )
