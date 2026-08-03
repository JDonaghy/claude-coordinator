"""Tests for coord.merge_queue — sequencing logic and the gh-driven processor."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from unittest.mock import patch

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
    # #1318: PR number -> commit messages on that PR; issue number -> whether
    # it carries the epic/tracking label. Defaults keep every prior test
    # (none of which set these) inert — get_pr_commit_messages returns []
    # and is_epic_issue returns False, matching pre-#1318 behavior.
    pr_commit_messages: dict[int, list[str]] = field(default_factory=dict)
    epic_issues: set[int] = field(default_factory=set)
    # #1477: PR number -> mergeable verdict (True/False/None). Defaults keep
    # every prior test (none of which set this) inert — check_pr_mergeable
    # returns None ("unknown") so reconcile_conflict_entries never unparks
    # an entry unless a test opts in explicitly.
    mergeable_results: dict[int, bool | None] = field(default_factory=dict)
    mergeable_calls: list[tuple[str, int]] = field(default_factory=list)
    # #1467: PR number -> whether the branch carries a merge commit
    # (True/False/None). Defaults keep every prior test (none of which set
    # this) inert — branch_has_merge_commit returns None ("unknown") so the
    # pre-flight squash fallback never fires unless a test opts in.
    merge_commit_results: dict[int, bool | None] = field(default_factory=dict)
    merge_commit_calls: list[tuple[str, int]] = field(default_factory=list)
    # #1624: branch name -> already-open PR dict ({"number", "url"}). Defaults
    # keep every prior test (none of which set this) inert —
    # find_pr_for_branch returns None so the dry-run "no PR yet" path runs,
    # matching pre-#1624 behavior.
    existing_prs: dict[str, dict] = field(default_factory=dict)
    find_pr_calls: list[tuple[str, str]] = field(default_factory=list)

    def find_pr_for_branch(self, repo: str, branch: str) -> dict | None:
        self.find_pr_calls.append((repo, branch))
        return self.existing_prs.get(branch)

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

    def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
        # #1475: tests don't exercise patch-id tracking by default; return
        # None so the fail-closed "no patch-id → stale on SHA mismatch"
        # path runs, matching get_branch_sha's default above.
        return None

    def get_pr_body(self, repo: str, number: int) -> str:
        return self.pr_bodies.get(number, "")

    def edit_pr_body(self, repo: str, number: int, body: str) -> None:
        self.edit_body_calls.append((repo, number, body))
        self.pr_bodies[number] = body

    def has_open_children(self, repo: str, issue_number: int) -> bool:
        return issue_number in self.open_children

    def is_epic_issue(self, repo: str, issue_number: int) -> bool:
        return issue_number in self.epic_issues

    def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
        return self.pr_commit_messages.get(number, [])

    def check_pr_mergeable(self, repo: str, number: int) -> bool | None:
        self.mergeable_calls.append((repo, number))
        return self.mergeable_results.get(number)

    def branch_has_merge_commit(self, repo: str, number: int) -> bool | None:
        self.merge_commit_calls.append((repo, number))
        return self.merge_commit_results.get(number)


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

    # ── #1318: pre-merge epic-closing-keyword guard (commit messages) ─────

    def test_epic_closing_keyword_in_commit_blocks_merge(self) -> None:
        # The #1314 incident: the PR body carries no closing keyword at all,
        # but a commit message on the branch does — GitHub's own scanner
        # reads commit messages verbatim once they land on the base branch,
        # so this must block the merge, not just lint the PR body.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(
            pr_commit_messages={100: [
                "fix(#1314): harden downstream breakages\n\n"
                "...its body carry \"Closes #1120\", which GitHub's native..."
            ]},
            epic_issues={1120},
        )
        events = process(items, gh)
        assert items[0].state == PENDING  # never merged
        assert gh.merge_calls == []
        blocked = [e for e in events if e.kind == "epic_closing_keyword_in_commit"]
        assert blocked and "#1120" in blocked[0].message
        assert items[0].error is not None and "#1120" in items[0].error

    def test_ordinary_closing_keyword_in_commit_passes_through(self) -> None:
        # Acceptance criterion from #1318: an ordinary `Closes #<non-epic>`
        # in a commit message must merge untouched — no epic label, no block.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(
            pr_commit_messages={100: ["fix(#55): a normal bug fix\n\nCloses #55"]},
            epic_issues=set(),
        )
        events = process(items, gh)
        assert items[0].state == MERGED
        assert not [e for e in events if "epic_closing_keyword" in e.kind]

    def test_force_merge_overrides_but_still_warns(self) -> None:
        # The override must never be silent — a warning event still fires
        # even though the merge proceeds.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(
            pr_commit_messages={100: ["Closes #1120"]},
            epic_issues={1120},
        )
        events = process(items, gh, force_merge=True)
        assert items[0].state == MERGED
        forced = [
            e for e in events if e.kind == "epic_closing_keyword_in_commit_forced"
        ]
        assert forced and "#1120" in forced[0].message

    def test_commit_message_lint_failure_never_blocks_the_merge(self) -> None:
        # Best-effort: a get_pr_commit_messages/is_epic_issue failure must
        # not itself prevent a merge.
        class _BoomOnCommits(FakeGh):
            def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
                raise RuntimeError("gh pr view --json commits failed")

        items = [_q("a", pr=100, size=10)]
        gh = _BoomOnCommits()
        process(items, gh)
        assert items[0].state == MERGED

    def test_pr_body_downgraded_for_epic_label_with_no_open_children(self) -> None:
        # #1318 widens the existing #1196 body downgrade: a fresh epic with
        # zero open children yet must still be protected, not just an epic
        # that already has open sub-issues.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(
            pr_bodies={100: "Closes #1120\n\nWorker-authored PR."},
            open_children=set(),
            epic_issues={1120},
        )
        events = process(items, gh)
        assert items[0].state == MERGED
        assert gh.edit_body_calls == [
            ("acme/api", 100, "Refs #1120\n\nWorker-authored PR.")
        ]
        downgraded = [e for e in events if e.kind == "pr_body_downgraded"]
        assert downgraded and "#1120" in downgraded[0].message


class TestProcessLinearityFallback:
    """#1467: `gh pr merge --rebase` refuses any branch containing a merge
    commit ("This branch can't be rebased") — a linearity failure GitHub's
    `mergeable` field can't predict. process() pre-flight-checks for a merge
    commit and falls back to --squash, which is always valid here."""

    def test_falls_back_to_squash_when_branch_has_merge_commit(self) -> None:
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(merge_commit_results={100: True})
        events = process(items, gh, method="rebase")

        assert items[0].state == MERGED
        assert gh.merge_calls == [("acme/api", 100, "squash")]
        fallback = [e for e in events if e.kind == "method_fallback"]
        assert len(fallback) == 1
        assert "squash" in fallback[0].message
        assert "#1467" in fallback[0].message

    def test_stays_on_rebase_when_branch_is_linear(self) -> None:
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(merge_commit_results={100: False})
        events = process(items, gh, method="rebase")

        assert items[0].state == MERGED
        assert gh.merge_calls == [("acme/api", 100, "rebase")]
        assert not [e for e in events if e.kind == "method_fallback"]

    def test_fail_closed_on_inconclusive_probe(self) -> None:
        # merge_commit_results defaults to {} -> None (inconclusive). The
        # method must stay unchanged rather than guess.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh()
        events = process(items, gh, method="rebase")

        assert gh.merge_calls == [("acme/api", 100, "rebase")]
        assert not [e for e in events if e.kind == "method_fallback"]

    def test_fail_closed_when_probe_raises(self) -> None:
        class RaisingGh(FakeGh):
            def branch_has_merge_commit(self, repo: str, number: int) -> bool | None:
                raise RuntimeError("gh timeout")

        items = [_q("a", pr=100, size=10)]
        gh = RaisingGh()
        events = process(items, gh, method="rebase")

        assert gh.merge_calls == [("acme/api", 100, "rebase")]
        assert not [e for e in events if e.kind == "method_fallback"]

    def test_backward_compatible_with_gh_ops_lacking_the_probe(self) -> None:
        # A pre-#1467 stub GhOps without branch_has_merge_commit at all must
        # keep working — getattr(..., None) fails closed, same as an
        # inconclusive read. A standalone class (not a FakeGh subclass) so
        # the method is genuinely absent, not merely deleted.
        class LegacyGh:
            def __init__(self) -> None:
                self.merge_calls: list[tuple[str, int, str]] = []

            def get_pr_size(self, repo: str, number: int) -> int:
                return 10

            def merge_pr(self, repo: str, number: int, method: str = "rebase"):
                self.merge_calls.append((repo, number, method))
                return True, "merged"

            def close_issue(self, repo: str, issue_number: int) -> None:
                pass

            def get_pr_body(self, repo: str, number: int) -> str:
                return ""

            def has_open_children(self, repo: str, issue_number: int) -> bool:
                return False

            def is_epic_issue(self, repo: str, issue_number: int) -> bool:
                return False

            def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
                return []

        items = [_q("a", pr=100, size=10)]
        gh = LegacyGh()
        events = process(items, gh, method="rebase")

        assert gh.merge_calls == [("acme/api", 100, "rebase")]
        assert not [e for e in events if e.kind == "method_fallback"]
        assert not hasattr(gh, "branch_has_merge_commit")

    def test_no_probe_when_method_is_not_rebase(self) -> None:
        # squash/merge never hit the "can't be rebased" refusal — no need
        # to spend a `gh api` round trip checking.
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(merge_commit_results={100: True})
        process(items, gh, method="squash")

        assert gh.merge_commit_calls == []
        assert gh.merge_calls == [("acme/api", 100, "squash")]


class TestProcessDryRunLinearityPreview:
    """#1467-review: `coord merge --dry-run` previews the review/smoke gates
    but, before this, never previewed the rebase→squash fallback — a
    dry-run over an entry already carrying a merge commit silently said
    "would merge ... via --rebase" even though the real run would fall
    back to --squash. Only reachable when the entry already has a
    pr_number (from an earlier non-dry-run attempt), since dry-run itself
    never opens a PR and the probe needs one to query.
    """

    def test_previews_squash_fallback_when_pr_already_exists(self) -> None:
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh(merge_commit_results={100: True})
        events = process(items, gh, method="rebase", dry_run=True)

        assert gh.merge_calls == []  # dry-run never actually merges
        fallback = [e for e in events if e.kind == "method_fallback"]
        assert len(fallback) == 1
        assert "dry run" in fallback[0].message
        assert "squash" in fallback[0].message
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "--squash" in merged[0].message

    def test_no_preview_without_a_prior_pr_number(self) -> None:
        # A brand-new entry has no pr_number yet in dry-run (dry-run never
        # creates one) — nothing to probe, so no fallback preview and the
        # merge preview reports the requested method unchanged.
        items = [_q("a", size=10)]
        gh = FakeGh(merge_commit_results={100: True})
        events = process(items, gh, method="rebase", dry_run=True)

        assert not [e for e in events if e.kind == "method_fallback"]
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "--rebase" in merged[0].message

    def test_fail_closed_on_inconclusive_probe_in_dry_run(self) -> None:
        items = [_q("a", pr=100, size=10)]
        gh = FakeGh()  # merge_commit_results defaults to {} -> None
        events = process(items, gh, method="rebase", dry_run=True)

        assert not [e for e in events if e.kind == "method_fallback"]
        merged = [e for e in events if e.kind == "merged"]
        assert merged and "--rebase" in merged[0].message


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
            if args[:2] == ("api", "graphql"):
                # #1354: the close-guard also does a live batch state
                # lookup; no live answer here, so it falls back to the
                # checkbox in epic_json above (#1039 unticked -> open).
                raise RuntimeError("gh api graphql: not available in this test")
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

    def test_commit_message_closes_epic_blocks_merge(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1318 acceptance criterion: a branch commit message containing
        `Closes #<epic>` is caught even though the PR body itself is clean
        — the #1196 body-lint alone can't see this (the actual #1314/#1120
        incident: the closing keyword sat in the *commit message*'s
        explanatory prose, not the PR body)."""
        from coord import github_ops as real_gh_ops

        epic_json = json.dumps({
            "number": 1120, "title": "Epic", "state": "open", "milestone": None,
            "labels": [{"name": "epic"}], "body": "",
        })
        calls: list[tuple[str, ...]] = []

        def fake_gh(*args: str) -> str:
            calls.append(args)
            if args[:2] == ("pr", "list"):
                return "[]"
            if args[:2] == ("pr", "create"):
                return "https://github.com/acme/api/pull/500"
            if args[:2] == ("pr", "view"):
                if args[-1] == "commits":
                    return json.dumps({"commits": [{
                        "messageHeadline": 'fix(#1314): harden downstream breakages',
                        "messageBody": (
                            "...its body carry \"Closes #1120\", which "
                            "GitHub's native closing-keyword auto-close "
                            "used to close the epic..."
                        ),
                    }]})
                return json.dumps({"body": "Automated merge from the coordinator."})
            if args[:2] == ("issue", "view"):
                return epic_json
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(real_gh_ops, "_gh", fake_gh)

        def _boom_subprocess(*a, **k):
            raise AssertionError(
                "must never reach a real `gh` subprocess call — the merge "
                "is refused before `gh pr merge`/`gh issue close`"
            )

        monkeypatch.setattr(real_gh_ops.subprocess, "run", _boom_subprocess)

        entry = _q("w1", repo="api", repo_github="acme/api", target="main", size=10)
        entry.issue_number = 200  # ordinary work issue; the epic only appears in the commit prose

        events = process([entry], real_gh_ops)

        assert entry.state == PENDING  # refused, never merged
        assert not any(a[:2] == ("pr", "merge") for a in calls)
        blocked = [e for e in events if e.kind == "epic_closing_keyword_in_commit"]
        assert blocked and "#1120" in blocked[0].message

    def test_commit_message_closes_ordinary_issue_merges_through(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Counterpart acceptance criterion from #1318: an ordinary `Closes
        #<non-epic>` in a commit message merges through untouched — the
        guard only fires for epic-labelled targets."""
        from coord import github_ops as real_gh_ops

        ordinary_json = json.dumps({
            "number": 55, "title": "Bug", "state": "open", "milestone": None,
            "labels": [], "body": "",
        })

        def fake_gh(*args: str) -> str:
            if args[:2] == ("pr", "list"):
                return "[]"
            if args[:2] == ("pr", "create"):
                return "https://github.com/acme/api/pull/500"
            if args[:2] == ("pr", "view"):
                if args[-1] == "commits":
                    return json.dumps({"commits": [{
                        "messageHeadline": "fix(#55): a normal bug fix",
                        "messageBody": "Closes #55",
                    }]})
                return json.dumps({"body": "Automated merge from the coordinator."})
            if args[:2] == ("issue", "view"):
                return ordinary_json
            if args[:2] == ("pr", "merge"):
                return "merged"
            raise AssertionError(f"unexpected gh call: {args}")

        monkeypatch.setattr(real_gh_ops, "_gh", fake_gh)

        class _FakeCompleted:
            returncode = 0
            stderr = ""

        def _fake_run(cmd, **kwargs):
            # `gh issue close` (#806's deterministic close path) shells out
            # via subprocess.run directly, not `_gh`.
            return _FakeCompleted()

        monkeypatch.setattr(real_gh_ops.subprocess, "run", _fake_run)

        entry = _q("w2", repo="api", repo_github="acme/api", target="main", size=10)
        entry.issue_number = 55

        events = process([entry], real_gh_ops)

        assert entry.state == MERGED
        assert not [e for e in events if "epic_closing_keyword" in e.kind]


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

    def test_process_populates_branch_patch_id_from_gh_ops(self) -> None:
        """#1475: process() must populate entry.branch_patch_id via
        gh_ops.get_branch_patch_id — the production population path."""
        patch_id_calls: list[tuple[str, str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                patch_id_calls.append((repo, base, branch))
                return "patchid-abc"

        cfg = self._config()
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        board = self._board(completed=[work, review])

        items = [_q("w1", size=10)]
        process(items, _TrackingGh(), config=cfg, board=board)

        assert len(patch_id_calls) >= 1, "process() must call gh_ops.get_branch_patch_id"
        assert patch_id_calls[0][2] == items[0].branch, "must fetch patch-id for the entry's branch"
        assert items[0].branch_patch_id == "patchid-abc"

    def test_process_skips_branch_patch_id_fetch_when_review_not_required(self) -> None:
        """#1475 (non-blocking review finding): has_approved_review never
        consults branch_patch_id unless a review is actually required for the
        entry, so process() must not spend a `gh api compare` round trip
        populating it in that case (gate disabled here via default_gates)."""
        patch_id_calls: list[tuple[str, str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                patch_id_calls.append((repo, base, branch))
                return "patchid-abc"

        cfg = self._config(gates=["merge"])  # "review" not in the effective gates
        work = self._work("w1")
        board = self._board(completed=[work])

        items = [_q("w1", size=10)]
        process(items, _TrackingGh(), config=cfg, board=board)

        assert patch_id_calls == [], "review not required — must not fetch branch_patch_id"
        assert items[0].branch_patch_id is None

    def test_process_skips_branch_patch_id_fetch_when_skip_review(self) -> None:
        """#1475 (non-blocking review finding): --skip-review means the review
        gate (and its patch-id check) is never consulted either."""
        patch_id_calls: list[tuple[str, str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                patch_id_calls.append((repo, base, branch))
                return "patchid-abc"

        cfg = self._config(gates=["review", "merge"])
        work = self._work("w1")
        board = self._board(completed=[work])

        items = [_q("w1", size=10)]
        process(items, _TrackingGh(), config=cfg, board=board, skip_review=True)

        assert patch_id_calls == [], "skip_review — must not fetch branch_patch_id"
        assert items[0].branch_patch_id is None

    def test_process_rebase_with_matching_patch_id_still_merges_end_to_end(self) -> None:
        """#1475: a rebase that moves the SHA but not the content must not
        force a re-review — the merge proceeds on the carried-forward approval."""
        cfg = self._config()
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "oldsha"
        review.review_patch_id = "patchid-same"

        class _RebasedGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                return "newsha"  # rebase moved the head

            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                return "patchid-same"  # but the content is byte-identical

        board = self._board(completed=[work, review])
        items = [_q("w1", size=10)]
        events = process(items, _RebasedGh(), config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "review_required" not in kinds, "content-identical rebase must not re-block review"
        assert "merged" in kinds, "approval must carry forward across a pure rebase"

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

    # ── #1475: patch-id carries an approval across a content-identical rebase ──

    def test_has_approved_review_matching_patch_id_survives_sha_move(self) -> None:
        """#1475: a pure rebase moves the SHA but not the content — the
        approval must still count when the patch-id matches."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-same"

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"  # SHA moved — a rebase happened
        entry.branch_patch_id = "patchid-same"  # but the diff is identical

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is True

    def test_has_approved_review_differing_patch_id_stays_stale(self) -> None:
        """#1475: a genuine content change must still void the approval even
        though both patch-ids are present."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-old"

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = "patchid-new"  # conflict resolution changed content

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is False

    def test_has_approved_review_missing_patch_id_fails_closed(self) -> None:
        """#1475: when the patch-id can't be computed on either side, the SHA
        mismatch alone must still void the approval (fail closed, not open)."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = None  # patch-id unavailable at review time

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None  # patch-id unavailable at merge time

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is False

    def test_has_approved_review_one_sided_patch_id_fails_closed(self) -> None:
        """#1475: a patch-id present on only one side must not be trusted."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-same"

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None  # merge-time fetch failed

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is False

    # ── #1506: compute branch_patch_id on demand instead of voiding ────────

    def test_has_approved_review_null_branch_patch_id_computed_via_gh_ops(self) -> None:
        """#1506: an entry whose approval predates #1475 (branch_patch_id
        never backfilled) must not be voided outright — when gh_ops is
        supplied, the current patch-id is computed on demand and, if it
        matches the review's, the approval still counts."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-same"

        entry = _q("w1", branch="worker/w1", target="main", repo_github="acme/api")
        entry.branch_head_sha = "def456"  # rebased — SHA moved
        entry.branch_patch_id = None      # never backfilled (pre-#1475 review)

        board = self._board(completed=[work, review])

        class _Gh:
            calls: list[tuple[str, str, str]] = []
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                self.calls.append((repo, base, branch))
                return "patchid-same"

        gh = _Gh()
        assert mq.has_approved_review(entry, board, gh) is True
        assert gh.calls == [("acme/api", "main", "worker/w1")]
        # #1506 acceptance: the computed value is backfilled so a later call
        # (e.g. process()'s own save_queue) persists it and never re-fetches.
        assert entry.branch_patch_id == "patchid-same"

    def test_has_approved_review_null_branch_patch_id_without_gh_ops_fails_closed(self) -> None:
        """Backward compatibility: callers that don't pass gh_ops (e.g.
        display_error, which is intentionally I/O-free) keep the pre-#1506
        fail-closed behaviour."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-same"

        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board) is False
        assert entry.branch_patch_id is None  # never touched — no gh_ops given

    def test_has_approved_review_computed_patch_id_still_voids_on_genuine_change(self) -> None:
        """#1506: computing the patch-id on demand must not turn into a
        rubber stamp — a genuinely different diff still voids the approval."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-old"

        entry = _q("w1", branch="worker/w1", target="main", repo_github="acme/api")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None

        class _Gh:
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                return "patchid-new"  # conflict resolution actually changed content

        assert mq.has_approved_review(entry, self._board(completed=[work, review]), _Gh()) is False

    def test_has_approved_review_computes_against_merge_base_not_baseRefOid(self) -> None:
        """#1506: the base passed for patch-id computation must be
        entry.target_branch (a branch name — GitHub's three-dot compare API
        resolves this to the true merge-base) and never a PR's recorded
        baseRefOid. This fixture makes the two diverge: computing against
        the (wrong) baseRefOid-like SHA yields a value that does NOT match
        the review's patch-id, while computing against the branch name
        (merge-base) yields the correct match — proving the verdict follows
        merge-base."""
        work = self._work("w1")
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-correct"

        entry = _q("w1", branch="worker/w1", target="main", repo_github="acme/api")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None

        stale_base_ref_oid = "0ldbaser3f0idsha"

        class _Gh:
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                if base == stale_base_ref_oid:
                    return "patchid-wrong-from-stale-base"
                if base == "main":  # entry.target_branch — the merge-base path
                    return "patchid-correct"
                return None

        board = self._board(completed=[work, review])
        assert mq.has_approved_review(entry, board, _Gh()) is True

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

    # ── #567: chain resolution when a fix worker has branch=NULL ──────────

    def test_has_approved_review_bounce_fix_null_branch_approves(self) -> None:
        """#567: a fix worker dispatched with branch=NULL (the #557 gap)
        still counts — the chain is reconstructed via
        review_of_assignment_id instead of branch equality."""
        orig_work = self._work("orig")
        fix_work = Assignment(
            machine_name="m1",
            repo_name="api",
            issue_number=1,
            issue_title="[fix-1] t",
            assignment_id="fix1",
            type="work",
            status="done",
            branch=None,  # #557 remote-interactive-rework gap
            review_of_assignment_id="orig",
        )
        re_review = self._review("fix1", verdict="approve")
        orig_review = self._review("orig", verdict="request-changes")
        board = self._board(completed=[orig_work, orig_review, fix_work, re_review])
        entry = _q("orig", branch="worker/orig")
        assert mq.has_approved_review(entry, board) is True

    def test_has_approved_review_entry_keyed_to_child_finds_parent_approval(
        self,
    ) -> None:
        """#1601: the backward-chain companion to the smoke-gate regression
        above, isolated the same way — no branch bridge at all (both rows'
        branches deliberately differ from the entry's), so ONLY the
        review_of_assignment_id id-chain can connect a CHILD-keyed entry back
        to an approval recorded against its PARENT. Before #1601 the walk was
        forward-only (a known parent pulled in its child) and could not
        resolve this direction."""
        orig_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", type="work", status="done",
            branch="worker/orig-real",
        )
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch=None, review_of_assignment_id="orig",
        )
        orig_review = self._review("orig", verdict="approve")
        board = self._board(completed=[orig_work, fix_work, orig_review])
        # Entry keyed to the CHILD; its own `.branch` matches neither row, so
        # branch equality contributes nothing — only the id-chain can bridge.
        entry = _q("fix1", branch="worker/entry-only")
        assert mq.has_approved_review(entry, board) is True

    def test_has_approved_review_multi_hop_null_branch_chain(self) -> None:
        """#567: a fix-of-a-fix chain (both branch=NULL) resolves via the
        fixed-point expansion, not just one hop."""
        orig_work = self._work("orig")
        fix1 = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch=None, review_of_assignment_id="orig",
        )
        fix2 = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-2] t", assignment_id="fix2", type="work",
            status="done", branch=None, review_of_assignment_id="fix1",
        )
        re_review = self._review("fix2", verdict="approve")
        board = self._board(completed=[orig_work, fix1, fix2, re_review])
        entry = _q("orig", branch="worker/orig")
        assert mq.has_approved_review(entry, board) is True

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


class TestScopedReviewCandidate:
    """#1476: find_scoped_review_candidate / only_conflict_fix_since_review —
    the pure-logic gate deciding whether a voided approval qualifies for a
    re-review SCOPED to the conflict-fix resolution delta instead of a full
    re-review of the whole PR."""

    @staticmethod
    def _board(active=None, completed=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str = "w1") -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done", branch=f"worker/{aid}",
        )

    @staticmethod
    def _review(
        of_aid: str, *, verdict: str | None = "approve", status: str = "done",
        head_sha: str | None = "abc123", patch_id: str | None = "patchid-old",
        dispatched_at: float = 100.0,
    ) -> Assignment:
        return Assignment(
            machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=f"rev-{of_aid}", type="review", status=status,
            review_of_assignment_id=of_aid, review_verdict=verdict,
            review_head_sha=head_sha, review_patch_id=patch_id,
            dispatched_at=dispatched_at,
        )

    @staticmethod
    def _conflict_fix(
        merge_entry_id: str, *, status: str = "done", dispatched_at: float = 200.0,
    ) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[conflict-fix] t", assignment_id="cf1",
            type="conflict-fix", status=status,
            review_of_assignment_id=merge_entry_id, dispatched_at=dispatched_at,
        )

    def _voided_entry(self) -> QueuedMerge:
        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = "patchid-new"
        return entry

    # ── find_scoped_review_candidate ───────────────────────────────────────

    def test_finds_candidate_on_patch_id_mismatch(self) -> None:
        work = self._work("w1")
        review = self._review("w1")
        board = self._board(completed=[work, review])
        entry = self._voided_entry()
        found = mq.find_scoped_review_candidate(entry, board)
        assert found is review

    def test_returns_none_when_content_identical(self) -> None:
        """#1475 already carries this approval forward — nothing to scope."""
        work = self._work("w1")
        review = self._review("w1", patch_id="patchid-same")
        board = self._board(completed=[work, review])
        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = "patchid-same"
        assert mq.find_scoped_review_candidate(entry, board) is None

    def test_returns_none_when_sha_unchanged(self) -> None:
        work = self._work("w1")
        review = self._review("w1", head_sha="abc123")
        board = self._board(completed=[work, review])
        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "abc123"  # nothing moved
        entry.branch_patch_id = "patchid-old"
        assert mq.find_scoped_review_candidate(entry, board) is None

    def test_returns_none_when_review_patch_id_missing(self) -> None:
        """Fail closed — an unconfirmable diff gets a full review."""
        work = self._work("w1")
        review = self._review("w1", patch_id=None)
        board = self._board(completed=[work, review])
        entry = self._voided_entry()
        assert mq.find_scoped_review_candidate(entry, board) is None

    def test_returns_none_when_current_patch_id_missing(self) -> None:
        work = self._work("w1")
        review = self._review("w1")
        board = self._board(completed=[work, review])
        entry = _q("w1", branch="worker/w1")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None
        assert mq.find_scoped_review_candidate(entry, board) is None

    def test_computes_current_patch_id_via_gh_ops_when_null(self) -> None:
        """#1506: when gh_ops is supplied, a null branch_patch_id is computed
        on demand (same as has_approved_review) instead of bailing out
        immediately — so a genuinely-voided pre-#1475 approval can still be
        scoped to the conflict-fix delta rather than falling to a full
        re-review."""
        work = self._work("w1")
        review = self._review("w1", patch_id="patchid-old")
        board = self._board(completed=[work, review])
        entry = _q("w1", branch="worker/w1", target="main", repo_github="acme/api")
        entry.branch_head_sha = "def456"
        entry.branch_patch_id = None

        class _Gh:
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                assert (repo, base, branch) == ("acme/api", "main", "worker/w1")
                return "patchid-new"

        found = mq.find_scoped_review_candidate(entry, board, _Gh())
        assert found is review
        assert entry.branch_patch_id == "patchid-new"  # backfilled, computed once

    def test_returns_none_when_no_review_at_all(self) -> None:
        work = self._work("w1")
        board = self._board(completed=[work])
        entry = self._voided_entry()
        assert mq.find_scoped_review_candidate(entry, board) is None

    def test_returns_none_when_verdict_was_request_changes(self) -> None:
        work = self._work("w1")
        review = self._review("w1", verdict="request-changes")
        board = self._board(completed=[work, review])
        entry = self._voided_entry()
        assert mq.find_scoped_review_candidate(entry, board) is None

    # ── only_conflict_fix_since_review ──────────────────────────────────────

    def test_true_when_only_a_conflict_fix_intervened(self) -> None:
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("w1", dispatched_at=200.0)
        board = self._board(completed=[work, review, cf])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is True

    def test_false_when_no_conflict_fix_found(self) -> None:
        """Nothing to attribute the content change to — fail closed."""
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        board = self._board(completed=[work, review])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_false_when_a_fix_round_also_ran_after_the_review(self) -> None:
        """Guardrail: any other new commit ⇒ full review, not scoped."""
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("w1", dispatched_at=200.0)
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch="worker/w1",
            review_of_assignment_id="w1", dispatched_at=250.0,
        )
        board = self._board(completed=[work, review, cf, fix_work])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_conflict_fix_before_review_not_relevant(self) -> None:
        """A conflict-fix that ran BEFORE this review doesn't count as the
        source of the post-approval content change — fail closed."""
        work = self._work("w1")
        review = self._review("w1", dispatched_at=300.0)
        cf = self._conflict_fix("w1", dispatched_at=100.0)  # earlier
        board = self._board(completed=[work, review, cf])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_ignores_conflict_fix_for_a_different_entry(self) -> None:
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("other-entry", dispatched_at=200.0)
        board = self._board(completed=[work, review, cf])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_ignores_failed_conflict_fix(self) -> None:
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("w1", status="failed", dispatched_at=200.0)
        board = self._board(completed=[work, review, cf])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_fix_round_before_review_does_not_disqualify(self) -> None:
        """A fix round that ran BEFORE the review (and so is exactly what the
        review covered) must not itself disqualify the scoped path."""
        earlier_fix = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch="worker/w1",
            review_of_assignment_id="w1", dispatched_at=50.0,
        )
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("w1", dispatched_at=200.0)
        board = self._board(completed=[work, earlier_fix, review, cf])
        entry = self._voided_entry()
        assert mq.only_conflict_fix_since_review(entry, board, review) is True

    # ── intervening_work_since_review ───────────────────────────────────────
    # #1488: `coord review-reaffirm` needs to tell only_conflict_fix_since_
    # review's two distinct False reasons apart — "a new work/fix round landed"
    # (hard refuse) vs "no conflict-fix explains the delta" (warn, the
    # hand-run-rebase case the escape hatch exists for).

    def test_intervening_empty_when_only_a_conflict_fix_ran(self) -> None:
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        cf = self._conflict_fix("w1", dispatched_at=200.0)
        board = self._board(completed=[work, review, cf])
        entry = self._voided_entry()
        assert mq.intervening_work_since_review(entry, board, review) == []

    def test_intervening_empty_when_nothing_at_all_ran(self) -> None:
        """The hand-run-rebase case: unattributable, but NOT new logic."""
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        board = self._board(completed=[work, review])
        entry = self._voided_entry()
        assert mq.intervening_work_since_review(entry, board, review) == []
        assert mq.only_conflict_fix_since_review(entry, board, review) is False

    def test_intervening_lists_a_fix_round_dispatched_after_the_review(self) -> None:
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch="worker/w1",
            review_of_assignment_id="w1", dispatched_at=250.0,
        )
        board = self._board(completed=[work, review, fix_work])
        entry = self._voided_entry()
        got = mq.intervening_work_since_review(entry, board, review)
        assert [a.assignment_id for a in got] == ["fix1"]

    def test_intervening_ignores_work_dispatched_before_the_review(self) -> None:
        earlier_fix = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch="worker/w1",
            review_of_assignment_id="w1", dispatched_at=50.0,
        )
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        board = self._board(completed=[work, earlier_fix, review])
        entry = self._voided_entry()
        assert mq.intervening_work_since_review(entry, board, review) == []

    def test_intervening_ignores_work_on_another_branch(self) -> None:
        other = Assignment(
            machine_name="m1", repo_name="api", issue_number=9, issue_title="t",
            assignment_id="w-other", type="work", status="done",
            branch="worker/other", dispatched_at=250.0,
        )
        work = self._work("w1")
        review = self._review("w1", dispatched_at=100.0)
        board = self._board(completed=[work, review, other])
        entry = self._voided_entry()
        assert mq.intervening_work_since_review(entry, board, review) == []

    def test_intervening_empty_when_review_has_no_dispatch_time(self) -> None:
        """No dispatch anchor ⇒ nothing is provably "after" ⇒ empty (matches
        only_conflict_fix_since_review's own pre-#1488 posture)."""
        work = self._work("w1")
        review = self._review("w1")
        review.dispatched_at = None
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch="worker/w1",
            review_of_assignment_id="w1", dispatched_at=250.0,
        )
        board = self._board(completed=[work, review, fix_work])
        entry = self._voided_entry()
        assert mq.intervening_work_since_review(entry, board, review) == []


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

    def test_has_smoke_verdict_bounce_fix_null_branch_counts(self) -> None:
        """#567: fix-work with branch=NULL (the #557 gap) still satisfies the
        gate — resolved via review_of_assignment_id instead of branch."""
        orig_work = self._work("orig", test_state=None)
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix] t",
            assignment_id="fix1", type="work", status="done",
            branch=None,  # #557 remote-interactive-rework gap
            review_of_assignment_id="orig",
            test_state="passed",
        )
        board = self._board(completed=[orig_work, fix_work])
        entry = _q("orig", branch="worker/orig")
        assert mq.has_smoke_verdict(entry, board) is True

    def test_has_smoke_verdict_entry_keyed_to_child_finds_parent_verdict(self) -> None:
        """#1601: the #567 chain walk was forward-only — a known PARENT
        pulled in its child, but an entry keyed to the CHILD (the fix round,
        e.g. after #292's re-keying) could not walk *backward* to reach a
        parent whose own smoke/test verdict is the only one on the branch —
        exactly the #1566 incident shape (a fix round approved by review but
        never re-tested). Isolated with NO branch bridge at all (both rows'
        branches deliberately differ from the entry's) so only the
        review_of_assignment_id id-chain can connect them — the chain must
        be symmetric: it should not matter which round it's keyed to."""
        orig_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="orig", type="work", status="done",
            branch="worker/orig-real", test_state="passed",
        )
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="[fix] t",
            assignment_id="fix1", type="work", status="done",
            branch=None,  # #557 remote-interactive-rework gap
            review_of_assignment_id="orig",
            test_state=None,  # the fix round never re-ran its own test/smoke
        )
        board = self._board(completed=[orig_work, fix_work])
        # Entry keyed to the CHILD (fix1) — the #292 re-key direction. Its own
        # `.branch` matches neither row, so branch equality contributes
        # nothing — only the id-chain can bridge to orig_work's verdict.
        entry = _q("fix1", branch="worker/entry-only")
        assert mq.has_smoke_verdict(entry, board) is True

    # ── #1479: test-verdict staleness (base-moved vs content-changed) ──

    def test_has_smoke_verdict_stale_when_base_moved(self) -> None:
        """Base moved, branch diff identical → test verdict is stale even
        though the branch's own content fingerprint didn't change."""
        work = self._work("w1", test_state="passed")
        work.test_head_sha = "branch-sha-1"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "main-sha-old"
        board = self._board(completed=[work])

        entry = _q("w1")
        entry.branch_head_sha = "branch-sha-1"       # unchanged
        entry.branch_patch_id = "patch-1"             # unchanged — identical content
        entry.target_branch_head_sha = "main-sha-new"  # main advanced since the test ran

        assert mq.has_smoke_verdict(entry, board) is False

    def test_has_smoke_verdict_stale_when_branch_content_changed(self) -> None:
        """Branch content changed (new commit) → test verdict is stale."""
        work = self._work("w1", test_state="passed")
        work.test_head_sha = "branch-sha-1"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "main-sha-1"
        board = self._board(completed=[work])

        entry = _q("w1")
        entry.branch_head_sha = "branch-sha-2"    # new commit pushed
        entry.branch_patch_id = "patch-2"          # content actually changed
        entry.target_branch_head_sha = "main-sha-1"  # base unchanged

        assert mq.has_smoke_verdict(entry, board) is False

    def test_has_smoke_verdict_fresh_when_neither_moved(self) -> None:
        """Base unchanged, branch content unchanged → verdict still counts."""
        work = self._work("w1", test_state="passed")
        work.test_head_sha = "branch-sha-1"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "main-sha-1"
        board = self._board(completed=[work])

        entry = _q("w1")
        entry.branch_head_sha = "branch-sha-1"
        entry.branch_patch_id = "patch-1"
        entry.target_branch_head_sha = "main-sha-1"

        assert mq.has_smoke_verdict(entry, board) is True

    def test_has_smoke_verdict_fresh_across_content_identical_rebase(self) -> None:
        """SHA moved but the diff didn't (a clean rebase that replayed onto
        the *same* base tip) — falls back to the patch-id match, same as the
        review gate's #1475 behaviour."""
        work = self._work("w1", test_state="passed")
        work.test_head_sha = "branch-sha-1"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "main-sha-1"
        board = self._board(completed=[work])

        entry = _q("w1")
        entry.branch_head_sha = "branch-sha-2"    # commit SHA changed...
        entry.branch_patch_id = "patch-1"          # ...but content is identical
        entry.target_branch_head_sha = "main-sha-1"

        assert mq.has_smoke_verdict(entry, board) is True

    def test_has_smoke_verdict_missing_anchors_fails_open(self) -> None:
        """Rows predating #1479 (no test_base_sha/test_head_sha captured)
        skip the staleness check entirely — same backward-compat contract as
        #821/#1475 for the review gate."""
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])

        entry = _q("w1")
        entry.branch_head_sha = "branch-sha-2"
        entry.branch_patch_id = "patch-2"
        entry.target_branch_head_sha = "main-sha-new"

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

    def test_process_gate_a_test_author_skipped_verdict_merges_despite_moved_base(
        self,
    ) -> None:
        """#1732 acceptance: a Gate-A test-author slice recorded `skipped`
        ("contract/fixture-only diff, nothing to smoke-test" — #1076/#1152)
        must merge on its own — no `--skip-smoke`, no human — even though a
        sibling merge has since moved the target branch out from under the
        recorded anchor. This is the unattended oracle-loop path #1732 was
        filed to unblock: `skipped` is a structural statement about the
        diff's shape, not a measurement at a SHA, so it cannot go stale."""
        cfg = self._config()
        work = self._work("w1", test_state="skipped")
        work.type = "test-author"
        work.test_head_sha = "branch-sha"
        work.test_patch_id = "patch-1"
        work.test_base_sha = "base-old"
        board = self._board(completed=[work])
        items = [_q("w1", size=10, assignment_type="test-author")]

        class _MovedBaseGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                return "base-new" if branch == "main" else "branch-sha"

            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                return "patch-1"

        events = process(items, _MovedBaseGh(), config=cfg, board=board)

        kinds = [e.kind for e in events]
        assert "smoke_required" not in kinds, (
            "a `skipped` verdict must never be treated as #1479-stale (#1732)"
        )
        assert "merged" in kinds
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

    # ── #1479-review: target_branch_head_sha population ──

    def test_process_populates_target_branch_head_sha_from_gh_ops(self) -> None:
        """#1479: process() must populate entry.target_branch_head_sha via
        gh_ops.get_branch_sha(target_branch) — the production population path
        has_smoke_verdict's base-moved staleness check relies on."""
        sha_calls: list[tuple[str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                sha_calls.append((repo, branch))
                return "main-sha-123"

        cfg = self._config()
        work = self._work("w1", test_state="passed")
        board = self._board(completed=[work])
        items = [_q("w1", size=10)]
        process(items, _TrackingGh(), config=cfg, board=board)

        assert ("acme/api", "main") in sha_calls, (
            "process() must call gh_ops.get_branch_sha for the target branch"
        )
        assert items[0].target_branch_head_sha == "main-sha-123"

    def test_process_hoists_target_branch_head_sha_fetch_per_group(self) -> None:
        """#1479-review (non-blocking): entries grouped under the same
        (repo_github, target_branch) share an identical target_branch_head_sha
        — process() must fetch it once per group, not once per entry."""
        sha_calls: list[tuple[str, str]] = []

        class _TrackingGh(FakeGh):
            def get_branch_sha(self, repo: str, branch: str) -> str | None:
                sha_calls.append((repo, branch))
                return "main-sha-shared"

        cfg = self._config()
        w1 = self._work("w1", test_state="passed")
        w2 = self._work("w2", test_state="passed")
        board = self._board(completed=[w1, w2])
        items = [
            _q("w1", branch="worker/w1", size=10),
            _q("w2", branch="worker/w2", size=20),
        ]
        process(items, _TrackingGh(), config=cfg, board=board)

        target_calls = [c for c in sha_calls if c == ("acme/api", "main")]
        assert len(target_calls) == 1, (
            "target_branch_head_sha must be fetched once per group, "
            f"got {len(target_calls)} calls: {sha_calls}"
        )
        assert items[0].target_branch_head_sha == "main-sha-shared"
        assert items[1].target_branch_head_sha == "main-sha-shared"


class TestGateBypassAudit:
    """#1213: a per-issue label override honoured by requires_review /
    requires_smoke merges without the bypassed gate(s), and every bypass
    writes a ``gate_bypassed`` business-tier audit row + a CLI-visible note
    on the "merged" event — never silent."""

    @staticmethod
    def _config(*, default_gates=None, labels=None, reviews_enabled=True):
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
        cfg.reviews.enabled = reviews_enabled
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

    def test_reviews_globally_disabled_does_not_report_phantom_review_bypass(
        self, coord_db
    ) -> None:
        # Review finding #1: when config.reviews.enabled is False, review was
        # never going to be required regardless of the label — requires_review
        # already returns False unconditionally. A ["merge"]-only label drops
        # "review" from the resolved gate list too, but that changes nothing
        # about enforcement, so it must NOT be reported as a bypassed gate
        # (only "test" is a real bypass here).
        cfg = self._config(labels={"gate:trivial": ["merge"]}, reviews_enabled=False)
        board = self._board()  # no review, no smoke verdict anywhere
        items = [_q("a", required_gates=["merge"])]
        events = process(items, FakeGh(), config=cfg, board=board)

        assert items[0].state == MERGED
        merged = [e for e in events if e.kind == "merged"]
        assert merged
        assert "gate bypass" in merged[0].message
        assert "test" in merged[0].message
        assert "review" not in merged[0].message

        rows = self._audit_rows(coord_db)
        assert len(rows) == 1
        details = json.loads(rows[0]["details_json"])
        assert details["bypassed_gates"] == ["test"]
        assert "review" not in details["bypassed_gates"]

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


class TestGroupBranchCandidates:
    """#1490: a fix/bounce cycle piles up more than one WORK_LIKE_TYPES row
    on the same branch (the original dispatch + every retry) —
    group_branch_candidates resolves each branch to a single winner instead
    of every caller processing (and re-announcing) every row."""

    @staticmethod
    def _work(
        aid: str,
        *,
        branch: str = "issue-1-fix",
        test_state: str | None = None,
        dispatched_at: float | None = None,
        repo: str = "api",
        status: str = "done",
        atype: str = "work",
    ) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name=repo, issue_number=1, issue_title="t",
            assignment_id=aid, type=atype, status=status, branch=branch,
            test_state=test_state, dispatched_at=dispatched_at,
        )

    def test_single_row_is_its_own_winner(self) -> None:
        a = self._work("a1")
        result = mq.group_branch_candidates([a])
        assert result == [(a, [])]

    def test_three_rows_one_branch_resolve_to_latest_passed(self) -> None:
        """The #1445 scenario verbatim: one failed test_state, two passed —
        the latest-dispatched *passed* row wins; the other two are
        superseded."""
        failed = self._work("31bd30875eb3", test_state="failed", dispatched_at=1000)
        passed1 = self._work("12fced1dfa80", test_state="passed", dispatched_at=2000)
        passed2 = self._work("5ed99d1f7edf", test_state="passed", dispatched_at=3000)
        result = mq.group_branch_candidates([failed, passed1, passed2])

        assert len(result) == 1
        winner, superseded = result[0]
        assert winner is passed2
        assert {id(x) for x in superseded} == {id(failed), id(passed1)}

    def test_falls_back_to_latest_overall_when_none_passed(self) -> None:
        """The branch is still mid-cycle (nothing has passed yet) — it must
        still resolve to a single winner (the most recent row) rather than
        disappearing entirely."""
        a1 = self._work("a1", dispatched_at=1000)
        a2 = self._work("a2", dispatched_at=2000)
        winner, superseded = mq.group_branch_candidates([a1, a2])[0]
        assert winner is a2
        assert superseded == [a1]

    def test_distinct_branches_are_separate_groups(self) -> None:
        a1 = self._work("a1", branch="issue-1-fix")
        a2 = self._work("a2", branch="issue-2-fix")
        result = mq.group_branch_candidates([a1, a2])
        assert len(result) == 2
        assert {w.assignment_id for w, _ in result} == {"a1", "a2"}
        assert all(superseded == [] for _, superseded in result)

    def test_filters_non_work_like_and_incomplete_rows(self) -> None:
        review = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="r1", type="review", status="done", branch="issue-1-fix",
        )
        not_done = self._work("nd", status="running")
        no_branch = self._work("nb", branch=None)
        no_aid = self._work("", branch="issue-1-fix")
        result = mq.group_branch_candidates([review, not_done, no_branch, no_aid])
        assert result == []

    def test_mock_author_and_test_author_are_grouped_too(self) -> None:
        """#930/#1141: WORK_LIKE_TYPES is 'work', 'mock-author', 'test-author'
        — all three flow through the same auto-enqueue path and must be
        grouped the same way."""
        ma = self._work("ma1", atype="mock-author", branch="ms-5-gate-a")
        ta = self._work("ta1", atype="test-author", branch="ms-37-test-author")
        result = mq.group_branch_candidates([ma, ta])
        assert len(result) == 2

    def test_order_is_stable_first_seen(self) -> None:
        a1 = self._work("a1", branch="issue-1-fix")
        b1 = self._work("b1", branch="issue-2-fix")
        a2 = self._work("a2", branch="issue-1-fix")
        result = mq.group_branch_candidates([a1, b1, a2])
        assert [w.branch for w, _ in result] == ["issue-1-fix", "issue-2-fix"]


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


class TestReconcileConflictEntries:
    """#1477: a CONFLICT entry re-tests its cached verdict on every tick
    instead of trusting the `gh pr merge` failure recorded whenever the
    queue last attempted it."""

    def test_clears_conflict_when_pr_now_mergeable(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = "gh pr merge 1464 ... --rebase failed: X Pull request #1464 is not mergeable"
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert len(events) == 1
        assert events[0].kind == "reopened"
        reloaded = load_queue()[0]
        assert reloaded.state == PENDING
        assert reloaded.error is None
        assert gh.mergeable_calls == [("acme/api", 100)]

    def test_leaves_entry_parked_when_still_conflicting(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = "not mergeable"
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: False})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        reloaded = load_queue()[0]
        assert reloaded.state == CONFLICT
        assert reloaded.error == "not mergeable"

    def test_leaves_entry_parked_when_mergeability_unknown(self, coord_db) -> None:
        """Fail-closed: `None` (gh error / GitHub still computing) must never
        be treated as a green light to unpark an entry."""
        entry = _q("a", state=CONFLICT, pr=100)
        save_queue([entry])

        gh = FakeGh()  # mergeable_results defaults to {} -> None
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert load_queue()[0].state == CONFLICT

    def test_skips_entry_with_no_pr_number(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=None)
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert gh.mergeable_calls == []
        assert load_queue()[0].state == CONFLICT

    def test_only_touches_conflict_entries(self, coord_db) -> None:
        """PENDING/MERGED/HUMAN_REQUIRED entries are never re-tested."""
        pending = _q("p", state=PENDING, pr=200)
        merged = _q("m", state=MERGED, pr=201)
        human = _q("h", state=mq.HUMAN_REQUIRED, pr=202)
        save_queue([pending, merged, human])

        gh = FakeGh(mergeable_results={200: True, 201: True, 202: True})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert gh.mergeable_calls == []
        states = {x.assignment_id: x.state for x in load_queue()}
        assert states == {"p": PENDING, "m": MERGED, "h": mq.HUMAN_REQUIRED}

    def test_gh_exception_does_not_wedge_the_tick(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        save_queue([entry])

        class RaisingGh(FakeGh):
            def check_pr_mergeable(self, repo: str, number: int) -> bool | None:
                raise RuntimeError("gh timeout")

        events = mq.reconcile_conflict_entries(RaisingGh())
        assert events == []
        assert load_queue()[0].state == CONFLICT

    def test_multiple_conflict_entries_reconciled_independently(self, coord_db) -> None:
        clean = _q("clean", state=CONFLICT, pr=100)
        still_broken = _q("broken", state=CONFLICT, pr=101)
        save_queue([clean, still_broken])

        gh = FakeGh(mergeable_results={100: True, 101: False})
        events = mq.reconcile_conflict_entries(gh)

        assert [e.entry.assignment_id for e in events] == ["clean"]
        states = {x.assignment_id: x.state for x in load_queue()}
        assert states == {"clean": PENDING, "broken": CONFLICT}


class TestReconcileConflictEntriesRebaseRefusalGuard:
    """#1467: a `mergeable: MERGEABLE` verdict is not evidence that a
    *rebase* merge will succeed — GitHub reports a branch carrying a merge
    commit as MERGEABLE right up until `gh pr merge --rebase` refuses it.
    An entry parked on that specific refusal must not unpark on the
    mergeable check alone; it also needs confirmation the branch has
    actually gone linear."""

    _REBASE_REFUSAL = "GraphQL: This branch can't be rebased (mergePullRequest)"

    def test_stays_parked_while_merge_commit_persists(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True}, merge_commit_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert load_queue()[0].state == CONFLICT
        assert gh.merge_commit_calls == [("acme/api", 100)]

    def test_stays_parked_when_merge_commit_probe_is_inconclusive(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        # merge_commit_results defaults to {} -> None (fail-closed).
        gh = FakeGh(mergeable_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert load_queue()[0].state == CONFLICT

    def test_stays_parked_when_gh_ops_lacks_the_probe(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        class NoProbeGh(FakeGh):
            branch_has_merge_commit = None  # simulate a pre-#1467 stub

        gh = NoProbeGh(mergeable_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert events == []
        assert load_queue()[0].state == CONFLICT

    def test_unparks_once_branch_is_confirmed_linear(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True}, merge_commit_results={100: False})
        events = mq.reconcile_conflict_entries(gh)

        assert [e.kind for e in events] == ["reopened"]
        reloaded = load_queue()[0]
        assert reloaded.state == PENDING
        assert reloaded.error is None

    def test_plain_conflict_unaffected_never_probes_merge_commit(self, coord_db) -> None:
        # A content conflict (not a rebase refusal) keeps the pre-#1467
        # mergeable-only behaviour untouched — the extra probe never fires.
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = "Pull request #100 is not mergeable"
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True})
        events = mq.reconcile_conflict_entries(gh)

        assert [e.kind for e in events] == ["reopened"]
        assert gh.merge_commit_calls == []
        assert load_queue()[0].state == PENDING


class TestReconcileOscillationRegression:
    """#1467 regression: an entry parked on a rebase-refusal whose PR
    reports MERGEABLE must not endlessly unpark -> retry -> re-park across
    ticks. Drives reconcile across multiple passes and (separately)
    verifies the terminal, merged outcome once the branch actually becomes
    linear."""

    _REBASE_REFUSAL = "GraphQL: This branch can't be rebased (mergePullRequest)"

    def test_does_not_oscillate_across_repeated_ticks(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        # Worst case for the old behaviour: GitHub always reports
        # MERGEABLE (true of a merge-commit branch) and the merge commit
        # never resolves (e.g. no conflict-fix worker landed yet).
        gh = FakeGh(mergeable_results={100: True}, merge_commit_results={100: True})

        for tick in range(3):
            events = mq.reconcile_conflict_entries(gh)
            assert events == [], f"tick {tick}: entry unparked despite unresolved merge commit"
            assert load_queue()[0].state == CONFLICT

    def test_reaches_terminal_merged_state_once_branch_goes_linear(self, coord_db) -> None:
        entry = _q("a", state=CONFLICT, pr=100, size=10)
        entry.error = self._REBASE_REFUSAL
        save_queue([entry])

        gh = FakeGh(mergeable_results={100: True}, merge_commit_results={100: True})

        # Pass 1: still has a merge commit -> stays parked, no wasted merge
        # attempt or misleading "conflict cleared" event.
        assert mq.reconcile_conflict_entries(gh) == []
        assert load_queue()[0].state == CONFLICT

        # A conflict-fix worker (or a human) rebases the branch onto main —
        # the merge commit is gone.
        gh.merge_commit_results[100] = False

        # Pass 2: now unparks...
        events = mq.reconcile_conflict_entries(gh)
        assert [e.kind for e in events] == ["reopened"]
        items = load_queue()
        assert items[0].state == PENDING

        # ...and the next merge pass succeeds cleanly via --rebase — a
        # terminal state, not another park.
        merge_events = mq.process(items, gh, method="rebase")
        assert items[0].state == MERGED
        assert not [e for e in merge_events if e.kind == "conflict"]


class TestResolveEntryKey:
    """#1477: --only/--drop accept a durable 'repo#issue' key in addition to
    a raw assignment_id, since the id mints fresh across a drop + re-enqueue
    cycle and can silently stop matching what an operator last saw."""

    def test_resolves_exact_assignment_id(self, coord_db) -> None:
        items = [_q("aid1"), _q("aid2")]
        assert mq.resolve_entry_key(items, "aid2") is items[1]

    def test_resolves_durable_repo_issue_key(self, coord_db) -> None:
        entry = QueuedMerge(
            assignment_id="aee6301971bf", repo_name="api", repo_github="acme/api",
            branch="issue-1461-fix", target_branch="main", issue_number=1461,
            issue_title="t", state=PENDING,
        )
        items = [entry]
        assert mq.resolve_entry_key(items, "api#1461") is entry
        assert mq.resolve_entry_key(items, "acme/api#1461") is entry

    def test_survives_drop_and_reenqueue_with_new_assignment_id(self, coord_db) -> None:
        """The exact bug in #1477: the id changes across drop + re-enqueue,
        but the durable key still finds the (new) row."""
        original = QueuedMerge(
            assignment_id="292740800331", repo_name="api", repo_github="acme/api",
            branch="issue-1461-fix", target_branch="main", issue_number=1461,
            issue_title="t", state=CONFLICT,
        )
        save_queue([original])
        assert mq.drop_entry("api#1461") is True
        assert load_queue() == []

        # Re-enqueue mints a fresh assignment id for the same branch/issue.
        retry = Assignment(
            machine_name="m", repo_name="api", issue_number=1461, issue_title="t",
            assignment_id="aee6301971bf", branch="issue-1461-fix", status="done",
        )
        enqueue(retry, repo_github="acme/api", target_branch="main")

        resolved = mq.resolve_entry_key(load_queue(), "api#1461")
        assert resolved is not None
        assert resolved.assignment_id == "aee6301971bf"

    def test_returns_none_when_no_match(self, coord_db) -> None:
        items = [_q("aid1")]
        assert mq.resolve_entry_key(items, "nonexistent") is None
        assert mq.resolve_entry_key(items, "api#9999") is None

    def test_does_not_fuzzy_match_plain_ids(self, coord_db) -> None:
        """A plain id with no '#' must never fall through to a durable-key
        scan — only an exact assignment_id match is attempted."""
        items = [_q("aid")]
        assert mq.resolve_entry_key(items, "ai") is None

    def test_ambiguous_durable_key_prefers_most_recent(self, coord_db) -> None:
        old = _q("old-aid", state=MERGED)
        old.issue_number = 1461
        new = _q("new-aid", state=PENDING)
        new.issue_number = 1461
        items = [old, new]
        assert mq.resolve_entry_key(items, "api#1461") is new

    # ── #1490: bare issue number + branch-name fallbacks ────────────────────

    def test_resolves_bare_issue_number(self, coord_db) -> None:
        entry = _q("aid1")
        entry.issue_number = 1461
        items = [entry]
        assert mq.resolve_entry_key(items, "1461") is entry

    def test_resolves_branch_name(self, coord_db) -> None:
        entry = _q("aid1", branch="issue-1461-fix")
        items = [entry]
        assert mq.resolve_entry_key(items, "issue-1461-fix") is entry

    def test_branch_resolves_even_when_assignment_id_was_rekeyed(self, coord_db) -> None:
        """#1490's actual failure mode: an operator reads assignment_id
        'X' off the board, but a concurrent auto-enqueue tick re-keys the
        entry to 'Y' before ``--only X`` runs. The stale id now matches
        nothing — but the branch, which never changes for the life of the
        entry, still resolves it."""
        entry = _q("Y", branch="issue-1445-fix")
        items = [entry]
        assert mq.resolve_entry_key(items, "X") is None  # the stale id: a hard miss
        assert mq.resolve_entry_key(items, "issue-1445-fix") is entry  # the stable fallback

    def test_bare_issue_number_takes_priority_over_a_coincidental_branch_name(
        self, coord_db
    ) -> None:
        """When a numeric key resolves via the issue-number form, that match
        wins outright — the branch fallback is never even consulted."""
        decoy = _q("aid1", branch="1461")
        decoy.issue_number = 9999
        target = _q("aid2")
        target.issue_number = 1461
        items = [decoy, target]
        assert mq.resolve_entry_key(items, "1461") is target

    def test_assignment_id_takes_priority_over_issue_number_and_branch(
        self, coord_db
    ) -> None:
        exact = _q("1461")  # assignment_id happens to look numeric
        exact.issue_number = 42
        other = _q("aid2")
        other.issue_number = 1461
        items = [exact, other]
        assert mq.resolve_entry_key(items, "1461") is exact

    def test_ambiguous_bare_issue_number_prefers_most_recent(self, coord_db) -> None:
        old = _q("old-aid", state=MERGED)
        old.issue_number = 1461
        new = _q("new-aid", state=PENDING)
        new.issue_number = 1461
        items = [old, new]
        assert mq.resolve_entry_key(items, "1461") is new


class TestDropEntryDurableKey:
    """#1477: drop_entry() resolves the durable 'repo#issue' form too."""

    def test_drops_by_durable_key(self, coord_db) -> None:
        entry = _q("aid1")
        entry.issue_number = 42
        save_queue([entry])
        assert mq.drop_entry("api#42") is True
        assert load_queue() == []

    def test_returns_false_when_durable_key_has_no_match(self, coord_db) -> None:
        save_queue([_q("aid1")])
        assert mq.drop_entry("api#9999") is False
        assert len(load_queue()) == 1


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

    # ── #934 milestone-aware target_branch ──────────────────────────────────

    @staticmethod
    def _config_with_develop_branch(*, develop_branch: str | None = "develop"):
        """Same shape as ``_config`` but the repo stand-in also carries
        ``develop_branch`` — #934's opt-in git model."""
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
            develop_branch: str | None = None

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)
            _repos: list = dc_field(default_factory=lambda: [_Repo(develop_branch=develop_branch)])

            def repo(self, name: str):
                return next((r for r in self._repos if r.name == name), None)

        cfg = _Cfg()
        cfg.pipeline.default_gates = ["review", "test", "merge"]
        return cfg

    def test_targets_feature_branch_for_opted_in_repo_with_milestone(self, coord_db) -> None:
        """#934 review should-fix: the "merge targets the right base" seam
        the issue explicitly asked for — enqueue_approved_work must resolve
        target_branch to feature/ms-NN when the repo opted into the git
        model and the issue belongs to a milestone, not hardcode
        default_branch."""
        cfg = self._config_with_develop_branch(develop_branch="develop")
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        with patch(
            "coord.github_ops.get_issue",
            return_value={"milestone": {"number": 9, "title": "M9"}},
        ):
            changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["w1"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].target_branch == "feature/ms-9"

    def test_targets_default_branch_when_issue_has_no_milestone(self, coord_db) -> None:
        """Opted-in repo, but this issue isn't tagged to any milestone —
        falls back to default_branch, same as an un-opted-in repo."""
        cfg = self._config_with_develop_branch(develop_branch="develop")
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        with patch("coord.github_ops.get_issue", return_value={"milestone": None}):
            changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["w1"]
        items = load_queue()
        assert items[0].target_branch == "main"

    def test_targets_default_branch_when_repo_not_opted_in(self, coord_db) -> None:
        """No develop_branch configured → default_branch, and the milestone
        `gh` lookup must never even happen (zero extra cost for repos that
        haven't opted in)."""
        cfg = self._config_with_develop_branch(develop_branch=None)
        work = self._work("w1", test_state="passed")
        rev = self._review("w1", verdict="approve")
        board = self._board(completed=[work, rev])

        with patch("coord.github_ops.get_issue") as get_issue:
            changed = mq.enqueue_approved_work(cfg, board)

        get_issue.assert_not_called()
        assert changed == ["w1"]
        items = load_queue()
        assert items[0].target_branch == "main"

    # ── #1490: one branch, N work rows, one queue entry ────────────────────

    def test_three_work_rows_one_branch_produce_one_entry(self, coord_db) -> None:
        """The exact #1445 scenario: one failed test_state, two passed, all
        on the same branch. Must produce exactly one queue entry, keyed to
        the winning (approved + test-passed) row — not whichever row the
        board happened to list last."""
        cfg = self._config()
        branch = "issue-1445-fix"
        failed = self._work("31bd30875eb3", test_state="failed", branch=branch)
        failed.dispatched_at = 1000
        passed1 = self._work("12fced1dfa80", test_state="passed", branch=branch)
        passed1.dispatched_at = 2000
        passed2 = self._work("5ed99d1f7edf", test_state="passed", branch=branch)
        passed2.dispatched_at = 3000
        # An approval anywhere in the branch's chain covers the whole branch
        # (has_approved_review scans by shared branch) — point it at the
        # winning row, matching what actually happened on #1445.
        rev = self._review("5ed99d1f7edf", verdict="approve")
        board = self._board(completed=[failed, passed1, passed2, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["5ed99d1f7edf"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "5ed99d1f7edf"
        assert items[0].branch == branch

    def test_repeated_ticks_do_not_reannounce_same_branch(self, coord_db) -> None:
        """#1490 regression: before the fix, every tick re-keyed the one
        queue entry to whichever row was processed last in board.completed
        order and reported it as a change — forever, even with zero new
        work. A second call with the same board must be a true no-op."""
        cfg = self._config()
        branch = "issue-1445-fix"
        failed = self._work("31bd30875eb3", test_state="failed", branch=branch)
        passed1 = self._work("12fced1dfa80", test_state="passed", branch=branch)
        passed2 = self._work("5ed99d1f7edf", test_state="passed", branch=branch)
        failed.dispatched_at, passed1.dispatched_at, passed2.dispatched_at = (
            1000, 2000, 3000,
        )
        rev = self._review("5ed99d1f7edf", verdict="approve")
        board = self._board(completed=[failed, passed1, passed2, rev])

        first = mq.enqueue_approved_work(cfg, board)
        second = mq.enqueue_approved_work(cfg, board)
        third = mq.enqueue_approved_work(cfg, board)

        assert first == ["5ed99d1f7edf"]
        assert second == []
        assert third == []
        assert len(load_queue()) == 1

    def test_iteration_order_does_not_change_the_winner(self, coord_db) -> None:
        """The winner is picked by dispatched_at, not by position in
        board.completed — reordering the same three rows must resolve to
        the same winner."""
        cfg = self._config()
        branch = "issue-1445-fix"
        failed = self._work("31bd30875eb3", test_state="failed", branch=branch)
        failed.dispatched_at = 1000
        passed1 = self._work("12fced1dfa80", test_state="passed", branch=branch)
        passed1.dispatched_at = 2000
        passed2 = self._work("5ed99d1f7edf", test_state="passed", branch=branch)
        passed2.dispatched_at = 3000
        rev = self._review("5ed99d1f7edf", verdict="approve")
        # Deliberately out of dispatch order.
        board = self._board(completed=[passed2, failed, passed1, rev])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["5ed99d1f7edf"]
        assert load_queue()[0].assignment_id == "5ed99d1f7edf"

    # ── #1601: sweep on a condition, not an event ───────────────────────────

    def test_enqueues_1566_topology_without_any_transition_event(
        self, coord_db, monkeypatch
    ) -> None:
        """#1601 (the #1566 incident): a review verdict written with no
        corresponding transition event still results in an enqueued merge on
        the next sweep. This board is constructed directly — no
        `process_review_completion`/`_advance_pipeline` call ever ran — the
        exact "the transition that would have triggered enqueue was missed"
        shape #1441 fixed for reviews, applied here to the merge queue.

        Board shape (from the #1566 incident): parent work is done, tested,
        and smoked, but its own `review_state` is stuck at "dispatched" with
        no verdict (superseded by a fix round); the fix round is done and
        approved but never re-tested; a second review approved the fix.
        `enqueue_approved_work` must resolve the branch's winner (the
        parent — it's the only row with a fresh terminal test_state) and
        find the fix round's approval through the chain."""
        from coord import github_ops

        monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: False)
        cfg = self._config()

        parent = self._work("8b26520edabb", test_state="passed", branch="issue-1566-fix")
        parent.review_state = "dispatched"
        parent.review_verdict = None
        parent.dispatched_at = 1.0
        review1 = self._review("8b26520edabb", verdict="request-changes")
        review1.dispatched_at = 2.0
        fix = self._work("adaff508c83d", test_state=None, branch="issue-1566-fix")
        fix.review_of_assignment_id = "8b26520edabb"
        fix.review_state = "done"
        fix.review_verdict = "approve"
        fix.dispatched_at = 3.0
        review2 = self._review("adaff508c83d", verdict="approve")
        review2.dispatched_at = 4.0
        board = self._board(completed=[parent, review1, fix, review2])

        changed = mq.enqueue_approved_work(cfg, board)

        assert changed == ["8b26520edabb"]
        items = load_queue()
        assert len(items) == 1
        assert items[0].assignment_id == "8b26520edabb"
        assert items[0].branch == "issue-1566-fix"


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

    def test_ready_when_gh_ops_backfills_null_branch_patch_id(self, coord_db) -> None:
        """#1506: an entry whose approved review predates #1475
        (review_patch_id set, but the entry's own branch_patch_id was never
        backfilled — e.g. no `coord merge` tick ran between the rebase and
        this `plan()` call) must not display BLOCKED just because the
        stored field is null — plan() already receives gh_ops (used for the
        epic-closing-keyword gate); this proves it's also threaded into the
        review gate's on-demand patch-id computation."""
        items = [_q("w1", size=100, target="main", repo_github="acme/api")]
        items[0].branch_head_sha = "def456"  # rebased since the review ran
        items[0].branch_patch_id = None      # never backfilled
        save_queue(items)
        review = self._review("w1", verdict="approve")
        review.review_head_sha = "abc123"
        review.review_patch_id = "patchid-same"
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            review,
        ])
        cfg = self._config()

        class _Gh:
            def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
                assert (repo, base) == ("acme/api", "main")
                return "patchid-same"
            def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
                return []
            def is_epic_issue(self, repo: str, issue_number: int) -> bool:
                return False

        plan = mq.plan(board, cfg, gh_ops=_Gh())
        assert plan[0].status == mq.PLAN_READY
        assert plan[0].reason is None

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

    def test_plan_and_only_agree_on_stale_parent_smoke_verdict(self, coord_db) -> None:
        """#1601 (the #1566 incident): a fix round is approved by review but
        never re-tested; the only test verdict anywhere on the branch is the
        PARENT's, and it's stale relative to the fix commit (its
        `test_head_sha` doesn't match the branch's live head). Before #1601,
        `has_smoke_verdict` only ever saw a freshly-enqueued entry's own
        (always-`None`) `branch_head_sha`/`branch_patch_id` fields — the
        staleness check silently no-op'd — so `coord merge --plan` showed
        READY for exactly the entry `coord merge --only` (whose `process()`
        DOES live-fetch those fields first) then refused as stale. Passing
        `gh_ops` into `has_smoke_verdict` (mirroring `has_approved_review`)
        closes that plan-vs-enforcement split: both must now see the SAME
        stale verdict and agree it's BLOCKED."""
        cfg = self._config()
        parent = self._work("8b26520edabb", test_state="passed")
        parent.branch = "issue-1566-fix"
        parent.test_head_sha = "sha-before-fix"
        parent.test_base_sha = "sha-base"
        parent.test_patch_id = "patch-before-fix"
        fix = Assignment(
            machine_name="m1", repo_name="api", issue_number=1,
            issue_title="[fix] t", assignment_id="adaff508c83d", type="work",
            status="done", branch="issue-1566-fix", test_state=None,
            review_of_assignment_id="8b26520edabb",
        )
        review2 = self._review("adaff508c83d", verdict="approve")
        board = self._board(completed=[parent, fix, review2])

        class _FixShaGh(FakeGh):
            def get_branch_sha(self, repo, branch):
                return "sha-base" if branch == "main" else "sha-after-fix"

            def get_branch_patch_id(self, repo, base, branch):
                return "patch-after-fix"

        gh = _FixShaGh()

        save_queue([_q("8b26520edabb", branch="issue-1566-fix", size=10)])
        plan_result = mq.plan(board, cfg, gh_ops=gh)
        assert plan_result[0].status == mq.PLAN_BLOCKED
        assert "test" in (plan_result[0].reason or "").lower()

        # `plan()` never persists its in-memory SHA backfill (read-only, no
        # DB writes) — reload a fresh entry so `--only`'s process() sees
        # exactly what a real invocation would: nothing pre-populated.
        save_queue([_q("8b26520edabb", branch="issue-1566-fix", size=10)])
        only_items = mq.load_queue()
        events = mq.process(only_items, gh, dry_run=True, config=cfg, board=board)
        kinds = [e.kind for e in events]
        assert "smoke_required" in kinds

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

    def test_ci_summary_populated_from_ci_store(self, coord_db) -> None:
        """#1344: plan() attaches a structured `ci_summary` + `pr_number` so
        the TUI can render CI badges straight from `/board` instead of
        shelling out to `gh pr checks` itself."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True

            def list_checks_for_pr(self, repo, number):
                return [
                    SimpleNamespace(name="build", status="completed", conclusion="success"),
                    SimpleNamespace(name="lint", status="completed", conclusion="failure", url="http://x/lint"),
                    SimpleNamespace(name="test", status="in_progress", conclusion=None),
                ]

        items = [_q("w1", size=50, pr=99)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].pr_number == 99
        summary = plan[0].ci_summary
        assert summary is not None
        assert summary.passed == 1
        assert summary.failed == 1
        assert summary.running == 1
        assert summary.failed_names == ["lint"]
        assert summary.first_failed_url == "http://x/lint"

    def test_ci_summary_none_without_pr_number(self, coord_db) -> None:
        """No PR yet opened → no CI summary (mirrors the CI gate's own guard)."""
        from types import SimpleNamespace

        class FakeCi:
            is_available = True

            def list_checks_for_pr(self, repo, number):
                return [SimpleNamespace(name="build", status="completed", conclusion="success")]

        items = [_q("w1", size=50)]  # pr_number=None
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert plan[0].pr_number is None
        assert plan[0].ci_summary is None

    def test_ci_summary_not_fetched_for_merged_entries(self, coord_db) -> None:
        """Review fix (#1344): `plan()` must not call `list_checks_for_pr` for
        non-PENDING entries when handed a *live* `CiStore` — the callers that
        pass one (`_auto_drain_tick`'s auto-drain and `coord merge --plan`,
        as opposed to the daemon's snapshot-backed `/board` read) would
        otherwise shell out to `gh pr checks` once per historical MERGED
        entry in the queue on every call, since `merge_queue` never prunes
        MERGED rows. Scoping the CI-summary computation to PENDING entries
        (matching `_entry_gate_status`'s own scope) prevents that."""
        from types import SimpleNamespace

        calls: list[tuple[str, int]] = []

        class FakeCi:
            is_available = True

            def list_checks_for_pr(self, repo, number):
                calls.append((repo, number))
                return [SimpleNamespace(name="build", status="completed", conclusion="success")]

        items = [
            _q("w1", size=50, pr=101, state=MERGED),
            _q("w2", size=50, pr=102, state=MERGING),
        ]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
            self._work("w2", test_state="passed"),
            self._review("w2", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, ci_store=FakeCi())
        assert calls == []
        assert all(pm.ci_summary is None for pm in plan)

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

    # ── #1318: epic-closing-keyword-in-commit gate ─────────────────────────

    def test_blocked_epic_closing_keyword_in_commit(self, coord_db) -> None:
        """A commit-message closing keyword for an epic shows PLAN_BLOCKED.

        This is the plan()/process() parity gap from #1318 review: an entry
        that `process()` would refuse to merge (epic auto-close hazard) must
        also show BLOCKED in the plan the operator checks beforehand, not
        just fail silently at merge time.
        """
        items = [_q("w1", size=50, pr=100)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        gh = FakeGh(
            pr_commit_messages={100: ["fix(#1314): ...\n\nCloses #1120"]},
            epic_issues={1120},
        )
        plan = mq.plan(board, cfg, gh_ops=gh)
        assert plan[0].status == mq.PLAN_BLOCKED
        assert "#1120" in (plan[0].reason or "")

    def test_not_blocked_ordinary_closing_keyword_in_commit(self, coord_db) -> None:
        """An ordinary (non-epic) closing keyword in a commit stays READY."""
        items = [_q("w1", size=50, pr=100)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        gh = FakeGh(
            pr_commit_messages={100: ["fix(#55): a normal bug fix\n\nCloses #55"]},
            epic_issues=set(),
        )
        plan = mq.plan(board, cfg, gh_ops=gh)
        assert plan[0].status == mq.PLAN_READY

    def test_epic_commit_gate_not_checked_without_pr_number(self, coord_db) -> None:
        """No PR yet opened → the commit-message gate is skipped, not blocked."""
        items = [_q("w1", size=50)]  # pr_number=None by default
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        gh = FakeGh(
            pr_commit_messages={100: ["Closes #1120"]},
            epic_issues={1120},
        )
        plan = mq.plan(board, cfg, gh_ops=gh)
        assert plan[0].status == mq.PLAN_READY

    def test_epic_commit_gate_skipped_without_gh_ops(self, coord_db) -> None:
        """Without gh_ops, the commit-message gate is skipped (backward compat)."""
        items = [_q("w1", size=50, pr=100)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg)
        assert plan[0].status == mq.PLAN_READY

    def test_epic_commit_gate_lint_failure_never_blocks_plan(self, coord_db) -> None:
        """A get_pr_commit_messages/is_epic_issue failure fails open, not blocked."""
        class _BoomOnCommits(FakeGh):
            def get_pr_commit_messages(self, repo: str, number: int) -> list[str]:
                raise RuntimeError("gh pr view --json commits failed")

        items = [_q("w1", size=50, pr=100)]
        save_queue(items)
        board = self._board(completed=[
            self._work("w1", test_state="passed"),
            self._review("w1", verdict="approve"),
        ])
        cfg = self._config()
        plan = mq.plan(board, cfg, gh_ops=_BoomOnCommits())
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

    def test_entry_gate_status_blocked_epic_closing_keyword_in_commit(self) -> None:
        """#1318: a commit-message epic closing keyword → PLAN_BLOCKED."""
        entry = _q("w1", pr=100)
        gh = FakeGh(
            pr_commit_messages={100: ["Closes #1120"]},
            epic_issues={1120},
        )
        status, reason = mq._entry_gate_status(entry, None, None, gh_ops=gh)
        assert status == mq.PLAN_BLOCKED
        assert reason is not None and "#1120" in reason


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

    # ── #567 follow-up: fix worker with branch=NULL must still be found ────

    def test_ready_when_fix_worker_approved_with_null_branch(self, coord_db) -> None:
        """#567 follow-up: `_work_has_approved_review_a` (used by
        staging_items, the /board staging section) must recognize an approved
        review on a fix worker dispatched with branch=NULL (the #557 gap) via
        the review_of_assignment_id chain — not just branch-keyed siblings.
        Mirrors the has_approved_review fix, now sharing `_chain_work_ids`."""
        orig_work = self._work("orig", branch="worker/orig")
        fix_work = Assignment(
            machine_name="m1", repo_name="api", issue_number=42,
            issue_title="[fix-1] t", assignment_id="fix1", type="work",
            status="done", branch=None, review_of_assignment_id="orig",
        )
        re_review = self._review("fix1", verdict="approve")
        orig_review = self._review("orig", verdict="request-changes")
        board = self._board(completed=[orig_work, orig_review, fix_work, re_review])
        cfg = self._config()
        items = mq.staging_items(board, cfg)
        assert len(items) == 1
        assert items[0].assignment_id == "orig"
        assert items[0].status == mq.STAGING_READY

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


# ── #920: find_sibling_overlaps ──────────────────────────────────────────────

class TestFindSiblingOverlaps:
    """#920: warn when ≥2 approved (PENDING), aging queue entries touch the
    same files — the #769/#645/#770 sibling-branch-collision shape.
    """

    AGING_HOURS = 2.0
    NOW = 1_000_000.0  # arbitrary fixed epoch so ages are deterministic

    @staticmethod
    def _config(aging_hours: float = AGING_HOURS):
        from coord.config import MergeConfig
        from types import SimpleNamespace
        return SimpleNamespace(merge=MergeConfig(sibling_overlap_aging_hours=aging_hours))

    @staticmethod
    def _board(completed=None, active=None):
        from coord.models import Board
        return Board(active=list(active or []), completed=list(completed or []))

    @staticmethod
    def _work(aid: str, *, issue_number: int, files: list[str]) -> Assignment:
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=issue_number,
            issue_title=f"issue {issue_number}", assignment_id=aid, type="work",
            status="done", branch=f"issue-{issue_number}-{aid}",
            files_allowed=files,
        )

    def _entry(
        self, aid: str, *, issue_number: int, enqueued_at: float,
        repo_github: str = "acme/api", target_branch: str = "main",
        state: str = mq.PENDING,
    ) -> mq.QueuedMerge:
        return mq.QueuedMerge(
            assignment_id=aid, repo_name="api", repo_github=repo_github,
            branch=f"issue-{issue_number}-{aid}", target_branch=target_branch,
            issue_number=issue_number, issue_title=f"issue {issue_number}",
            state=state, enqueued_at=enqueued_at,
        )

    def test_warns_on_aged_overlapping_pair(self, coord_db) -> None:
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 60),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py", "coord/bar.py"]),
            self._work("a2", issue_number=102, files=["coord/bar.py", "coord/baz.py"]),
        ])
        warnings = mq.find_sibling_overlaps(board, self._config(), now=self.NOW)
        assert len(warnings) == 1
        w = warnings[0]
        assert w.repo_name == "api"
        assert w.target_branch == "main"
        assert w.issue_numbers == (101, 102)  # oldest (a1) first
        assert w.overlapping_files == ("coord/bar.py",)
        assert w.oldest_age_hours == pytest.approx(self.AGING_HOURS + 1, abs=0.05)

    def test_no_warning_when_files_dont_overlap(self, coord_db) -> None:
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 60),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/baz.py"]),
        ])
        assert mq.find_sibling_overlaps(board, self._config(), now=self.NOW) == []

    def test_no_warning_when_not_yet_aged(self, coord_db) -> None:
        """Overlap exists but the oldest entry hasn't crossed the threshold yet."""
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=self.NOW - 60),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 30),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/foo.py"]),
        ])
        assert mq.find_sibling_overlaps(board, self._config(), now=self.NOW) == []

    def test_no_warning_with_single_entry(self, coord_db) -> None:
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([self._entry("a1", issue_number=101, enqueued_at=old_enqueued)])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
        ])
        assert mq.find_sibling_overlaps(board, self._config(), now=self.NOW) == []

    def test_non_pending_entries_ignored(self, coord_db) -> None:
        """A MERGED sibling doesn't trigger a warning against a live PENDING one."""
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued, state=mq.MERGED),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 60),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/foo.py"]),
        ])
        assert mq.find_sibling_overlaps(board, self._config(), now=self.NOW) == []

    def test_different_target_branches_not_grouped(self, coord_db) -> None:
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued, target_branch="main"),
            self._entry("a2", issue_number=102, enqueued_at=old_enqueued, target_branch="feature/ms-1"),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/foo.py"]),
        ])
        assert mq.find_sibling_overlaps(board, self._config(), now=self.NOW) == []

    def test_transitive_cluster_of_three(self, coord_db) -> None:
        """a1↔a2 share a file, a2↔a3 share a different file — all three cluster."""
        old_enqueued = self.NOW - (self.AGING_HOURS + 1) * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 120),
            self._entry("a3", issue_number=103, enqueued_at=self.NOW - 60),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/a.py"]),
            self._work("a2", issue_number=102, files=["coord/a.py", "coord/b.py"]),
            self._work("a3", issue_number=103, files=["coord/b.py"]),
        ])
        warnings = mq.find_sibling_overlaps(board, self._config(), now=self.NOW)
        assert len(warnings) == 1
        assert warnings[0].issue_numbers == (101, 102, 103)
        assert set(warnings[0].overlapping_files) == {"coord/a.py", "coord/b.py"}

    def test_disabled_via_zero_aging_hours(self, coord_db) -> None:
        old_enqueued = self.NOW - 1000 * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued),
            self._entry("a2", issue_number=102, enqueued_at=old_enqueued),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/foo.py"]),
        ])
        cfg = self._config(aging_hours=0)
        assert mq.find_sibling_overlaps(board, cfg, now=self.NOW) == []

    def test_missing_merge_config_defaults_to_24h(self, coord_db) -> None:
        """A config object with no `.merge` attribute falls back to the default."""
        from types import SimpleNamespace
        old_enqueued = self.NOW - 25 * 3600
        mq.save_queue([
            self._entry("a1", issue_number=101, enqueued_at=old_enqueued),
            self._entry("a2", issue_number=102, enqueued_at=self.NOW - 60),
        ])
        board = self._board(completed=[
            self._work("a1", issue_number=101, files=["coord/foo.py"]),
            self._work("a2", issue_number=102, files=["coord/foo.py"]),
        ])
        warnings = mq.find_sibling_overlaps(board, SimpleNamespace(), now=self.NOW)
        assert len(warnings) == 1


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


# ── #1640: stale vs missing smoke verdict, and plan/only agreement ───────────

class TestStaleSmokeVerdictReporting:
    """#1640: a verdict that EXISTS but fails the #1479 freshness check must
    be reported as stale — never as "no verdict recorded" — and every reader
    must reach the same conclusion for the same entry.

    The scenario reproduced here is the one that made #1640 get filed as a
    lost DB write: a passing verdict is recorded, a sibling merge moves
    `main`, and the next merge attempt refuses. The verdict is intact on the
    board the whole time; only the base it was recorded against has moved.

    Nothing here relaxes the gate — every assertion below still expects the
    stale verdict to BLOCK. See the "no behaviour change" clause in #1640.
    """

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _config(*, gates: list[str] | None = None):
        from dataclasses import dataclass as _dc, field as _f

        @_dc
        class _Reviews:
            enabled: bool = False

        @_dc
        class _Pipeline:
            default_gates: list[str] | None = None

        @_dc
        class _Cfg:
            reviews: _Reviews = _f(default_factory=_Reviews)
            pipeline: _Pipeline = _f(default_factory=_Pipeline)

        cfg = _Cfg()
        cfg.pipeline.default_gates = gates if gates is not None else ["test", "merge"]
        return cfg

    @staticmethod
    def _board(completed=None):
        from coord.models import Board
        return Board(active=[], completed=list(completed or []))

    @staticmethod
    def _tested_work(aid: str = "w1", *, base_sha: str = "base-old") -> Assignment:
        """A done work assignment carrying a PASSING verdict, anchored (per
        #1479) to the branch/base it was actually tested against."""
        return Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id=aid, type="work", status="done",
            branch=f"worker/{aid}",
            test_state="passed",
            test_head_sha="branch-sha",
            test_base_sha=base_sha,
            test_patch_id="patch-1",
        )

    @dataclass
    class _Gh(FakeGh):
        """FakeGh that answers the two freshness lookups. `base_sha` is what
        the target branch reads as *now* — set it different from the
        assignment's `test_base_sha` to simulate a sibling merge having moved
        main under an already-tested branch."""

        base_sha: str = "base-new"
        branch_sha: str = "branch-sha"

        def get_branch_sha(self, repo: str, branch: str) -> str | None:
            return self.base_sha if branch == "main" else self.branch_sha

        def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
            return "patch-1"

    # ── defect 1: the message names the case ──────────────────────────────

    def test_moved_base_is_reported_as_stale_not_missing(self) -> None:
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is False, "a moved base must still BLOCK (#1479)"
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "base"
        assert verdict.recorded_sha == "base-old"
        assert verdict.current_sha == "base-new"
        # The exact wording that mis-diagnosed #1640 must not appear.
        assert "no verdict recorded" not in (verdict.message or "")
        assert "stale" in (verdict.message or "")
        assert "base-old"[:7] in (verdict.message or "")
        assert "base-new"[:7] in (verdict.message or "")

    def test_no_verdict_at_all_is_still_reported_as_missing(self) -> None:
        """The genuine "never recorded" case keeps its original wording — the
        distinction is only useful if both halves are accurate."""
        work = self._tested_work()
        work.test_state = None
        board = self._board(completed=[work])

        verdict = mq.evaluate_smoke_verdict(_q("w1", target="main"), board)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_MISSING
        assert verdict.message == "smoke test required but no verdict recorded"

    def test_changed_branch_content_reports_the_branch_anchor(self) -> None:
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.branch_head_sha = "branch-new"
        entry.branch_patch_id = "patch-2"  # content really did change

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE
        assert verdict.anchor == "branch"
        assert "branch" in (verdict.message or "")

    def test_fresh_verdict_still_passes(self) -> None:
        """Guard against over-blocking: an unmoved base must still merge."""
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-old"

        verdict = mq.evaluate_smoke_verdict(entry, board)
        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK
        assert verdict.message is None

    # ── #1732: `skipped` is not subject to #1479 base-SHA freshness ───────
    #
    # `skipped` is a structural claim about the diff ("contract/fixture-only,
    # nothing to smoke-test" — #1076/#1152), not a measurement of code at a
    # SHA the way `passed` is. It must never be reported STALE just because
    # the base or branch moved — there is nothing to re-verify, and the only
    # way through used to be `--skip-smoke`, waiving a gate that had already
    # been correctly waived.

    def test_skipped_verdict_is_not_stale_when_base_moved(self) -> None:
        """The direct regression: a `skipped` verdict recorded against base X
        must not block when the base is now Y."""
        work = self._tested_work()
        work.test_state = "skipped"
        board = self._board(completed=[work])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"  # base moved since recording

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK
        assert verdict.message is None

    def test_skipped_verdict_is_not_stale_when_branch_content_changed(self) -> None:
        """Same exemption applies to the branch-content-changed anchor, not
        just the base-moved one — `skipped` doesn't decay under either."""
        work = self._tested_work()
        work.test_state = "skipped"
        board = self._board(completed=[work])
        entry = _q("w1", target="main")
        entry.branch_head_sha = "branch-new"   # new commit pushed
        entry.branch_patch_id = "patch-2"       # content actually changed

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is True
        assert verdict.kind == mq.SMOKE_OK

    def test_passed_verdict_still_goes_stale_when_base_moves(self) -> None:
        """#1479 must stay intact for `passed` — this fix must not over-reach
        into auto-waiving stale verdicts generally. Restates
        ``test_moved_base_is_reported_as_stale_not_missing`` side by side
        with the `skipped` exemption above so the two can't silently drift
        onto the same (wrong) behaviour."""
        board = self._board(completed=[self._tested_work()])  # test_state="passed"
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"

        verdict = mq.evaluate_smoke_verdict(entry, board)

        assert verdict.ok is False
        assert verdict.kind == mq.SMOKE_STALE

    def test_has_smoke_verdict_still_returns_the_same_booleans(self) -> None:
        """The boolean seam every gate call site uses is unchanged."""
        board = self._board(completed=[self._tested_work()])
        stale_entry = _q("w1", target="main")
        stale_entry.target_branch_head_sha = "base-new"
        fresh_entry = _q("w1", target="main")
        fresh_entry.target_branch_head_sha = "base-old"

        assert mq.has_smoke_verdict(stale_entry, board) is False
        assert mq.has_smoke_verdict(fresh_entry, board) is True

    def test_process_error_string_names_the_moved_base(self) -> None:
        """`coord merge --only`'s wording — the string the operator reads."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        items = [_q("w1", target="main", size=10)]

        events = process(items, self._Gh(), config=cfg, board=board)

        blocked = [e for e in events if e.kind == "smoke_required"]
        assert len(blocked) == 1
        assert "no verdict recorded" not in blocked[0].message
        assert "stale" in blocked[0].message
        assert items[0].error is not None and "stale" in items[0].error

    def test_dry_run_uses_the_same_stale_wording(self) -> None:
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        items = [_q("w1", target="main", size=10)]

        events = process(
            items, self._Gh(), config=cfg, board=board, dry_run=True
        )

        blocked = [e for e in events if e.kind == "smoke_required"]
        assert len(blocked) == 1
        assert "stale" in blocked[0].message
        assert "no verdict recorded" not in blocked[0].message

    # ── defect 2: --plan and --only agree ─────────────────────────────────

    def test_plan_and_only_agree_after_the_base_moves(self, coord_db) -> None:
        """The #1640 acceptance sequence.

        Record a passing verdict, move the base, then ask both readers about
        the SAME entry: `plan()` (what `coord merge --plan` renders) and
        `process()` (what `coord merge --only` runs). Before #1640 the former
        said READY and the latter refused.
        """
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        gh = self._Gh()  # main now reads base-new; the verdict says base-old
        save_queue([_q("w1", target="main", size=10)])

        planned = mq.plan(board, cfg, gh_ops=gh)
        assert len(planned) == 1
        assert planned[0].status == mq.PLAN_BLOCKED, (
            "--plan must not show READY for a verdict --only refuses"
        )
        assert "stale" in (planned[0].reason or "")
        assert "missing" not in (planned[0].reason or "")

        items = mq.load_queue()
        events = process(items, gh, config=cfg, board=board)
        refusals = [e for e in events if e.kind == "smoke_required"]
        assert len(refusals) == 1, "the gate must still block (#1479 unchanged)"
        assert "stale" in refusals[0].message

    def test_plan_and_only_agree_when_the_verdict_is_fresh(self, coord_db) -> None:
        """Same two readers, unmoved base → both say go. Agreement has to
        hold in the passing direction too, or the fix is just "block more"."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work(base_sha="base-new")])
        gh = self._Gh()
        save_queue([_q("w1", target="main", size=10)])

        planned = mq.plan(board, cfg, gh_ops=gh)
        assert planned[0].status == mq.PLAN_READY
        assert planned[0].reason is None

        items = mq.load_queue()
        events = process(items, gh, config=cfg, board=board, dry_run=True)
        assert not [e for e in events if e.kind == "smoke_required"]

    def test_plan_gate_status_reason_names_staleness(self) -> None:
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.target_branch_head_sha = "base-new"

        status, reason = mq._entry_gate_status(entry, board, self._config())

        assert status == mq.PLAN_BLOCKED
        assert reason is not None
        assert "stale" in reason and "missing" not in reason

    # ── the staging path (merge_queue.py's raw test_state read) ───────────

    def test_staging_item_blocks_on_a_stale_verdict(self, coord_db) -> None:
        """#1640 defect 2, staging half: the section used to read the raw
        `test_state` column with no freshness check and show READY."""
        from types import SimpleNamespace

        cfg = self._config()
        cfg.repo = lambda name: SimpleNamespace(  # type: ignore[attr-defined]
            github="acme/api", default_branch="main"
        )
        board = self._board(completed=[self._tested_work()])
        save_queue([])

        items = mq.staging_items(board, cfg, gh_ops=self._Gh())

        assert len(items) == 1
        assert items[0].status == mq.STAGING_BLOCKED
        assert "stale" in (items[0].reason or "")

    def test_staging_item_ready_when_verdict_is_fresh(self, coord_db) -> None:
        from types import SimpleNamespace

        cfg = self._config()
        cfg.repo = lambda name: SimpleNamespace(  # type: ignore[attr-defined]
            github="acme/api", default_branch="main"
        )
        board = self._board(completed=[self._tested_work(base_sha="base-new")])
        save_queue([])

        items = mq.staging_items(board, cfg, gh_ops=self._Gh())

        assert len(items) == 1
        assert items[0].status == mq.STAGING_READY
        assert items[0].reason is None

    def test_staging_without_gh_ops_makes_no_calls(self, coord_db) -> None:
        """The `/board` read path's no-live-I/O contract: gh_ops=None means
        the freshness anchors are simply unavailable, never a blind `gh` call."""
        from types import SimpleNamespace

        cfg = self._config()
        cfg.repo = lambda name: SimpleNamespace(  # type: ignore[attr-defined]
            github="acme/api", default_branch="main"
        )
        board = self._board(completed=[self._tested_work()])
        save_queue([])

        items = mq.staging_items(board, cfg)

        assert len(items) == 1
        assert items[0].status == mq.STAGING_READY

    # ── display_error must not clear a staleness refusal ──────────────────

    def test_display_error_keeps_a_stale_refusal(self) -> None:
        """`display_error` recomputes I/O-free, so it can see the terminal
        verdict but not the anchors. Clearing on that evidence would put the
        false green back on `coord status`."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.error = (
            "smoke test verdict is stale: recorded against base base-ol, "
            "base is now base-ne — re-verify"
        )

        assert mq.display_error(entry, board, cfg) == entry.error

    def test_display_error_still_clears_a_satisfied_missing_verdict(self) -> None:
        """#420's original behaviour for the "never recorded" string is
        untouched: once a verdict lands, the stored string stops showing."""
        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        entry = _q("w1", target="main")
        entry.error = "smoke test required but no verdict recorded"

        assert mq.display_error(entry, board, cfg) is None

    def test_plan_through_the_daemon_gate_snapshot_also_blocks(
        self, coord_db
    ) -> None:
        """#1640 end-to-end for the daemon-fronted setup.

        `/board` (and therefore `coord merge --plan` against a daemon) passes
        the tick-refreshed `GateSnapshot` as gh_ops. It used not to implement
        `get_branch_sha` at all; `evaluate_smoke_verdict` swallowed the
        AttributeError and every staleness check became a no-op, so the plan
        rendered READY for the entry `--only` refused. The snapshot now
        serves the anchors from its own refreshed data.
        """
        from coord.gate_snapshot import GateSnapshot

        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        save_queue([_q("w1", target="main", size=10)])

        snapshot = GateSnapshot(
            branch_shas={
                ("acme/api", "main"): "base-new",       # a sibling merge landed
                ("acme/api", "worker/w1"): "branch-sha",
            },
            branch_patch_ids={("acme/api", "main", "worker/w1"): "patch-1"},
        )

        planned = mq.plan(board, cfg, gh_ops=snapshot)

        assert planned[0].status == mq.PLAN_BLOCKED
        assert "stale" in (planned[0].reason or "")

    def test_plan_through_an_empty_gate_snapshot_fails_open(self, coord_db) -> None:
        """A snapshot that hasn't refreshed yet knows no SHAs. That must read
        as "anchor unavailable" (fail open, today's behaviour for a `gh` that
        errors) — a cold daemon must not blanket-block every entry."""
        from coord.gate_snapshot import GateSnapshot

        cfg = self._config()
        board = self._board(completed=[self._tested_work()])
        save_queue([_q("w1", target="main", size=10)])

        planned = mq.plan(board, cfg, gh_ops=GateSnapshot())

        assert planned[0].status == mq.PLAN_READY
