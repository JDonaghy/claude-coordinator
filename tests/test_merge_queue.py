"""Tests for coord.merge_queue — sequencing logic and the gh-driven processor."""

from __future__ import annotations

import json
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
    assignment_type: str = "work",
    required_gates: list[str] | None = None,
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
        assignment_type=assignment_type,
        required_gates=required_gates if required_gates is not None else [],
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

    def test_roundtrip_preserves_assignment_type(self, coord_db) -> None:
        # #1077: assignment_type must survive a save/load cycle so the merge
        # processor can still tell a mock-author entry apart after a daemon
        # restart re-reads the queue from disk.
        save_queue([_q("a", assignment_type="mock-author"), _q("b")])
        again = {x.assignment_id: x.assignment_type for x in load_queue()}
        assert again == {"a": "mock-author", "b": "work"}

    def test_roundtrip_preserves_required_gates(self, coord_db) -> None:
        # #1213: a label-resolved gate list must survive a save/load cycle
        # so the merge gate stays commit-bound after a daemon restart.
        save_queue([_q("a", required_gates=["merge"]), _q("b")])
        again = {x.assignment_id: x.required_gates for x in load_queue()}
        assert again == {"a": ["merge"], "b": []}


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

    def test_enqueue_carries_assignment_type(self, coord_db) -> None:
        # #1077: the queued entry must remember the originating assignment's
        # type so `process()` can decide whether merging closes the issue.
        a = Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ga", branch="worker/ga", status="done",
            type="mock-author",
        )
        entry = enqueue(a, repo_github="acme/api", target_branch="main")
        assert entry is not None
        assert entry.assignment_type == "mock-author"
        assert load_queue()[0].assignment_type == "mock-author"

    def test_enqueue_snapshots_required_gates(self, coord_db) -> None:
        # #1213: a label-resolved gate list on the assignment must be
        # snapshotted onto the queue entry at enqueue time (commit-bound).
        a = Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ga", branch="worker/ga", status="done",
            required_gates=["merge"],
        )
        entry = enqueue(a, repo_github="acme/api", target_branch="main")
        assert entry is not None
        assert entry.required_gates == ["merge"]
        assert load_queue()[0].required_gates == ["merge"]

    def test_enqueue_untagged_work_gets_empty_required_gates(self, coord_db) -> None:
        # Untagged work (no label override) must snapshot [] — the fallback
        # sentinel — not None, so requires_review/requires_smoke fall back to
        # config.pipeline.default_gates unchanged (#1213 compatibility contract).
        entry = enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        assert entry is not None
        assert entry.required_gates == []

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
    close_calls: list[tuple[str, int]] = field(default_factory=list)
    close_raises: bool = False
    next_pr: int = 100
    # #1196 hole 2 (PR-body lint): PR number -> body text; issue number ->
    # whether it currently has open children. Defaults keep every prior
    # test (none of which set these) inert — get_pr_body returns "" so
    # `process()`'s lint step is a no-op, matching pre-#1196 behavior.
    pr_bodies: dict[int, str] = field(default_factory=dict)
    open_children: set[int] = field(default_factory=set)
    edit_body_calls: list[tuple[str, int, str]] = field(default_factory=list)

    def create_pr(self, repo: str, *, base: str, head: str, title: str, body: str) -> dict:
        self.create_calls.append((repo, {"base": base, "head": head, "title": title}))
        pr_num = self.next_pr
        self.next_pr += 1
        self.pr_bodies.setdefault(pr_num, body)
        return {"number": pr_num, "url": f"https://gh/x/{pr_num}", "existed": False}

    def get_pr_size(self, repo: str, number: int) -> int:
        return self.sizes.get(number, 100)

    def merge_pr(self, repo: str, number: int, method: str = "rebase") -> tuple[bool, str]:
        self.merge_calls.append((repo, number, method))
        return self.merge_results.get(number, (True, "merged"))

    def close_issue(self, repo: str, issue_number: int) -> None:
        self.close_calls.append((repo, issue_number))
        if self.close_raises:
            raise RuntimeError("gh issue close failed")

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        # Tests don't exercise SHA tracking by default; return None so the
        # backward-compatible "no SHA → skip staleness check" path runs.
        return None

    def get_pr_body(self, repo: str, number: int) -> str:
        return self.pr_bodies.get(number, "")

    def edit_pr_body(self, repo: str, number: int, body: str) -> None:
        self.edit_body_calls.append((repo, number, body))
        self.pr_bodies[number] = body

    def has_open_children(self, repo: str, issue_number: int) -> bool:
        return issue_number in self.open_children


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

    def test_closes_linked_issue_on_merge(self) -> None:
        # #806: a successful merge must close the linked issue deterministically,
        # not rely on the worker having put `Closes #N` in the PR body.
        items = [_q("a")]
        process(items, gh := FakeGh())
        assert items[0].state == MERGED
        assert gh.close_calls == [(items[0].repo_github, items[0].issue_number)]

    def test_close_failure_does_not_revert_merge(self) -> None:
        # #806: closing is best-effort — a `gh issue close` failure must leave
        # the merge standing and surface a warning, never undo MERGED.
        items = [_q("a")]
        events = process(items, FakeGh(close_raises=True))
        assert items[0].state == MERGED
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "could not close" in merged[0].message

    def test_dry_run_does_not_close(self) -> None:
        # #806: dry-run never reaches the real merge path, so no issue is closed.
        items = [_q("a")]
        process(items, gh := FakeGh(), dry_run=True)
        assert gh.close_calls == []

    def test_mock_author_merge_does_not_close_tracking_issue(self) -> None:
        # #1077: a "mock-author" (Gate A) entry's issue_number is the
        # milestone's tracking issue, not something the PR resolves — merging
        # it must NOT close that issue, unlike a "work" entry (#806 above).
        items = [_q("a", assignment_type="mock-author")]
        events = process(items, gh := FakeGh())
        assert items[0].state == MERGED
        assert gh.close_calls == []
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "left open" in merged[0].message

    def test_briefing_body_uses_refs_for_mock_author(self) -> None:
        # #1077: the fallback create_pr body (when no PR was opened upstream)
        # must use the non-closing "Refs #N" for mock-author entries.
        from coord.merge_queue import _briefing_body

        entry = _q("a", assignment_type="mock-author")
        body = _briefing_body(entry)
        assert "Refs #1" in body
        assert "Closes #1" not in body

    def test_briefing_body_uses_closes_for_work(self) -> None:
        # #1077: "work" entries keep the #806 closing-keyword behavior.
        from coord.merge_queue import _briefing_body

        entry = _q("a", assignment_type="work")
        body = _briefing_body(entry)
        assert body.startswith("Closes #1\n\n")

    def test_conflict_does_not_halt_other_repo_groups(self) -> None:
        """A conflict in one (repo, target) group must not touch other groups."""
        items = [
            _q("a", size=10),
            _q("other", repo="ui", repo_github="acme/ui", size=5),
        ]
        gh = FakeGh(
            sizes={100: 10, 101: 5},
            merge_results={100: (False, "Merge conflict")},
        )
        events = process(items, gh)
        states = {x.assignment_id: x.state for x in items}
        assert states["a"] == CONFLICT
        # Different repo group still processes
        assert states["other"] == MERGED
        kinds = [e.kind for e in events]
        assert "conflict" in kinds

    def test_conflict_parks_entry_and_sibling_still_merges(self) -> None:
        """#735: a conflicting entry is parked (CONFLICT) and siblings in the
        same (repo, target) group continue to merge — no group-wide halt."""
        items = [
            _q("a", size=10),
            _q("b", size=20),
        ]
        # PR 100 → `a` (first opened), PR 101 → `b`
        gh = FakeGh(
            sizes={100: 10, 101: 20},
            merge_results={100: (False, "Merge conflict")},
        )
        events = process(items, gh, presorted=True)
        states = {x.assignment_id: x.state for x in items}
        # Conflicting entry is parked
        assert states["a"] == CONFLICT
        # Sibling in the same group still merges (#735)
        assert states["b"] == MERGED
        kinds = [e.kind for e in events]
        assert "conflict" in kinds
        assert "merged" in kinds

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

    # ── #1196 hole 2: pre-merge PR-body closing-keyword lint ──────────────

    def test_downgrades_worker_pr_body_closes_for_epic_with_open_children(self) -> None:
        # GitHub's own closing-keyword magic reads the PR body directly at
        # merge time and never calls github_ops.close_issue — the only
        # place that can stop it is a pre-merge scan/rewrite.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(pr_bodies={100: "Closes #1041\n\nWorker-authored PR."}, open_children={1041})
        events = process(items, gh)
        assert items[0].state == MERGED
        assert gh.edit_body_calls == [
            ("acme/api", 100, "Refs #1041\n\nWorker-authored PR.")
        ]
        downgraded = [e for e in events if e.kind == "pr_body_downgraded"]
        assert downgraded and "#1041" in downgraded[0].message

    def test_leaves_regular_pr_body_untouched(self) -> None:
        # No regression for the common case: a PR body closing a regular
        # (childless) issue is never rewritten.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(pr_bodies={100: "Closes #55"}, open_children=set())
        process(items, gh)
        assert gh.edit_body_calls == []

    def test_lint_ignores_pr_body_with_no_closing_keyword(self) -> None:
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(pr_bodies={100: "Refs #1041, unrelated context."}, open_children={1041})
        process(items, gh)
        assert gh.edit_body_calls == []

    def test_lint_failure_never_blocks_the_merge(self) -> None:
        # Best-effort throughout: a get_pr_body/has_open_children/
        # edit_pr_body failure must not prevent (or revert) a merge.
        class _BoomOnBody(FakeGh):
            def get_pr_body(self, repo: str, number: int) -> str:
                raise RuntimeError("gh pr view failed")

        items = [_q("a", pr=100, size=10)]
        gh = _BoomOnBody()
        process(items, gh)
        assert items[0].state == MERGED


class TestProcessRealGithubOpsChokepoint:
    """#1196 acceptance criterion: 'Dispatching type="work" against an epic
    with an open child and merging it leaves the epic OPEN' — driven through
    the REAL `coord.github_ops` module wired in as `gh_ops` (only the `gh`
    subprocess boundary is faked), not `FakeGh`'s `close_raises` stand-in.
    This exercises the actual #1196 chokepoint end to end: both hole 1 (a
    "work" assignment whose issue_number IS the epic) and hole 2 (the PR
    body's own `Closes #<epic>` keyword) in one pass.
    """

    def test_type_work_direct_on_epic_with_open_child_stays_open(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from coord import github_ops as real_gh_ops

        epic_json = json.dumps({
            "number": 1041, "title": "Epic", "state": "open", "milestone": None,
            "labels": [], "body": "## Sub-issues\n- [ ] #1039\n- [x] #1040\n",
        })

        def fake_gh(*args: str) -> str:
            if args[:2] == ("pr", "list"):
                return "[]"
            if args[:2] == ("pr", "create"):
                return "https://github.com/acme/api/pull/500"
            if args[:2] == ("pr", "view"):
                return json.dumps({
                    "body": "Closes #1041\n\nAutomated merge from the coordinator."
                })
            if args[:2] == ("issue", "view"):
                return epic_json
            if args[:2] == ("pr", "edit"):
                return ""
            if args[:2] == ("pr", "merge"):
                return "merged"
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(real_gh_ops, "_gh", fake_gh)

        def _boom_subprocess(*a, **k):
            raise AssertionError(
                "must never reach the real `gh issue close` subprocess call "
                "— the epic has an open child"
            )

        monkeypatch.setattr(real_gh_ops.subprocess, "run", _boom_subprocess)

        entry = _q("w1", repo="api", repo_github="acme/api", target="main", size=10)
        entry.issue_number = 1041  # #1196 hole 1: the epic itself, type="work"

        events = process([entry], real_gh_ops)

        # Merge succeeded — the PR itself lands.
        assert entry.state == MERGED
        # But the epic was never closed: the chokepoint's guard refused.
        merged_events = [e for e in events if e.kind == "merged"]
        assert merged_events
        assert "could not close" in merged_events[0].message
        assert "open children" in merged_events[0].message.lower()
        assert "#1039" in merged_events[0].message
        # Hole 2: the PR body's own `Closes #1041` was downgraded pre-merge.
        downgrade_events = [e for e in events if e.kind == "pr_body_downgraded"]
        assert downgrade_events and "#1041" in downgrade_events[0].message


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

    def test_requires_review_entry_override_bypasses_default(self) -> None:
        # #1213: an entry whose snapshotted required_gates drops "review"
        # bypasses the gate even though the default policy requires it.
        cfg = self._config(enabled=True, gates=["review", "merge"])
        entry = _q("a", required_gates=["merge"])
        assert mq.requires_review(entry, cfg) is False

    def test_requires_review_entry_override_can_also_require_it(self) -> None:
        # An override that keeps "review" still gates, same as default.
        cfg = self._config(enabled=True, gates=["merge"])
        entry = _q("a", required_gates=["review", "merge"])
        assert mq.requires_review(entry, cfg) is True

    def test_requires_review_empty_entry_gates_falls_back_to_default(self) -> None:
        # #1213 compatibility contract: untagged work (entry.required_gates
        # empty/absent) must behave exactly as before — default policy wins.
        cfg = self._config(enabled=True, gates=["review", "merge"])
        assert mq.requires_review(_q("a", required_gates=[]), cfg) is True
        cfg_without = self._config(enabled=True, gates=["merge"])
        assert mq.requires_review(_q("a", required_gates=[]), cfg_without) is False

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
        """Callers that don't pass config/board still work (no surprise breakage).

        When config is None, requires_review() returns False so no gate fires.
        The fail-closed rule (#821) only applies when config is present and
        confirms review is required but board is absent.
        """
        items = [_q("w1", size=10)]
        gh = FakeGh()
        events = process(items, gh)
        assert any(e.kind == "merged" for e in events)

    # ── #821: fail-closed gates ───────────────────────────────────────────

    def test_process_fails_closed_when_board_none_and_review_required(self) -> None:
        """#821: process() with board=None must block a review-required entry."""
        cfg = self._config()  # reviews.enabled=True, gate includes "review"
        items = [_q("w1", size=10)]
        gh = FakeGh()
        # No board → cannot confirm review approval → fail closed.
        events = process(items, gh, config=cfg, board=None)

        kinds = [e.kind for e in events]
        assert "review_required" in kinds, "gate must fire when board is None"
        assert "merged" not in kinds, "merge must not proceed without confirmed review"
        assert items[0].state == PENDING
        assert items[0].error is not None

    def test_process_fails_closed_when_board_none_and_smoke_required(self) -> None:
        """#821: process() with board=None must block a smoke-required entry."""
        from dataclasses import dataclass as _dc, field as _dc_field

        @_dc
        class _Reviews:
            enabled: bool = False  # review gate off

        @_dc
        class _Pipeline:
            default_gates: list | None = None

        @_dc
        class _SmokeConfig:
            reviews: _Reviews = _dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = _dc_field(default_factory=_Pipeline)

        cfg = _SmokeConfig()
        cfg.pipeline.default_gates = ["test", "merge"]  # smoke gate on, review off
        items = [_q("w1", size=10)]
        gh = FakeGh()
        # No board → cannot confirm smoke verdict → fail closed.
        events = process(items, gh, config=cfg, board=None)

        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds, "smoke gate must fire when board is None"
        assert "merged" not in kinds
        assert items[0].state == PENDING, "blocked entry must remain PENDING"
        assert items[0].error is not None, "blocked entry must carry an error message"

    def test_process_fail_closed_board_none_skip_review_still_merges(self) -> None:
        """#821: explicit skip_review=True can still bypass the gate for local overrides."""
        cfg = self._config()
        items = [_q("w1", size=10)]
        gh = FakeGh()
        # skip_review=True is the explicit local override; must still work.
        events = process(items, gh, config=cfg, board=None, skip_review=True)

        kinds = [e.kind for e in events]
        assert "review_required" not in kinds
        assert "merged" in kinds

    # ── #821: commit-bound approval — production population ──────────────

    def test_process_populates_branch_head_sha_from_gh_ops(self) -> None:
        """#821: process() must populate entry.branch_head_sha via gh_ops.get_branch_sha.

        This verifies the *production population* path — that get_branch_sha is
        actually called (not just that has_approved_review checks the value).
        """
        from dataclasses import dataclass as _dc, field as _dc_field

        sha_calls: list[tuple[str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                sha_calls.append((repo, branch))
                return "cafebabe"

        cfg = self._config()
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "cafebabe"  # matches what _TrackingGh returns
        board = self._board(completed=[work, review])

        items = [_q("w1", size=10)]
        process(items, _TrackingGh(), config=cfg, board=board)

        # get_branch_sha must have been called for the entry.
        assert len(sha_calls) >= 1, "process() must call gh_ops.get_branch_sha"
        assert sha_calls[0][1] == items[0].branch, "must fetch SHA for the entry's branch"
        # The field must be populated on the entry.
        assert items[0].branch_head_sha == "cafebabe"

    def test_process_stale_sha_blocks_merge_end_to_end(self) -> None:
        """#821: end-to-end — review at old SHA + branch moved → process blocks merge."""
        cfg = self._config()
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "oldsha"  # review was at this commit

        class _MovedBranchGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                return "newsha"  # branch has new commits since review

        board = self._board(completed=[work, review])
        items = [_q("w1", size=10)]
        events = process(items, _MovedBranchGh(), config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "merged" not in kinds, "stale approval must not allow merge"
        assert "review_required" in kinds, "stale approval must re-block the review gate"

    # ── #821: commit-bound approval ───────────────────────────────────────

    def test_has_approved_review_stale_sha_blocks(self) -> None:
        """#821: an approval covering a different commit SHA is rejected."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"  # SHA when review was done

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"  # branch moved since review

        board = self._board(completed=[work, review])
        # Review SHA != branch SHA → stale approval → must return False.
        assert mq.has_approved_review(entry, board) is False

    def test_has_approved_review_matching_sha_passes(self) -> None:
        """#821: an approval at the same commit SHA is accepted."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "abc123"  # same SHA as review

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is True

    def test_has_approved_review_no_sha_skips_commit_check(self) -> None:
        """#821: when SHAs are absent, the commit check is skipped (backward compat)."""
        work = self._work("w1")
        # review_head_sha unset (pre-821 row)
        review = self._review("w1", verdict="approve")

        entry = _q("w1", branch="worker/w1")
        # branch_head_sha also unset

        board = self._board(completed=[work, review])
        # No SHAs → skip the commit check → approval still valid.
        assert mq.has_approved_review(entry, board) is True

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


class TestPassesMergeGates:
    """#946: passes_merge_gates() is the shared predicate composing the
    review + smoke gates, used by every enqueue path (enqueue_approved_work,
    the `coord merge` auto-enqueue loop, and enqueue()) so none of them can
    drift out of sync with the others."""

    @staticmethod
    def _config(*, reviews_enabled: bool = True, gates: list[str] | None = None):
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class _Reviews:
            enabled: bool = True

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)

        cfg = _Cfg()
        cfg.reviews.enabled = reviews_enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["test", "review", "merge"]
        return cfg

    @staticmethod
    def _board(active=None, completed=None):
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

    @staticmethod
    def _review(of_aid: str, *, verdict: str | None = "approve") -> Assignment:
        return Assignment(
            machine_name="m2",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            assignment_id=f"rev-{of_aid}",
            type="review",
            status="done",
            review_of_assignment_id=of_aid,
            review_verdict=verdict,
        )

    def test_refused_on_failed_test_and_no_review(self) -> None:
        """#782 repro: failed test, no review → gate refuses."""
        cfg = self._config()
        work = self._work("w1", test_state="failed")
        board = self._board(completed=[work])
        assert mq.passes_merge_gates(work, cfg, board) is False

    def test_refused_on_no_verdict_and_no_review(self) -> None:
        """#795 repro: no test verdict at all, no review → gate refuses."""
        cfg = self._config()
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        assert mq.passes_merge_gates(work, cfg, board) is False

    def test_passes_with_passed_test_and_approved_review(self) -> None:
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        review = self._review("w1", verdict="approve")
        board = self._board(completed=[work, review])
        assert mq.passes_merge_gates(work, cfg, board) is True

    def test_passes_when_gates_disabled(self) -> None:
        cfg = self._config(reviews_enabled=False, gates=["merge"])
        work = self._work("w1", test_state=None)
        board = self._board(completed=[work])
        assert mq.passes_merge_gates(work, cfg, board) is True


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

    def test_requires_smoke_entry_override_bypasses_default(self) -> None:
        # #1213: an entry whose snapshotted required_gates drops "test"
        # bypasses the smoke gate even though the default policy requires it.
        cfg = self._config(gates=["test", "merge"])
        entry = _q("a", required_gates=["merge"])
        assert mq.requires_smoke(entry, cfg) is False

    def test_requires_smoke_entry_override_can_also_require_it(self) -> None:
        cfg = self._config(gates=["merge"])
        entry = _q("a", required_gates=["test", "merge"])
        assert mq.requires_smoke(entry, cfg) is True

    def test_requires_smoke_empty_entry_gates_falls_back_to_default(self) -> None:
        # #1213 compatibility contract: untagged work (entry.required_gates
        # empty/absent) must behave exactly as before — default policy wins.
        cfg = self._config(gates=["test", "merge"])
        assert mq.requires_smoke(_q("a", required_gates=[]), cfg) is True
        cfg_without = self._config(gates=["merge"])
        assert mq.requires_smoke(_q("a", required_gates=[]), cfg_without) is False

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

    def test_has_smoke_verdict_mock_author_none_returns_false(self) -> None:
        """#930 fix: a ``type="mock-author"`` (Gate A) entry with no test
        verdict must correctly fail the gate (``False``), not silently fail
        open — before the fix, the ``type == "work"`` filter excluded the
        mock-author row itself from ``branch_work``, so this incorrectly
        returned ``True`` (fail-open) regardless of ``test_state``."""
        mock_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ma1", type="mock-author", status="done",
            branch="ms-5-gate-a", test_state=None,
        )
        board = self._board(completed=[mock_author])
        assert mq.has_smoke_verdict(_q("ma1", branch="ms-5-gate-a"), board) is False

    def test_has_smoke_verdict_mock_author_passed(self) -> None:
        """#930 fix: same as above but with a passed verdict — must now
        correctly return True by actually checking test_state, rather than
        via the old accidental fail-open."""
        mock_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ma1", type="mock-author", status="done",
            branch="ms-5-gate-a", test_state="passed",
        )
        board = self._board(completed=[mock_author])
        assert mq.has_smoke_verdict(_q("ma1", branch="ms-5-gate-a"), board) is True

    def test_has_smoke_verdict_test_author_none_returns_false(self) -> None:
        """#1141 fix: a ``type="test-author"`` (#931, per-issue JIT
        acceptance-slice authoring) entry with no test verdict must correctly
        fail the gate (``False``), not silently fail open — mirrors the
        mock-author fix from #930, which test-author never got."""
        test_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ta1", type="test-author", status="done",
            branch="ms-37-test-author", test_state=None,
        )
        board = self._board(completed=[test_author])
        assert mq.has_smoke_verdict(_q("ta1", branch="ms-37-test-author"), board) is False

    def test_has_smoke_verdict_test_author_skipped(self) -> None:
        """#1141 fix: same as above but with a ``skipped`` verdict — the
        expected verdict for a fixtures/tests-only test-author diff (nothing
        to smoke) — must correctly return True."""
        test_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ta1", type="test-author", status="done",
            branch="ms-37-test-author", test_state="skipped",
        )
        board = self._board(completed=[test_author])
        assert mq.has_smoke_verdict(_q("ta1", branch="ms-37-test-author"), board) is True

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
        """Legacy callers that don't pass config/board still work.

        When config is None, requires_smoke() returns False (no "test" gate
        configured) so no smoke gate fires.  The fail-closed rule (#821) only
        applies when config is present and says smoke is required but board
        is absent.
        """
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


class TestGateBypassAudit:
    """#1213: a per-issue label override honoured by requires_review /
    requires_smoke merges without the bypassed gate(s), and every bypass
    writes a ``gate_bypassed`` business-tier audit row + a CLI-visible note
    on the "merged" event — never silent."""

    @staticmethod
    def _config(*, default_gates=None, labels=None):
        from dataclasses import dataclass, field as dc_field
        @dataclass
        class _Reviews:
            enabled: bool = True
        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None
            labels: dict = dc_field(default_factory=dict)
        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
        cfg = _Cfg()
        cfg.pipeline.default_gates = (
            default_gates if default_gates is not None else ["test", "review", "merge"]
        )
        cfg.pipeline.labels = labels or {}
        return cfg

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _audit_rows(coord_db, event_type: str = "gate_bypassed") -> list:
        return coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type = ?", (event_type,)
        ).fetchall()

    def test_merge_only_label_bypasses_review_and_smoke(self, coord_db) -> None:
        cfg = self._config(labels={"gate:trivial": ["merge"]})
        board = self._board()  # no review, no smoke verdict anywhere
        items = [_q("a", required_gates=["merge"])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == MERGED
        merged = [e for e in events if e.kind == "merged"]
        assert merged
        assert "gate bypass" in merged[0].message
        assert "gate:trivial" in merged[0].message

        rows = self._audit_rows(coord_db)
        assert len(rows) == 1
        assert rows[0]["tier"] == "business"
        assert rows[0]["category"] == "gate"
        assert rows[0]["actor"] == "user"
        details = json.loads(rows[0]["details_json"])
        assert details["label"] == "gate:trivial"
        assert sorted(details["bypassed_gates"]) == ["review", "test"]
        assert details["resolved_gates"] == ["merge"]

    def test_untagged_work_is_completely_unaffected(self, coord_db) -> None:
        # #1213 acceptance: the important regression test — untagged work
        # (no per-issue override) must still be gated exactly as before.
        cfg = self._config()
        board = self._board()  # no review, no smoke verdict
        items = [_q("a", required_gates=[])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == PENDING
        kinds = [e.kind for e in events]
        assert "review_required" in kinds
        assert "merged" not in kinds
        assert self._audit_rows(coord_db) == []

    def test_label_resolving_to_test_and_merge_still_requires_test(self, coord_db) -> None:
        # An issue whose label resolves to ["test", "merge"] still requires
        # a Test verdict, just not a review.  Board carries the matching work
        # assignment with no verdict yet, so the smoke gate fails closed
        # (has_smoke_verdict only fails *open* when no matching branch work
        # is found on the board at all).
        cfg = self._config(labels={"needs-test": ["test", "merge"]})
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="a", type="work", status="done", branch="worker/a",
            test_state=None,
        )
        board = self._board(completed=[work])
        items = [_q("a", required_gates=["test", "merge"])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == PENDING
        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds
        assert "review_required" not in kinds
        assert self._audit_rows(coord_db) == []

    def test_label_resolving_to_test_and_merge_merges_once_tested(self, coord_db) -> None:
        cfg = self._config(labels={"needs-test": ["test", "merge"]})
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="a", type="work", status="done", branch="worker/a",
            test_state="passed",
        )
        board = self._board(completed=[work])
        items = [_q("a", required_gates=["test", "merge"])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == MERGED
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "review" in merged[0].message

        rows = self._audit_rows(coord_db)
        assert len(rows) == 1
        details = json.loads(rows[0]["details_json"])
        assert details["bypassed_gates"] == ["review"]

    def test_no_audit_row_when_resolved_gates_match_default(self, coord_db) -> None:
        # An entry carrying required_gates that happens to equal the default
        # policy isn't a real bypass — no phantom audit row.
        cfg = self._config(default_gates=["merge"])
        board = self._board()
        items = [_q("a", required_gates=["merge"])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == MERGED
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "gate bypass" not in merged[0].message
        assert self._audit_rows(coord_db) == []

    def test_dry_run_shows_bypass_note_but_writes_no_audit(self, coord_db) -> None:
        # #1213: "coord merge output names any bypassed gate" applies to the
        # dry-run preview too, but a preview must never write an audit row.
        cfg = self._config(labels={"gate:trivial": ["merge"]})
        board = self._board()
        items = [_q("a", required_gates=["merge"])]
        events = process(items, FakeGh(), config=cfg, board=board, dry_run=True)

        merged = [e for e in events if e.kind == "merged"]
        assert merged and "gate bypass" in merged[0].message
        assert self._audit_rows(coord_db) == []


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

    def test_preserves_assignment_type_across_review_bounce(self, coord_db) -> None:
        # #1077 (review round 1): a mock-author entry's assignment_type must
        # survive a review bounce. auto_loop._dispatch_fix_for_review
        # unconditionally dispatches fix workers with type="work" regardless
        # of the original assignment's type, and that fix assignment is what
        # reaches refresh_entry_assignment once its own re-review approves
        # (via _advance_pipeline). If assignment_type were re-keyed from the
        # fix assignment here, every ordinary request-changes round trip on a
        # Gate A mock-author PR would flip the entry back to "work" and
        # re-enable close-on-merge -- reproducing the original #1077 bug.
        orig = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", type="mock-author", branch="worker/orig",
            status="done",
        )
        mq.enqueue(orig, repo_github="acme/api", target_branch="main")
        assert load_queue()[0].assignment_type == "mock-author"

        # Simulate the bounce: fix worker is dispatched with type="work"
        # hardcoded, same branch as the original.
        fix = self._work("fix1", branch="worker/orig")
        assert fix.type == "work"
        result = mq.refresh_entry_assignment(fix, repo_github="acme/api", target_branch="main")
        assert result is True
        items = load_queue()
        assert items[0].assignment_id == "fix1"  # assignment_id does re-key
        assert items[0].assignment_type == "mock-author"  # type does NOT

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


class TestEnqueueApprovedWork:
    """#736: enqueue_approved_work() is the daemon-tick path for reliable
    enqueue-on-approval — called from _passive_tick every 30 seconds so
    approved+tested work enters the merge queue without a manual coord merge.
    """

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _config(*, review_enabled: bool = True, gates: list[str] | None = None):
        """Minimal config-like object with .reviews, .pipeline, and .repo()."""
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class _Reviews:
            enabled: bool = True

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Repo:
            name: str = "api"
            github: str = "acme/api"
            default_branch: str = "main"

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
            _repos: list = dc_field(default_factory=lambda: [_Repo()])

            def repo(self, name: str):
                return next((r for r in self._repos if r.name == name), None)

        cfg = _Cfg()
        cfg.reviews.enabled = review_enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["review", "test", "merge"]
        return cfg

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str, *, test_state: str | None = "passed", branch: str | None = None) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done",
            branch=branch or f"issue-1-{aid}",
            test_state=test_state,
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str = "approve") -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=f"rev-{of_aid}", type="review", status="done",
            review_of_assignment_id=of_aid, review_verdict=verdict,
        )

    # ── basic happy path ──────────────────────────────────────────────────

    def test_enqueues_when_approved_and_test_passed(self, coord_db) -> None:
        """Approved review + passed test → entry created in merge queue."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["w1"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "w1"
        assert items[0].branch == "issue-1-w1"

    def test_enqueues_mock_author_completion(self, coord_db) -> None:
        """#930 fix: a completed ``type="mock-author"`` (Gate A) assignment
        with an approved review + passed test must be enqueued the same as
        ordinary work — previously the scan hard-filtered on
        ``type == "work"`` so a Gate A branch could never reach the merge
        queue through any coord command."""
        cfg = self._config()
        mock_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ma1", type="mock-author", status="done",
            branch="ms-5-gate-a", test_state="passed",
        )
        rev = self._review("ma1", verdict="approve")
        board = self._board(completed=[mock_author, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["ma1"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "ma1"
        assert items[0].branch == "ms-5-gate-a"

    def test_enqueues_test_author_completion(self, coord_db) -> None:
        """#1141 fix: a completed ``type="test-author"`` (#931, per-issue JIT
        acceptance-slice authoring) assignment with an approved review +
        skipped test must be enqueued the same as ordinary work — previously
        the scan didn't recognize ``test-author`` so a JIT slice could never
        reach the merge queue through any coord command (confirmed live on
        PR #1139, epic #1117/ms-37 retrofit)."""
        cfg = self._config()
        test_author = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="ta1", type="test-author", status="done",
            branch="ms-37-test-author", test_state="skipped",
        )
        rev = self._review("ta1", verdict="approve")
        board = self._board(completed=[test_author, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["ta1"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "ta1"
        assert items[0].branch == "ms-37-test-author"

    def test_enqueues_when_test_state_is_skipped(self, coord_db) -> None:
        """test_state='skipped' also satisfies the smoke gate."""
        cfg = self._config()
        work = self._work("w2", test_state="skipped")
        rev = self._review("w2", verdict="approve")
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert "w2" in changed
        assert any(i.assignment_id == "w2" for i in load_queue())

    # ── idempotency ───────────────────────────────────────────────────────

    def test_is_idempotent(self, coord_db) -> None:
        """Second call with the same board is a no-op."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        first = mq.enqueue_approved_work(cfg, board)
        second = mq.enqueue_approved_work(cfg, board)

        assert first == ["w1"]
        assert second == []  # already enqueued, no change
        assert len(load_queue()) == 1

    # ── gate conditions ───────────────────────────────────────────────────

    def test_skips_when_review_required_but_not_approved(self, coord_db) -> None:
        """No approved review → item is NOT enqueued when review is required."""
        cfg = self._config(review_enabled=True, gates=["review", "test", "merge"])
        work = self._work("w1", test_state="passed")
        # No review assignment on the board.
        board = self._board(completed=[work])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []
        assert load_queue() == []

    def test_skips_when_test_required_but_no_verdict(self, coord_db) -> None:
        """No test verdict → item is NOT enqueued when smoke is required."""
        cfg = self._config(gates=["review", "test", "merge"])
        work = self._work("w1", test_state=None)
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []
        assert load_queue() == []

    def test_enqueues_when_reviews_disabled(self, coord_db) -> None:
        """When reviews.enabled=False, the review gate is skipped entirely
        and items with a passing smoke verdict are enqueued."""
        cfg = self._config(review_enabled=False, gates=["test", "merge"])
        work = self._work("w1", test_state="passed")
        # No review on board — but reviews are disabled so it doesn't matter.
        board = self._board(completed=[work])

        changed = mq.enqueue_approved_work(cfg, board)

        assert "w1" in changed
        assert len(load_queue()) == 1

    def test_enqueues_when_smoke_gate_not_configured(self, coord_db) -> None:
        """When 'test' is absent from default_gates, smoke is not required."""
        cfg = self._config(gates=["review", "merge"])  # no 'test' gate
        work = self._work("w1", test_state=None)  # no test verdict — but gate off
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert "w1" in changed

    def test_skips_work_with_no_branch(self, coord_db) -> None:
        """Assignments without a branch are silently ignored."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")
        work.branch = None  # type: ignore[assignment]
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []

    def test_stale_merged_entry_for_different_branch_does_not_block_enqueue(
        self, coord_db
    ) -> None:
        """#1150: a MERGED queue entry from a *prior* work attempt on a
        different branch (same issue) must NOT block enqueue of fresh work —
        the old issue-level ``already_merged`` shortcut conflated "this issue
        has ever had a merge" with "this exact branch/commit is already
        merged". Termination is now decided solely by Gate 3's commit-aware
        ``work_is_terminal`` (stubbed non-terminal by the autouse fixture)."""
        cfg = self._config()
        work = self._work("w1", test_state="passed")  # branch "issue-1-w1"
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])
        # Seed a MERGED entry for the SAME issue but a DIFFERENT branch — e.g.
        # the issue's original, already-shipped PR from a prior cycle.
        mq.save_queue([_q("orig", state=mq.MERGED, repo="api", branch="worker/orig")])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["w1"]
        branches = {x.branch for x in load_queue()}
        assert "issue-1-w1" in branches
        # The historical MERGED entry is untouched.
        merged = [x for x in load_queue() if x.assignment_id == "orig"]
        assert merged and merged[0].state == mq.MERGED

    def test_still_skips_when_work_is_terminal_reports_true(
        self, coord_db, monkeypatch
    ) -> None:
        """When Gate 3 (``work_is_terminal``, commit-aware post-#1150) genuinely
        reports this branch as terminal, enqueue is still correctly skipped."""
        from coord import github_ops

        cfg = self._config()
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])
        monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: True)

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []
        assert load_queue() == []

    def test_skips_unknown_repo(self, coord_db) -> None:
        """Assignments for a repo not in config are silently skipped."""
        cfg = self._config()  # only has 'api'
        work = Assignment(
            machine_name="m1", repo_name="unknown-repo", issue_number=1,
            issue_title="t", assignment_id="w1", type="work",
            status="done", branch="issue-1-w1", test_state="passed",
        )
        rev = Assignment(
            machine_name="m2", repo_name="unknown-repo", issue_number=1,
            issue_title="t", assignment_id="rev-w1", type="review",
            status="done", review_of_assignment_id="w1", review_verdict="approve",
        )
        board = self._board(completed=[work, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []

    # ── re-keying after bounce (#292) ─────────────────────────────────────

    def test_rekeyes_after_bounce(self, coord_db) -> None:
        """After a review bounce the fix work's approval re-keys the queue
        entry so has_approved_review can find it (#292 Defect 2)."""
        cfg = self._config()

        # Original work is done; its entry was created by a prior coord merge run.
        orig_work = self._work("orig", branch="issue-1-orig")
        mq.save_queue([
            QueuedMerge(
                assignment_id="orig",
                repo_name="api",
                repo_github="acme/api",
                branch="issue-1-orig",
                target_branch="main",
                issue_number=1,
                issue_title="t",
            )
        ])

        # Fix work is now done on the same branch; it was approved.
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix-1] t",
            assignment_id="fix1", type="work", status="done",
            branch="issue-1-orig",  # same branch as orig_work
            test_state="passed",
        )
        fix_rev = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-fix1", type="review", status="done",
            review_of_assignment_id="fix1", review_verdict="approve",
        )
        board = self._board(completed=[orig_work, fix_work, fix_rev])

        changed = mq.enqueue_approved_work(cfg, board)

        # The entry was re-keyed to fix1 (the approved fix assignment).
        assert changed == ["fix1"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "fix1"
        assert items[0].branch == "issue-1-orig"

    def test_rekeying_is_idempotent(self, coord_db) -> None:
        """Re-keying is a no-op when the entry is already keyed to fix1."""
        cfg = self._config()

        # Entry already keyed to fix1.
        mq.save_queue([
            QueuedMerge(
                assignment_id="fix1",
                repo_name="api",
                repo_github="acme/api",
                branch="issue-1-orig",
                target_branch="main",
                issue_number=1,
                issue_title="t",
            )
        ])

        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix-1] t",
            assignment_id="fix1", type="work", status="done",
            branch="issue-1-orig",
            test_state="passed",
        )
        fix_rev = Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="rev-fix1", type="review", status="done",
            review_of_assignment_id="fix1", review_verdict="approve",
        )
        board = self._board(completed=[fix_work, fix_rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == []  # already correct — no change
        assert load_queue()[0].assignment_id == "fix1"


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


# ── #732 drop_entry / prune_stale_queue_entries ───────────────────────────────

class TestDropEntry:
    """#732: drop_entry() removes exactly one row by assignment_id."""

    def test_drops_existing_entry(self, coord_db) -> None:
        save_queue([_q("aid1"), _q("aid2")])
        removed = mq.drop_entry("aid1")
        assert removed is True
        remaining = load_queue()
        assert [x.assignment_id for x in remaining] == ["aid2"]

    def test_returns_false_when_not_found(self, coord_db) -> None:
        save_queue([_q("aid1")])
        removed = mq.drop_entry("ghost")
        assert removed is False
        # original entry untouched
        assert len(load_queue()) == 1

    def test_returns_false_on_empty_queue(self, coord_db) -> None:
        assert mq.drop_entry("anything") is False

    def test_only_removes_exact_match(self, coord_db) -> None:
        """Prefix / substring of an ID must not match."""
        save_queue([_q("aid-long"), _q("aid")])
        mq.drop_entry("aid")
        remaining = [x.assignment_id for x in load_queue()]
        assert "aid-long" in remaining
        assert "aid" not in remaining


class TestPruneStaleQueueEntries:
    """#732: prune_stale_queue_entries() removes closed-issue / merged-PR entries."""

    def _seed(self, coord_db, entries: list[QueuedMerge]) -> None:
        save_queue(entries)

    def test_prunes_closed_issue(self, coord_db, monkeypatch) -> None:
        from coord import github_ops

        monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: n == 217)
        monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, branch: False)

        self._seed(coord_db, [
            _q("stale", state=CONFLICT),
            _q("live"),
        ])
        pruned = mq.prune_stale_queue_entries()
        assert len(pruned) == 0  # issue_number on _q() is 1, not 217
        # Seed with the right issue number
        save_queue([
            QueuedMerge(
                assignment_id="stale217",
                repo_name="api", repo_github="acme/api",
                branch="issue-217-foo", target_branch="main",
                issue_number=217, issue_title="closed issue",
                state=CONFLICT,
            ),
            _q("live"),
        ])
        pruned = mq.prune_stale_queue_entries()
        assert len(pruned) == 1
        assert pruned[0].assignment_id == "stale217"
        remaining = load_queue()
        assert len(remaining) == 1
        assert remaining[0].assignment_id == "live"

    def test_prunes_merged_pr(self, coord_db, monkeypatch) -> None:
        from coord import github_ops

        monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: False)
        monkeypatch.setattr(
            github_ops, "pr_is_merged",
            lambda repo, branch: branch == "issue-1-merged-branch",
        )

        save_queue([
            QueuedMerge(
                assignment_id="merged-aid",
                repo_name="api", repo_github="acme/api",
                branch="issue-1-merged-branch", target_branch="main",
                issue_number=1, issue_title="t",
                state=PENDING,
            ),
            _q("live", branch="issue-2-live"),
        ])
        pruned = mq.prune_stale_queue_entries()
        assert [x.assignment_id for x in pruned] == ["merged-aid"]
        assert [x.assignment_id for x in load_queue()] == ["live"]

    def test_leaves_merged_state_entry_untouched(self, coord_db, monkeypatch) -> None:
        """MERGED-state entries are correct history — must not be re-checked."""
        from coord import github_ops

        calls: list[str] = []
        monkeypatch.setattr(
            github_ops, "issue_is_closed",
            lambda repo, n: calls.append("closed") or False,
        )
        monkeypatch.setattr(
            github_ops, "pr_is_merged",
            lambda repo, b: calls.append("pr") or False,
        )

        save_queue([_q("done", state=MERGED)])
        pruned = mq.prune_stale_queue_entries()
        assert pruned == []
        assert calls == []  # no gh calls at all
        assert len(load_queue()) == 1

    def test_dry_run_does_not_write(self, coord_db, monkeypatch) -> None:
        from coord import github_ops

        monkeypatch.setattr(github_ops, "issue_is_closed", lambda repo, n: True)
        monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: False)

        save_queue([_q("stale")])
        pruned = mq.prune_stale_queue_entries(dry_run=True)
        assert len(pruned) == 1
        assert len(load_queue()) == 1  # still there — dry run

    def test_fail_open_on_gh_error(self, coord_db, monkeypatch) -> None:
        """A gh error in issue_is_closed keeps the entry (fail-open)."""
        from coord import github_ops

        monkeypatch.setattr(
            github_ops, "issue_is_closed",
            lambda repo, n: False,  # gh error simulated as False (fail-open)
        )
        monkeypatch.setattr(github_ops, "pr_is_merged", lambda repo, b: False)

        save_queue([_q("live")])
        pruned = mq.prune_stale_queue_entries()
        assert pruned == []
        assert len(load_queue()) == 1


# ── #776: enqueued_at + size-at-enqueue-time ──────────────────────────────────

class TestEnqueuedAt:
    """#776: enqueue() sets enqueued_at and populates size via the compare API."""

    def _assignment(self, aid: str = "abc", branch: str = "issue-1-foo") -> Assignment:
        return Assignment(
            machine_name="m", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, branch=branch, status="done",
        )

    def test_enqueue_sets_enqueued_at(self, coord_db, monkeypatch) -> None:
        from coord import github_ops
        monkeypatch.setattr(github_ops, "get_branch_diff_size", lambda *a: 0)
        before = mq.__import_time = __import__("time").time()
        enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        items = load_queue()
        assert len(items) == 1
        assert items[0].enqueued_at is not None
        assert items[0].enqueued_at >= before

    def test_enqueue_populates_size_from_compare_api(self, coord_db, monkeypatch) -> None:
        from coord import github_ops
        monkeypatch.setattr(github_ops, "get_branch_diff_size", lambda repo, base, branch: 123)
        enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        items = load_queue()
        assert items[0].size == 123

    def test_enqueue_size_none_on_compare_failure(self, coord_db, monkeypatch) -> None:
        """When get_branch_diff_size returns 0, size is stored as None (unknown)."""
        from coord import github_ops
        monkeypatch.setattr(github_ops, "get_branch_diff_size", lambda *a: 0)
        enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        items = load_queue()
        # 0 is treated as unknown → None so unknown-size entries sort last.
        assert items[0].size is None

    def test_enqueue_size_survives_exception(self, coord_db, monkeypatch) -> None:
        """If the compare API raises, enqueue still succeeds with size=None."""
        from coord import github_ops
        def _raise(*a):
            raise RuntimeError("gh error")
        monkeypatch.setattr(github_ops, "get_branch_diff_size", _raise)
        entry = enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        assert entry is not None
        assert entry.size is None

    def test_enqueued_at_roundtrips_through_db(self, coord_db, monkeypatch) -> None:
        from coord import github_ops
        monkeypatch.setattr(github_ops, "get_branch_diff_size", lambda *a: 50)
        entry = enqueue(self._assignment(), repo_github="acme/api", target_branch="main")
        assert entry is not None
        loaded = load_queue()[0]
        assert loaded.enqueued_at == pytest.approx(entry.enqueued_at, abs=1.0)
        assert loaded.size == 50


# ── #776: plan() ─────────────────────────────────────────────────────────────

class TestPlan:
    """#776: plan() returns an ordered, gate-annotated PlannedMerge list.

    The plan is the single source of truth for ordering and gate-status —
    it must match sequence() exactly and apply the same gate logic as process().
    """

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _config(*, review_enabled: bool = True, gates: list[str] | None = None):
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class _Reviews:
            enabled: bool = True

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)

        cfg = _Cfg()
        cfg.reviews.enabled = review_enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["review", "test", "merge"]
        return cfg

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1", *, test_state: str | None = "passed") -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done",
            branch=f"issue-1-{aid}", test_state=test_state,
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str = "approve") -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=f"rev-{of_aid}", type="review", status="done",
            review_of_assignment_id=of_aid, review_verdict=verdict,
        )

    @staticmethod
    def _seed_queue(
        items: list,
        *,
        monkeypatch,
        github_ops_mod=None,
    ) -> None:
        """Seed pre-built QueuedMerge items directly (bypass enqueue size-lookup)."""
        save_queue(items)

    # ── ordering tests ────────────────────────────────────────────────────

    def test_ordering_matches_sequence(self, coord_db) -> None:
        """Plan order within a group must match sequence() (size-ascending)."""
        items = [_q("big", size=500), _q("small", size=50), _q("mid", size=100)]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        aids = [p.assignment_id for p in plan]
        # sequence() returns [small, mid, big]
        assert aids == ["small", "mid", "big"]

    def test_rank_is_one_based_ascending(self, coord_db) -> None:
        """Rank starts at 1 and increments by 1 per entry."""
        items = [_q("a", size=10), _q("b", size=20), _q("c", size=30)]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert [p.rank for p in plan] == [1, 2, 3]

    def test_unknown_size_goes_last(self, coord_db) -> None:
        """Entries with unknown size are placed last (same as sequence())."""
        items = [_q("big", size=None), _q("small", size=50)]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert [p.assignment_id for p in plan] == ["small", "big"]

    def test_groups_by_repo_and_target_branch(self, coord_db) -> None:
        """Each (repo_github, target_branch) group is ordered independently."""
        items = [
            _q("api-big",   repo="api", repo_github="acme/api", target="main",    size=500),
            _q("api-small", repo="api", repo_github="acme/api", target="main",    size=50),
            _q("ui-big",    repo="ui",  repo_github="acme/ui",  target="develop", size=300),
        ]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        # Both groups present in plan; each group ordered by size
        aids = [p.assignment_id for p in plan]
        # api group: small first; ui group has one entry
        assert "api-small" in aids
        api_idx_small = aids.index("api-small")
        api_idx_big   = aids.index("api-big")
        assert api_idx_small < api_idx_big

    # ── gate-status tests ─────────────────────────────────────────────────

    def test_ready_when_all_gates_pass(self, coord_db) -> None:
        """An entry with approved review + passed test appears as READY."""
        items = [_q("w1", size=100)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg)
        assert len(plan) == 1
        assert plan[0].status == mq.PLAN_READY
        assert plan[0].reason is None
        assert plan[0].rank == 1
        assert plan[0].size == 100

    def test_blocked_review_not_approved(self, coord_db) -> None:
        """Entry missing an approved review appears as BLOCKED with reason."""
        items = [_q("w1", size=50)]
        save_queue(items)
        # No review on the board
        board = self._board(completed=[self._work("w1", test_state="passed")])
        cfg = self._config()
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "review" in (plan[0].reason or "").lower()

    def test_blocked_test_verdict_missing(self, coord_db) -> None:
        """Entry with no test verdict appears as BLOCKED with reason."""
        items = [_q("w1", size=50)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state=None),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "test" in (plan[0].reason or "").lower()

    def test_blocked_ci_failed(self, coord_db) -> None:
        """Entry with a failed CI check appears as BLOCKED with CI reason."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(name="build", status="completed", conclusion="failure")]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "CI failed" in (plan[0].reason or "")

    def test_blocked_ci_running(self, coord_db) -> None:
        """Entry with a still-running CI check appears as BLOCKED."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(name="build", status="in_progress", conclusion=None)]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "CI running" in (plan[0].reason or "")

    def test_ci_not_checked_without_pr_number(self, coord_db) -> None:
        """An entry with no PR yet opened is not blocked on CI."""
        from types import SimpleNamespace

        class AlwaysFailCi:
            is_available = True
            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(name="build", status="completed", conclusion="failure")]

        # pr=None → no pr_number
        items = [_q("w1", size=50)]  # pr_number=None by default
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        # Even with a failing CI, no pr_number → CI gate skipped → READY
        plan = mq.plan(board, cfg, ci_store=AlwaysFailCi())
        assert plan[0].status == mq.PLAN_READY

    # ── non-PENDING state mapping ─────────────────────────────────────────

    def test_merging_entry_status(self, coord_db) -> None:
        items = [_q("w1", state=mq.MERGING)]
        save_queue(items)
        board = self._board()
        cfg = self._config(review_enabled=False, gates=["merge"])
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_MERGING

    def test_merged_entry_status(self, coord_db) -> None:
        items = [_q("w1", state=mq.MERGED)]
        save_queue(items)
        board = self._board()
        cfg = self._config(review_enabled=False, gates=["merge"])
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_MERGED

    def test_conflict_entry_status(self, coord_db) -> None:
        items = [_q("w1", state=mq.CONFLICT)]
        save_queue(items)
        board = self._board()
        cfg = self._config(review_enabled=False, gates=["merge"])
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_NEEDS_ATTENTION

    # ── metadata fields ───────────────────────────────────────────────────

    def test_target_branch_is_populated(self, coord_db) -> None:
        items = [_q("w1", target="develop")]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert plan[0].target_branch == "develop"

    def test_enqueued_at_propagated(self, coord_db) -> None:
        import time as _time
        ts = _time.time() - 60.0
        q = QueuedMerge(
            assignment_id="w1", repo_name="api", repo_github="acme/api",
            branch="issue-1-w1", target_branch="main",
            issue_number=1, issue_title="t",
            enqueued_at=ts,
        )
        save_queue([q])
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert plan[0].enqueued_at == pytest.approx(ts, abs=1.0)

    def test_milestone_from_issues_table(self, coord_db) -> None:
        """Milestone title is pulled from the issues table when present."""
        from coord.db import get_connection
        conn = get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO issues "
            "(repo_name, number, title, body, state, labels, milestone_title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("api", 1, "t", "", "open", "[]", "v1.0"),
        )
        conn.commit()

        items = [_q("w1")]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert plan[0].milestone == "v1.0"

    def test_milestone_none_when_not_in_issues_table(self, coord_db) -> None:
        items = [_q("w1")]
        save_queue(items)
        cfg = self._config(review_enabled=False, gates=["merge"])
        board = self._board()
        plan = mq.plan(board, cfg)
        assert plan[0].milestone is None

    # ── empty queue ───────────────────────────────────────────────────────

    def test_empty_queue_returns_empty_list(self, coord_db) -> None:
        cfg = self._config()
        board = self._board()
        plan = mq.plan(board, cfg)
        assert plan == []

    # ── gate_status helper (unit test for _entry_gate_status) ─────────────

    def test_entry_gate_status_ready(self, coord_db) -> None:
        """All gates pass → READY."""
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        entry = _q("w1")
        cfg = self._config()
        status, reason = mq._entry_gate_status(entry, board, cfg)
        assert status == mq.PLAN_READY
        assert reason is None

    def test_entry_gate_status_no_config_returns_ready(self) -> None:
        """Without config/board, gate evaluation is skipped → READY."""
        entry = _q("w1")
        status, reason = mq._entry_gate_status(entry, None, None)
        assert status == mq.PLAN_READY
        assert reason is None


# ── #778: staging_items() ─────────────────────────────────────────────────────

class TestStagingItems:
    """#778: staging_items() surfaces approved/done work not yet in the queue.

    The helper must:
    - Return READY items when all gates pass.
    - Return BLOCKED items when the smoke gate fails.
    - Exclude items whose review is not yet approved.
    - Exclude items already tracked in the merge queue.
    - Exclude items from issues already MERGED.
    - Behave sensibly when review or smoke gates are disabled.
    """

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _config(*, review_enabled: bool = True, gates: list[str] | None = None):
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class _Reviews:
            enabled: bool = True

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)

        cfg = _Cfg()
        cfg.reviews.enabled = review_enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["review", "test", "merge"]
        return cfg

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(
        aid: str = "w1",
        *,
        test_state: str | None = "passed",
        branch: str | None = None,
        issue_number: int = 42,
    ) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=issue_number,
            issue_title="Some feature", assignment_id=aid, type="work",
            status="done", branch=branch or f"issue-{issue_number}-{aid}",
            test_state=test_state,
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str = "approve") -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=42,
            issue_title="Some feature", assignment_id=f"rev-{of_aid}",
            type="review", status="done",
            review_of_assignment_id=of_aid, review_verdict=verdict,
        )

    # ── ready path ────────────────────────────────────────────────────────

    def test_ready_when_approved_and_smoke_passed(self, coord_db) -> None:
        """Approved review + passed test → READY staging item."""
        work = self._work("w1", test_state="passed")
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].assignment_id == "w1"
        assert items[0].status == mq.STAGING_READY
        assert items[0].reason is None

    def test_ready_when_mock_author_approved_and_smoke_passed(self, coord_db) -> None:
        """#930 fix: a ``type="mock-author"`` (Gate A) completion is a
        staging item too — mirrors ordinary work, since it must flow through
        the same Work -> Test -> Review -> Merge pipeline."""
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=42,
            issue_title="Some feature", assignment_id="ma1", type="mock-author",
            status="done", branch="ms-5-gate-a", test_state="passed",
        )
        rev = self._review("ma1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].assignment_id == "ma1"
        assert items[0].status == mq.STAGING_READY

    def test_ready_when_test_author_approved_and_smoke_skipped(self, coord_db) -> None:
        """#1141 fix: a ``type="test-author"`` (#931, per-issue JIT
        acceptance-slice authoring) completion is a staging item too —
        mirrors ordinary work/mock-author, since it must flow through the
        same Work -> Test -> Review -> Merge pipeline. Uses a skipped test
        verdict, the expected verdict for a fixtures/tests-only diff."""
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1117,
            issue_title="ms-37 acceptance slice", assignment_id="ta1",
            type="test-author", status="done", branch="ms-37-test-author",
            test_state="skipped",
        )
        rev = self._review("ta1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].assignment_id == "ta1"
        assert items[0].status == mq.STAGING_READY

    def test_ready_when_approved_and_smoke_skipped(self, coord_db) -> None:
        """Approved review + skipped test → READY (skipped counts as verdict)."""
        work = self._work("w1", test_state="skipped")
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].status == mq.STAGING_READY

    # ── blocked path ──────────────────────────────────────────────────────

    def test_blocked_when_smoke_verdict_missing(self, coord_db) -> None:
        """Approved review but no smoke verdict → BLOCKED with reason."""
        work = self._work("w1", test_state=None)
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].status == mq.STAGING_BLOCKED
        assert items[0].reason == "test verdict missing"

    def test_blocked_when_smoke_verdict_failed(self, coord_db) -> None:
        """test_state='failed' counts as missing for staging purposes."""
        work = self._work("w1", test_state="failed")
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].status == mq.STAGING_BLOCKED

    # ── exclusion: review not yet approved ────────────────────────────────

    def test_excluded_when_review_not_approved(self, coord_db) -> None:
        """Work with request-changes review is NOT a staging item."""
        work = self._work("w1")
        rev = self._review("w1", verdict="request-changes")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert items == []

    def test_excluded_when_no_review_at_all(self, coord_db) -> None:
        """Work with no review at all is excluded when review gate is enabled."""
        work = self._work("w1")
        board = self._board(completed=[work])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert items == []

    # ── exclusion: already in queue ───────────────────────────────────────

    def test_excluded_when_already_queued(self, coord_db) -> None:
        """Items already in the merge queue are not shown in staging."""
        work = self._work("w1")
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        # Seed the queue with the same assignment_id.
        save_queue([_q("w1")])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert items == []

    def test_excluded_when_branch_already_queued_by_different_assignment(
        self, coord_db
    ) -> None:
        """A fix dispatched after the original work was enqueued must not
        appear in staging, even though its assignment_id differs from the
        queued entry.  Branch-level dedup catches this (#778 smoke-test
        failure: fix-1 cycled in/out of staging every ~30 s)."""
        branch = "issue-42-original"
        # The original work (different aid) is already in the queue.
        original_work = self._work("w-orig", branch=branch, issue_number=42)
        # A fix worker shares the same branch but has a fresh assignment_id.
        fix_work = self._work("w-fix", branch=branch, issue_number=42, test_state=None)
        rev = self._review("w-fix")
        board = self._board(completed=[original_work, fix_work, rev])
        # Queue contains the original assignment_id — NOT the fix's.
        save_queue([_q("w-orig", branch=branch)])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        # The fix must be excluded: its branch is already in the queue.
        assert items == [], (
            f"Expected no staging items but got: {items}"
        )

    def test_excluded_when_issue_already_merged(self, coord_db) -> None:
        """Items from an issue with a MERGED queue entry are excluded."""
        work = self._work("w1", issue_number=42)
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        # Seed a MERGED entry for the same (repo, issue) pair.
        merged_entry = QueuedMerge(
            assignment_id="old-w", repo_name="api", repo_github="acme/api",
            branch="issue-42-old", target_branch="main",
            issue_number=42, issue_title="Some feature",
            state=MERGED,
        )
        save_queue([merged_entry])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert items == []

    # ── gate-disabled paths ───────────────────────────────────────────────

    def test_included_when_review_gate_disabled(self, coord_db) -> None:
        """When reviews are disabled, work is included without needing a review."""
        work = self._work("w1")
        board = self._board(completed=[work])
        cfg = self._config(review_enabled=False, gates=["test", "merge"])
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].status == mq.STAGING_READY

    def test_included_when_smoke_gate_disabled(self, coord_db) -> None:
        """When 'test' is not in default_gates, missing verdict → READY."""
        work = self._work("w1", test_state=None)
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config(gates=["review", "merge"])  # no "test" gate
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].status == mq.STAGING_READY

    # ── metadata ─────────────────────────────────────────────────────────

    def test_item_carries_metadata(self, coord_db) -> None:
        """StagingItem carries the correct repo/issue/branch metadata."""
        work = self._work("w1", issue_number=99, branch="issue-99-w1")
        rev = self._review("w1")
        board = self._board(completed=[work, rev])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        item = items[0]
        assert item.assignment_id == "w1"
        assert item.repo_name == "api"
        assert item.issue_number == 99
        assert item.branch == "issue-99-w1"
        assert item.issue_title == "Some feature"

    # ── no-config / no-board ──────────────────────────────────────────────

    def test_returns_empty_without_board(self, coord_db) -> None:
        """Without a board there are no completed assignments to scan."""
        from coord.models import Board
        cfg = self._config()
        items = mq.staging_items(Board(active=[], completed=[]), cfg)
        assert items == []


# ── #420: display_error — recompute stale gate errors live ──────────────────

class TestDisplayError:
    """`coord status`'s merge-queue section must not echo a stored
    ``entry.error`` verbatim when it was a review/smoke gate message — that
    string is only refreshed by a real merge attempt (`process()`), so an
    approval or verdict recorded afterward (the normal interactive path, no
    `coord merge`/auto-loop tick in between) would otherwise keep showing as
    "blocked" forever, inviting an operator to redundantly bounce already-
    approved work (the #410 real-world case).
    """

    @staticmethod
    def _config(*, review_enabled: bool = True, gates: list[str] | None = None):
        from dataclasses import dataclass, field as dc_field
        @dataclass
        class _Reviews:
            enabled: bool = True
        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None
        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
        cfg = _Cfg()
        cfg.reviews.enabled = review_enabled
        cfg.pipeline.default_gates = gates if gates is not None else ["review", "test", "merge"]
        return cfg

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1", *, test_state: str | None = None) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done", branch=f"worker/{aid}",
            test_state=test_state,
        )

    @staticmethod
    def _review(of_aid: str, *, verdict: str | None = "approve") -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=f"rev-{of_aid}", type="review", status="done",
            review_of_assignment_id=of_aid, review_verdict=verdict,
        )

    def test_clears_stale_review_error_once_approved(self) -> None:
        """The #410 case: entry.error was stamped before the approval landed;
        a later read must not keep showing "review required but not approved"."""
        cfg = self._config()
        entry = _q("w1")
        entry.error = "review required but not approved"
        board = self._board(completed=[
            self._work("w1"), self._review("w1", verdict="approve"),
        ])
        assert mq.display_error(entry, board, cfg) is None

    def test_keeps_review_error_when_still_unapproved(self) -> None:
        cfg = self._config()
        entry = _q("w1")
        entry.error = "review required but not approved"
        board = self._board(completed=[self._work("w1")])
        assert mq.display_error(entry, board, cfg) == "review required but not approved"

    def test_keeps_review_error_when_request_changes(self) -> None:
        cfg = self._config()
        entry = _q("w1")
        entry.error = "review required but not approved"
        board = self._board(completed=[
            self._work("w1"), self._review("w1", verdict="request-changes"),
        ])
        assert mq.display_error(entry, board, cfg) == "review required but not approved"

    def test_clears_stale_smoke_error_once_verdict_recorded(self) -> None:
        cfg = self._config(review_enabled=False, gates=["test", "merge"])
        entry = _q("w1")
        entry.error = "smoke test required but no verdict recorded"
        board = self._board(completed=[self._work("w1", test_state="passed")])
        assert mq.display_error(entry, board, cfg) is None

    def test_keeps_smoke_error_when_no_verdict_yet(self) -> None:
        cfg = self._config(review_enabled=False, gates=["test", "merge"])
        entry = _q("w1")
        entry.error = "smoke test required but no verdict recorded"
        board = self._board(completed=[self._work("w1")])
        assert mq.display_error(entry, board, cfg) == "smoke test required but no verdict recorded"

    def test_other_errors_pass_through_unchanged(self) -> None:
        """Conflict/CI errors reflect the outcome of the last real attempt —
        they must not be recomputed just because board/config are available."""
        cfg = self._config()
        entry = _q("w1")
        entry.error = "checks failed: build (failure)"
        board = self._board(completed=[
            self._work("w1"), self._review("w1", verdict="approve"),
        ])
        assert mq.display_error(entry, board, cfg) == "checks failed: build (failure)"

    def test_none_error_stays_none(self) -> None:
        cfg = self._config()
        entry = _q("w1")
        board = self._board()
        assert mq.display_error(entry, board, cfg) is None

    def test_falls_back_to_stored_error_without_board_or_config(self) -> None:
        """Can't safely recompute without both board and config — keep the
        stored string rather than silently dropping a real block."""
        entry = _q("w1")
        entry.error = "review required but not approved"
        assert mq.display_error(entry, None, None) == "review required but not approved"
