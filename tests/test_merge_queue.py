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
