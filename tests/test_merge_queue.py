"""Tests for coord.merge_queue — sequencing logic and the gh-driven processor."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from coord import merge_queue as mq
from coord.merge_queue import (
    CONFLICT,
    MERGED,
    MERGING,
    PENDING,
    QueuedMerge,
    enqueue,
    load_queue,
    pending_summary,
    process,
    reorder,
    save_queue,
    sequence,
)
from coord.models import Assignment


def _q(
    aid: str,
    *,
    repo: str = "api",
    repo_github: str = "acme/api",
    branch: str | None = None,
    target: str = "main",
    size: int | None = None,
    state: str = PENDING,
    pr: int | None = None,
) -> QueuedMerge:
    return QueuedMerge(
        assignment_id=aid,
        repo_name=repo,
        repo_github=repo_github,
        branch=branch or f"worker/{aid}",
        target_branch=target,
        issue_number=1,
        issue_title="t",
        state=state,
        size=size,
        pr_number=pr,
    )


# ── Pure logic ───────────────────────────────────────────────────────────────

class TestSequence:
    def test_sorts_by_size_ascending(self) -> None:
        items = [_q("a", size=500), _q("b", size=50), _q("c", size=100)]
        ordered = sequence(items)
        assert [x.assignment_id for x in ordered] == ["b", "c", "a"]

    def test_unknown_size_goes_last_and_tiebreaks_by_id(self) -> None:
        items = [_q("z"), _q("a"), _q("m", size=10)]
        ordered = sequence(items)
        assert [x.assignment_id for x in ordered] == ["m", "a", "z"]

    def test_only_pending_returned(self) -> None:
        items = [
            _q("a", size=10, state=PENDING),
            _q("b", size=5, state=MERGED),
            _q("c", size=20, state=CONFLICT),
        ]
        assert [x.assignment_id for x in sequence(items)] == ["a"]


class TestReorder:
    def test_explicit_order_wins(self) -> None:
        items = [_q("a", size=10), _q("b", size=20), _q("c", size=5)]
        out = reorder(items, ["b", "a"])
        assert [x.assignment_id for x in out] == ["b", "a", "c"]

    def test_unknown_ids_dropped(self) -> None:
        items = [_q("a"), _q("b")]
        out = reorder(items, ["ghost", "a"])
        assert [x.assignment_id for x in out] == ["a", "b"]


# ── Persistence (SQLite-based) ────────────────────────────────────────────────

class TestPersistence:
    def test_roundtrip(self, coord_db) -> None:
        items = [_q("a", size=10), _q("b", size=20)]
        save_queue(items)
        again = load_queue()
        assert [x.assignment_id for x in again] == ["a", "b"]
        assert again[0].size == 10

    def test_load_empty_returns_empty(self, coord_db) -> None:
        assert load_queue() == []

    def test_save_replaces_all(self, coord_db) -> None:
        save_queue([_q("old")])
        save_queue([_q("new1"), _q("new2")])
        items = load_queue()
        assert [x.assignment_id for x in items] == ["new1", "new2"]


class TestEnqueue:
    def _assignment(self, *, branch: str | None = "worker/foo") -> Assignment:
        return Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="abc", branch=branch, status="done",
        )

    def test_enqueue_appends(self, coord_db) -> None:
        entry = enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        assert entry is not None
        assert load_queue()[0].assignment_id == "abc"

    def test_idempotent(self, coord_db) -> None:
        enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        second = enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        assert second is None
        assert len(load_queue()) == 1

    def test_skipped_when_no_branch(self, coord_db) -> None:
        a = self._assignment(branch=None)
        assert enqueue(a, repo_github="acme/api", target_branch="main") is None
        assert load_queue() == []

    def test_dedup_by_branch_not_assignment_id(self, coord_db) -> None:
        """#274: a second work assignment on the same branch — fix-1 in the
        auto-loop, or the PR-creator dispatched by ``coord pr`` — must not
        produce a duplicate queue row."""
        first = Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", branch="issue-1-foo", status="done",
        )
        fix = Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="[fix-1] t",
            assignment_id="fix1", branch="issue-1-foo", status="done",
        )
        assert enqueue(first, repo_github="acme/api", target_branch="main") is not None
        assert enqueue(fix, repo_github="acme/api", target_branch="main") is None
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "orig"

    def test_different_branch_same_repo_still_enqueues(self, coord_db) -> None:
        """Sanity: dedup is scoped to (repo_github, branch), not repo alone."""
        a1 = Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="a1", branch="issue-1-foo", status="done",
        )
        a2 = Assignment(
            machine_name="m", repo_name="api", issue_number=2, issue_title="t",
            assignment_id="a2", branch="issue-2-bar", status="done",
        )
        assert enqueue(a1, repo_github="acme/api", target_branch="main") is not None
        assert enqueue(a2, repo_github="acme/api", target_branch="main") is not None
        assert len(load_queue()) == 2


# ── Processing with a stub gh ops ────────────────────────────────────────────

@dataclass
class FakeGh:
    """Stub the surface in `coord.merge_queue.GhOps`."""

    sizes: dict[int, int] = field(default_factory=dict)
    merge_results: dict[int, tuple[bool, str]] = field(default_factory=dict)
    create_calls: list[tuple[str, dict]] = field(default_factory=list)
    merge_calls: list[tuple[str, int, str]] = field(default_factory=list)
    next_pr: int = 100

    def create_pr(self, repo: str, *, base: str, head: str, title: str, body: str) -> dict:
        self.create_calls.append((repo, {"base": base, "head": head, "title": title}))
        pr_num = self.next_pr
        self.next_pr += 1
        return {"number": pr_num, "url": f"https://gh/x/{pr_num}", "existed": False}

    def get_pr_size(self, repo: str, number: int) -> int:
        return self.sizes.get(number, 100)

    def merge_pr(self, repo: str, number: int, method: str = "rebase") -> tuple[bool, str]:
        self.merge_calls.append((repo, number, method))
        return self.merge_results.get(number, (True, "merged"))


class TestProcess:
    def test_opens_pr_sizes_and_merges_in_size_order(self) -> None:
        items = [_q("big"), _q("small"), _q("mid")]
        gh = FakeGh(sizes={100: 500, 101: 10, 102: 100})
        events = process(items, gh)

        # PRs opened in original order
        opened = [e.entry.assignment_id for e in events if e.kind == "opened"]
        assert opened == ["big", "small", "mid"]
        # Merge order driven by size: small (101) → mid (102) → big (100)
        merge_seq = [c[1] for c in gh.merge_calls]
        assert merge_seq == [101, 102, 100]
        # All entries left in MERGED state
        assert {x.state for x in items} == {MERGED}

    def test_conflict_halts_that_repo_group_only(self) -> None:
        items = [
            _q("a", size=10),
            _q("b", size=20),
            _q("other", repo="ui", repo_github="acme/ui", size=5),
        ]
        # Pre-assign PR numbers so we know which to fail.
        gh = FakeGh(
            sizes={100: 10, 101: 20, 102: 5},
            merge_results={100: (False, "Merge conflict")},
        )
        events = process(items, gh)
        # `a` conflicts, `b` is in same group → not merged
        states = {x.assignment_id: x.state for x in items}
        assert states["a"] == CONFLICT
        assert states["b"] == PENDING
        # Different repo group still processes
        assert states["other"] == MERGED
        # And we surfaced a conflict event
        kinds = [e.kind for e in events]
        assert "conflict" in kinds

    def test_dry_run_no_gh_calls(self) -> None:
        items = [_q("a"), _q("b")]
        gh = FakeGh()
        events = process(items, gh, dry_run=True)
        assert gh.create_calls == []
        assert gh.merge_calls == []
        assert all(e.kind in ("opened", "merged") for e in events)
        # State untouched in dry-run
        assert all(x.state == PENDING for x in items)

    def test_skips_terminal_entries(self) -> None:
        items = [
            _q("done", state=MERGED, pr=1),
            _q("pending", size=10),
        ]
        gh = FakeGh()
        process(items, gh)
        # No second call for the already-merged entry
        assert all(c[1] != 1 for c in gh.merge_calls)


class TestReviewGate:
    """#253: process() must refuse to merge when reviews are required and
    no approved review is on the board.

    Reproduces the symptom from quadraui#233: a PR was opened and merged in
    the same `coord merge` invocation, in 2 seconds, with no review.  These
    tests cover the regression for both the legacy code path (no config/board
    passed → gate skipped) and the new code path (config+board passed → gate
    fires).
    """

    @staticmethod
    def _config(*, enabled: bool = True, gates: list[str] | None = None):
        """Build a minimal config-like object with the fields the gate reads."""
        from dataclasses import dataclass
        @dataclass
        class _Reviews:
            enabled: bool = True
        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None
        @dataclass
        class _Cfg:
            reviews: _Reviews = field(default_factory=_Reviews)
            pipeline: _Pipeline = field(default_factory=_Pipeline)
        cfg = _Cfg()
        cfg.reviews.enabled = enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["review", "merge"]
        return cfg

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1") -> Assignment:
        return Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=aid,
            type="work",
            status="done",
            branch=f"worker/{aid}",
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str | None = "approve", status: str = "done") -> Assignment:
        return Assignment(
            machine_name="m2",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=f"rev-{of_aid}",
            type="review",
            status=status,
            review_of_assignment_id=of_aid,
            review_verdict=verdict,
        )

    def test_requires_review_helper_honours_config(self) -> None:
        cfg = self._config(enabled=True, gates=["review", "merge"])
        assert mq.requires_review(_q("a"), cfg) is True
        cfg_off = self._config(enabled=False)
        assert mq.requires_review(_q("a"), cfg_off) is False
        cfg_no_gate = self._config(enabled=True, gates=["merge"])
        assert mq.requires_review(_q("a"), cfg_no_gate) is False

    def test_has_approved_review_finds_matching_review(self) -> None:
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        board = self._board(completed=[work, review])
        assert mq.has_approved_review(_q("w1"), board) is True

    def test_has_approved_review_rejects_request_changes(self) -> None:
        work = self._work("w1")
        review = self._review("w1", verdict="request-changes")
        board = self._board(completed=[work, review])
        assert mq.has_approved_review(_q("w1"), board) is False

    def test_has_approved_review_ignores_unrelated_reviews(self) -> None:
        work = self._work("w1")
        # Approved review but for a different work assignment
        review = self._review("w99", verdict="approve")
        board = self._board(completed=[work, review])
        assert mq.has_approved_review(_q("w1"), board) is False

    def test_process_emits_review_required_event_and_halts_merge(self) -> None:
        """The smoking-gun #233 regression: no review on board → no merge_pr call."""
        cfg = self._config()
        board = self._board(completed=[self._work("w1")])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        # PR is opened (so the user can inspect) but merge_pr is never called.
        kinds = [e.kind for e in events]
        assert "opened" in kinds
        assert "review_required" in kinds
        assert "merged" not in kinds
        assert gh.merge_calls == []
        # Item remains PENDING with an error so the TUI can surface it.
        assert items[0].state == PENDING
        assert items[0].error == "review required but not approved"

    def test_process_proceeds_when_review_is_approved(self) -> None:
        cfg = self._config()
        board = self._board(completed=[
            self._work("w1"),
            self._review("w1", verdict="approve"),
        ])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        assert any(e.kind == "merged" for e in events)
        assert gh.merge_calls and gh.merge_calls[0][1] == 100  # the opened PR
        assert items[0].state == MERGED

    def test_skip_review_bypasses_gate(self) -> None:
        """--skip-review must let a no-review merge proceed."""
        cfg = self._config()
        board = self._board(completed=[self._work("w1")])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, skip_review=True)

        kinds = [e.kind for e in events]
        assert "review_required" not in kinds
        assert "merged" in kinds
        assert items[0].state == MERGED

    def test_reviews_disabled_bypasses_gate(self) -> None:
        cfg = self._config(enabled=False)
        board = self._board(completed=[self._work("w1")])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "review_required" not in kinds
        assert "merged" in kinds

    def test_legacy_callers_without_config_unaffected(self) -> None:
        """Callers that don't pass config/board still work (no surprise breakage)."""
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh)
        assert any(e.kind == "merged" for e in events)

    # ── #292 Defect 1: has_approved_review with bounce ────────────────────

    def test_has_approved_review_bounce_fix_approves(self) -> None:
        """#292: approval on fix-work is found even when entry is keyed to orig-work."""
        orig_work = self._work("orig")
        fix_work = Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="[fix-1] t",
            assignment_id="fix1",
            type="work",
            status="done",
            # Same branch as orig_work
            branch="worker/orig",
        )
        # Review that approved the fix work (not the original)
        re_review = self._review("fix1", verdict="approve")
        # Original review requested changes
        orig_review = self._review("orig", verdict="request-changes")
        board = self._board(completed=[orig_work, orig_review, fix_work, re_review])
        # Entry keyed to orig-work (as it would be after the first coord merge)
        entry = _q("orig", branch="worker/orig")
        assert mq.has_approved_review(entry, board) is True

    def test_has_approved_review_bounce_no_approve_yet(self) -> None:
        """#292: if no approval at all across the branch, still returns False."""
        orig_work = self._work("orig")
        fix_work = Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="[fix-1] t",
            assignment_id="fix1",
            type="work",
            status="done",
            branch="worker/orig",
        )
        orig_review = self._review("orig", verdict="request-changes")
        fix_review = self._review("fix1", verdict="request-changes")
        board = self._board(completed=[orig_work, orig_review, fix_work, fix_review])
        entry = _q("orig", branch="worker/orig")
        assert mq.has_approved_review(entry, board) is False

    # ── #292 Defect 3: skip-and-proceed instead of group-halt ────────────

    def test_process_review_gated_entry_does_not_block_approved_sibling(self) -> None:
        """#292: an un-reviewed entry should not block an approved sibling."""
        cfg = self._config()
        approved_work = self._work("approved")
        approved_review = self._review("approved", verdict="approve")
        board = self._board(completed=[
            self._work("ungated"),  # no review
            approved_work,
            approved_review,
        ])
        # Two entries in the same (repo, target) group
        items = [
            _q("ungated", size=10),
            _q("approved", size=20),
        ]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        # ungated entry is blocked
        assert "review_required" in kinds
        # approved entry still merges
        assert "merged" in kinds
        # Both PRC opened
        assert len(gh.create_calls) == 2
        states = {x.assignment_id: x.state for x in items}
        assert states["ungated"] == PENDING
        assert states["approved"] == MERGED

    def test_process_review_gated_entry_does_not_block_first_entry_if_second_approved(self) -> None:
        """#292: approved entry merges even when it is sequenced AFTER a blocked one."""
        cfg = self._config()
        board = self._board(completed=[
            self._work("blocked"),  # no review
            self._work("approved"),
            self._review("approved", verdict="approve"),
        ])
        # Explicit ordering: blocked first, approved second
        items = [_q("blocked", size=5), _q("approved", size=50)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, presorted=True)

        kinds = [e.kind for e in events]
        assert "review_required" in kinds
        assert "merged" in kinds
        states = {x.assignment_id: x.state for x in items}
        assert states["blocked"] == PENDING
        assert states["approved"] == MERGED

    # ── #292 Defect 4: dry-run applies the review gate ────────────────────

    def test_dry_run_shows_review_required_for_unapproved(self) -> None:
        """#292: dry-run must surface review_required, not 'would merge'."""
        cfg = self._config()
        board = self._board(completed=[self._work("w1")])  # no approval
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "review_required" in kinds
        assert "merged" not in kinds
        # dry-run never touches state
        assert items[0].state == PENDING

    def test_dry_run_shows_merged_for_approved(self) -> None:
        """#292: dry-run with a real approval → would-merge event."""
        cfg = self._config()
        board = self._board(completed=[
            self._work("w1"),
            self._review("w1", verdict="approve"),
        ])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "merged" in kinds
        assert "review_required" not in kinds
        assert items[0].state == PENDING  # dry-run: state untouched


class TestSmokeGate:
    """#465: process() must refuse to merge when interactive smoke is required
    and no passing/skipped verdict is recorded on the work assignment.

    The smoke gate is the second gate (after review, before CI).  It mirrors
    the review gate in structure: skip-not-halt, same legacy-caller semantics,
    dry-run applies it.
    """

    @staticmethod
    def _config(*, gates: list[str] | None = None):
        """Build a minimal config-like object that includes the smoke gate."""
        from dataclasses import dataclass, field as dc_field
        @dataclass
        class _Reviews:
            enabled: bool = False  # review gate off by default in smoke tests
        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None
        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
        cfg = _Cfg()
        cfg.pipeline.default_gates = gates if gates is not None else ["test", "merge"]
        return cfg

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1", *, test_state: str | None = None) -> Assignment:
        return Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=aid,
            type="work",
            status="done",
            branch=f"worker/{aid}",
            test_state=test_state,
        )

    # ── requires_smoke / has_smoke_verdict helpers ──

    def test_requires_smoke_honours_config(self) -> None:
        cfg_with = self._config(gates=["test", "merge"])
        assert mq.requires_smoke(_q("a"), cfg_with) is True

    def test_requires_smoke_false_when_test_not_in_gates(self) -> None:
        cfg_without = self._config(gates=["review", "merge"])
        assert mq.requires_smoke(_q("a"), cfg_without) is False

    def test_requires_smoke_false_when_no_pipeline(self) -> None:
        from dataclasses import dataclass
        @dataclass
        class _NoPipelineCfg:
            pass
        assert mq.requires_smoke(_q("a"), _NoPipelineCfg()) is False

    def test_has_smoke_verdict_passed(self) -> None:
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])
        assert mq.has_smoke_verdict(_q("w1"), board) is True

    def test_has_smoke_verdict_skipped(self) -> None:
        work = self._work("w1", test_state="skipped")
        board = self._board(completed=[work])
        assert mq.has_smoke_verdict(_q("w1"), board) is True

    def test_has_smoke_verdict_none_returns_false(self) -> None:
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        assert mq.has_smoke_verdict(_q("w1"), board) is False

    def test_has_smoke_verdict_failed_returns_false(self) -> None:
        work = self._work("w1", test_state="failed")
        board = self._board(completed=[work])
        assert mq.has_smoke_verdict(_q("w1"), board) is False

    def test_has_smoke_verdict_no_matching_work_fails_open(self) -> None:
        """When no work assignment for the branch is found on the board, the
        gate fails open (returns True) — can't block without evidence."""
        # Work on a different branch — does not count for entry w1.
        unrelated = Assignment(
            machine_name="m1", repo_name="api", issue_number=2, issue_title="t",
            assignment_id="w99", type="work", status="done",
            branch="worker/w99", test_state="passed",
        )
        board = self._board(completed=[unrelated])
        # No work for "w1"'s branch on the board → fail open.
        assert mq.has_smoke_verdict(_q("w1"), board) is True

    def test_has_smoke_verdict_empty_board_fails_open(self) -> None:
        """Empty board → fail open."""
        board = self._board()
        assert mq.has_smoke_verdict(_q("w1"), board) is True

    def test_has_smoke_verdict_bounce_fix_counts(self) -> None:
        """Fix-work on the same branch with a passing test_state satisfies the gate."""
        orig_work = self._work("orig", test_state=None)
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix] t",
            assignment_id="fix1", type="work", status="done",
            branch="worker/orig",  # same branch as orig_work
            test_state="passed",
        )
        board = self._board(completed=[orig_work, fix_work])
        entry = _q("orig", branch="worker/orig")
        assert mq.has_smoke_verdict(entry, board) is True

    # ── process() smoke gate ──

    def test_process_emits_smoke_required_when_no_verdict(self) -> None:
        """No smoke verdict → PR is opened but merge is blocked."""
        cfg = self._config()
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "opened" in kinds
        assert "smoke_required" in kinds
        assert "merged" not in kinds
        assert gh.merge_calls == []
        assert items[0].state == PENDING
        assert items[0].error == "smoke test required but no verdict recorded"

    def test_process_proceeds_when_smoke_passed(self) -> None:
        """Smoke passed → merge proceeds (no smoke_required event)."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        assert any(e.kind == "merged" for e in events)
        assert not any(e.kind == "smoke_required" for e in events)
        assert items[0].state == MERGED

    def test_process_proceeds_when_smoke_skipped(self) -> None:
        """Smoke skipped → merge proceeds."""
        cfg = self._config()
        work = self._work("w1", test_state="skipped")
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        assert any(e.kind == "merged" for e in events)
        assert items[0].state == MERGED

    def test_process_skip_smoke_bypasses_gate(self) -> None:
        """--skip-smoke must let a no-verdict merge proceed."""
        cfg = self._config()
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, skip_smoke=True)

        kinds = [e.kind for e in events]
        assert "smoke_required" not in kinds
        assert "merged" in kinds
        assert items[0].state == MERGED

    def test_process_smoke_gate_off_when_test_not_in_gates(self) -> None:
        """When 'test' is not in default_gates the smoke gate is disabled."""
        cfg = self._config(gates=["review", "merge"])  # no "test"
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "smoke_required" not in kinds
        assert "merged" in kinds

    def test_process_legacy_callers_without_config_unaffected(self) -> None:
        """Legacy callers that don't pass config/board still work."""
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh)
        assert any(e.kind == "merged" for e in events)

    def test_process_smoke_gate_does_not_block_sibling(self) -> None:
        """An unsmoked entry must not halt the group — its sibling with a
        verdict should still merge."""
        cfg = self._config()
        unsmoked = self._work("unsmoked", test_state=None)
        smoked = self._work("smoked", test_state="passed")
        board = self._board(completed=[unsmoked, smoked])
        items = [
            _q("unsmoked", branch="worker/unsmoked", size=10),
            _q("smoked", branch="worker/smoked", size=20),
        ]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds
        assert "merged" in kinds
        states = {x.assignment_id: x.state for x in items}
        assert states["unsmoked"] == PENDING
        assert states["smoked"] == MERGED

    def test_dry_run_shows_smoke_required_for_no_verdict(self) -> None:
        """dry-run must surface smoke_required, not 'would merge'."""
        cfg = self._config()
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds
        assert "merged" not in kinds
        assert items[0].state == PENDING  # dry-run never mutates state

    def test_dry_run_shows_merged_for_passed_smoke(self) -> None:
        """dry-run with passed smoke verdict → would-merge event."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh, config=cfg, board=board, dry_run=True)

        kinds = [e.kind for e in events]
        assert "merged" in kinds
        assert "smoke_required" not in kinds
        assert items[0].state == PENDING  # dry-run: state untouched


class TestRefreshEntryAssignment:
    """#292: refresh_entry_assignment creates or updates queue entries."""

    def _work(self, aid: str, branch: str = "worker/orig") -> Assignment:
        return Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=aid,
            type="work",
            status="done",
            branch=branch,
        )

    def test_creates_entry_when_none_exists(self, coord_db) -> None:
        work = self._work("fix1")
        result = mq.refresh_entry_assignment(work, repo_github="acme/api", target_branch="main")
        assert result is True
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "fix1"

    def test_updates_assignment_id_for_existing_pending_entry(self, coord_db) -> None:
        # Seed with orig-work keyed entry
        orig = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", branch="worker/orig", status="done",
        )
        mq.enqueue(orig, repo_github="acme/api", target_branch="main")
        assert load_queue()[0].assignment_id == "orig"

        fix = self._work("fix1", branch="worker/orig")
        result = mq.refresh_entry_assignment(fix, repo_github="acme/api", target_branch="main")
        assert result is True
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "fix1"

    def test_clears_stale_review_error(self, coord_db) -> None:
        orig = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", branch="worker/orig", status="done",
        )
        mq.enqueue(orig, repo_github="acme/api", target_branch="main")
        items = load_queue()
        items[0].error = "review required but not approved"
        mq.save_queue(items)

        fix = self._work("fix1", branch="worker/orig")
        mq.refresh_entry_assignment(fix, repo_github="acme/api", target_branch="main")
        assert load_queue()[0].error is None

    def test_no_change_when_assignment_id_already_correct(self, coord_db) -> None:
        work = self._work("fix1")
        mq.enqueue(work, repo_github="acme/api", target_branch="main")
        result = mq.refresh_entry_assignment(work, repo_github="acme/api", target_branch="main")
        assert result is False  # no change

    def test_does_not_touch_merged_entry(self, coord_db) -> None:
        orig = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", branch="worker/orig", status="done",
        )
        mq.enqueue(orig, repo_github="acme/api", target_branch="main")
        items = load_queue()
        items[0].state = mq.MERGED
        mq.save_queue(items)

        fix = self._work("fix1", branch="worker/orig")
        result = mq.refresh_entry_assignment(fix, repo_github="acme/api", target_branch="main")
        assert result is False
        assert load_queue()[0].assignment_id == "orig"  # untouched

    def test_noop_when_no_branch(self, coord_db) -> None:
        work = self._work("fix1", branch="")
        work.branch = None  # type: ignore[assignment]
        result = mq.refresh_entry_assignment(work, repo_github="acme/api", target_branch="main")
        assert result is False
        assert load_queue() == []


class TestPendingSummary:
    def test_groups_by_repo_excludes_terminal(self) -> None:
        items = [
            _q("a", repo="api"),
            _q("b", repo="api", state=MERGED),
            _q("c", repo="ui", state=CONFLICT),
        ]
        s = pending_summary(items)
        assert set(s.keys()) == {"api", "ui"}
        assert [x.assignment_id for x in s["api"]] == ["a"]
        assert [x.assignment_id for x in s["ui"]] == ["c"]
