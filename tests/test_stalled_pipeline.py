"""Tests for #1441 — the stalled-pipeline sweeper.

The auto-loop (coord.auto_loop) only reacts to review/fix TRANSITIONS: the
instant a review or fix flips to `done` during a given `coord notify` pass.
Once that transition is consumed, nothing re-examines the row — so a
precondition that lands late (a Test verdict backfilled two days after the
review completed, the vimcode #602 reference case) leaves it stranded
forever with no error and no surfacing.

`coord.notify.detect_stalled_pipeline` re-scans every *done* work chain on
the board each notify pass and flags the ones stuck on an unmet
precondition a fresh transition would already have resolved. Mirrors
`detect_needs_attention`'s contract (#846): detection + surfacing only, no
dispatch/kill/handoff, idempotent via the shared `notified` ledger.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from coord import notify as notify_mod
from coord import state as state_mod
from coord.auto_loop import LoopAction
from coord.comments import EVENT_STALLED, format_stalled_pipeline
from coord.config import Config, PipelineConfig
from coord.github_ops import work_is_terminal as _real_work_is_terminal
from coord.merge_queue import CONFLICT, PENDING, QueuedMerge
from coord.models import Assignment, Board, Machine, Repo


# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="vimcode", github="acme/vimcode", default_branch="main")],
        machines=[
            Machine(
                name="mac-mini",
                host="mac-mini.tailnet",
                repos=["vimcode"],
                repo_paths={"vimcode": "/tmp/vimcode"},
            ),
        ],
        pipeline=PipelineConfig(default_gates=["review", "test", "merge"]),
    )


def _work(
    aid: str = "work-1",
    *,
    status: str = "done",
    test_state: str | None = None,
    provider_name: str | None = None,
    review_state: str | None = None,
    required_gates: list[str] | None = None,
    dispatched_at: float = 1000.0,
    finished_at: float | None = 1100.0,
    repo_name: str = "vimcode",
    issue_number: int = 602,
) -> Assignment:
    return Assignment(
        machine_name="mac-mini",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title="ctx_blocks_event gate uses ModalStack",
        assignment_id=aid,
        status=status,
        type="work",
        branch=f"issue-{issue_number}-fix",
        test_state=test_state,
        provider_name=provider_name,
        review_state=review_state,
        required_gates=required_gates or [],
        dispatched_at=dispatched_at,
        finished_at=finished_at,
    )


def _review(
    of_aid: str,
    *,
    aid: str = "review-1",
    status: str = "done",
    review_verdict: str | None = "request-changes",
    review_posted_at: float | None = 1150.0,
    dispatched_at: float = 1120.0,
    finished_at: float | None = 1140.0,
    repo_name: str = "vimcode",
    issue_number: int = 602,
) -> Assignment:
    return Assignment(
        machine_name="mac-mini",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title="[review] ctx_blocks_event gate uses ModalStack",
        assignment_id=aid,
        status=status,
        type="review",
        review_of_assignment_id=of_aid,
        review_verdict=review_verdict,
        review_posted_at=review_posted_at,
        dispatched_at=dispatched_at,
        finished_at=finished_at,
    )


def _fix(
    of_aid: str,
    *,
    aid: str = "fix-1",
    status: str = "done",
    dispatched_at: float = 1200.0,
    finished_at: float | None = 1300.0,
    test_state: str | None = None,
    provider_name: str | None = None,
    repo_name: str = "vimcode",
    issue_number: int = 602,
) -> Assignment:
    return Assignment(
        machine_name="mac-mini",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title="[fix-1] ctx_blocks_event gate uses ModalStack",
        assignment_id=aid,
        status=status,
        type="work",
        branch=f"issue-{issue_number}-fix",
        review_of_assignment_id=of_aid,
        review_iteration=1,
        test_state=test_state,
        provider_name=provider_name,
        dispatched_at=dispatched_at,
        finished_at=finished_at,
    )


def _board(*assignments: Assignment) -> Board:
    active = [a for a in assignments if a.status in ("running", "pending")]
    completed = [a for a in assignments if a.status not in ("running", "pending")]
    return Board(active=active, completed=completed)


# ── Reference fixture: vimcode #602 ──────────────────────────────────────────


def _vimcode_602_board() -> Board:
    """Board state captured 2026-07-26 for vimcode#602 — the concrete #1441
    reference instance: work done, test backfilled 'passed' two days after
    the review completed with 'request-changes', no fix ever dispatched."""
    work = _work(
        "work-602",
        status="done",
        test_state="passed",
        provider_name="claude-pty",
        review_state="dispatched",
    )
    review = _review("work-602", aid="review-602", review_verdict="request-changes")
    return _board(work, review)


class TestVimcode602ReferenceCase:
    def test_602_board_is_detected_as_stalled(self, config: Config) -> None:
        board = _vimcode_602_board()
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        detection, work = results[0]
        assert detection.assignment_id == "work-602"
        assert detection.reason == "review_request_changes_no_fix"
        assert detection.issue_number == 602
        assert detection.repo_name == "vimcode"
        assert "review-602" in detection.detail
        assert work.assignment_id == "work-602"


# ── Candidate stall state 1: review request-changes, no fix dispatched ──────


class TestReviewRequestChangesNoFix:
    def test_flags_when_no_fix_exists(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        assert results[0][0].reason == "review_request_changes_no_fix"

    def test_not_flagged_when_fix_already_dispatched(self, config: Config) -> None:
        """A fix worker was already dispatched for this review — being
        actively handled, not stalled."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
            _fix("work-1", aid="fix-1", status="running", dispatched_at=1200.0),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_not_flagged_when_review_approved(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        # approve => falls through to the merge-queue check; already queued
        # so nothing is flagged.
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=PENDING,
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []

    def test_not_flagged_while_review_still_running(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", status="running", review_verdict=None),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_superseded_head_uses_latest_fix_in_chain(self, config: Config) -> None:
        """work0 -> review1 (request-changes) -> fix1 -> review2
        (request-changes) -> no fix2. The stalled row is fix1 (the current
        head), not the original work0."""
        work0 = _work("work-0", test_state="passed", dispatched_at=1000.0, finished_at=1100.0)
        review1 = _review("work-0", aid="review-1", dispatched_at=1120.0, finished_at=1140.0)
        fix1 = _fix("work-0", aid="fix-1", dispatched_at=1200.0, finished_at=1300.0)
        review2 = _review(
            "fix-1", aid="review-2", dispatched_at=1320.0, finished_at=1340.0,
        )
        board = _board(work0, review1, fix1, review2)
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        detection, work = results[0]
        assert detection.assignment_id == "fix-1"
        assert detection.reason == "review_request_changes_no_fix"


# ── Candidate stall state 2: done, test verdict present, no review ever ────


class TestDoneNoReview:
    def test_flags_when_test_passed_and_no_review(self, config: Config) -> None:
        board = _board(_work("work-1", test_state="passed"))
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        assert results[0][0].reason == "done_no_review"

    def test_not_flagged_when_test_verdict_still_missing(self, config: Config) -> None:
        """No review yet is EXPECTED while the test gate hasn't cleared —
        not a stall."""
        board = _board(_work("work-1", test_state=None))
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_not_flagged_for_interactive_completion(self, config: Config) -> None:
        """#555: an interactive (claude-pty) completion is excluded from
        automatic review dispatch by design — its absence isn't a bug."""
        board = _board(
            _work("work-1", test_state="passed", provider_name="claude-pty")
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_not_flagged_as_done_no_review_when_review_gate_not_required(
        self, config: Config
    ) -> None:
        """No review dispatched is correct (not a stall) when "review" isn't
        even in required_gates. The row is still eligible for the
        merge-queue check (case 3) — supply a matching queue entry to
        isolate case 2's behaviour from case 3's."""
        board = _board(
            _work("work-1", test_state="passed", required_gates=["test", "merge"])
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=PENDING, required_gates=["test", "merge"],
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []


# ── Candidate stall state 3: approved + tested, not in the merge queue ─────


class TestApprovedNotQueued:
    def test_flags_when_approved_and_not_queued(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(results) == 1
        assert results[0][0].reason == "approved_not_queued"

    def test_not_flagged_when_already_queued(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=PENDING,
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []

    def test_not_flagged_when_test_not_passed(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state=None),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []


# ── Terminal-state guard (#522, reused not re-derived) ──────────────────────


class TestTerminalGuard:
    def test_terminal_work_never_surfaces(self, config: Config, monkeypatch) -> None:
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert results == []

    def test_terminal_cache_is_threaded_through_and_populated(
        self, config: Config
    ) -> None:
        """A caller-supplied terminal_cache is passed straight through to
        `work_is_terminal` (the #522 chokepoint pattern shared with the
        review/fix auto-loop) and ends up populated — so a caller sharing
        one cache dict across several sweep calls in the same notify pass
        gets the dedupe `work_is_terminal` itself implements."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        cache: dict = {}
        # Bypass the autouse "always non-terminal" stub for this one test so
        # the REAL `work_is_terminal` (and its cache-population logic) runs;
        # stub its own `gh`-hitting internals instead.
        with patch("coord.github_ops.work_is_terminal", _real_work_is_terminal), \
             patch("coord.github_ops.issue_is_closed", return_value=False), \
             patch("coord.github_ops.pr_is_merged", return_value=False):
            notify_mod.detect_stalled_pipeline(
                config, board=board, merge_queue_items=[], terminal_cache=cache
            )
        assert cache == {("acme/vimcode", 602, "issue-602-fix"): False}


# ── Idempotency via the notified ledger ─────────────────────────────────────


class TestIdempotency:
    def test_already_notified_row_not_returned_again(
        self, config: Config, coord_db
    ) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        first = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(first) == 1
        with patch.object(notify_mod, "github_ops") as mock_gh:
            notify_mod.post_stalled_pipeline(first[0][0], config)
            assert mock_gh.post_issue_comment.called

        second = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert second == []


# ── post_stalled_pipeline ───────────────────────────────────────────────────


class TestPostStalledPipeline:
    def test_posts_comment_and_marks_notified(self, config: Config, coord_db) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        detection, _work_row = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        with patch.object(notify_mod, "github_ops") as mock_gh:
            notify_mod.post_stalled_pipeline(detection, config)

        mock_gh.post_issue_comment.assert_called_once()
        args, _kwargs = mock_gh.post_issue_comment.call_args
        assert args[0] == "acme/vimcode"
        assert args[1] == 602
        assert "Pipeline row stalled" in args[2]

        notified = state_mod.load_notified()
        assert "work-1:stalled" in notified
        assert notified["work-1:stalled"]["event"] == EVENT_STALLED


# ── format_stalled_pipeline ──────────────────────────────────────────────────


class TestFormatStalledPipeline:
    def test_renders_reason_label_and_marker(self) -> None:
        body = format_stalled_pipeline(
            assignment_id="work-602",
            machine_name="mac-mini",
            repo_name="vimcode",
            issue_number=602,
            reason="review_request_changes_no_fix",
            detail="Review review-602 completed with request-changes...",
        )
        assert "work-602" in body
        assert "#602" in body
        assert "Review requested changes, no fix dispatched" in body
        assert f"<!-- coord:event={EVENT_STALLED}" in body


# ── Reachable from `coord notify`, not only `reconcile()` (§7) ─────────────


class TestReachableFromNotify:
    def test_run_surfaces_the_602_reference_case(
        self, config: Config, coord_db
    ) -> None:
        """`coord notify` (coord.notify.run) must reach the sweeper on its
        own — the #1441 regression class (docs/OPERATING_GOTCHAS.md §7) is
        exactly a sweeper that only ever got wired into `reconcile()`, which
        a thin-client/timer-only `coord-notify.timer` setup never calls."""
        board = _vimcode_602_board()
        state_mod.save_board(board)

        # Patch the specific posting call, not the whole `github_ops`
        # module — `detect_stalled_pipeline` also calls
        # `github_ops.work_is_terminal` (real, autouse-stubbed to False by
        # `conftest._non_terminal_work`) and a blanket module mock would
        # make that call return a truthy MagicMock, hiding every row as
        # falsely "terminal".
        with patch.object(
            notify_mod, "_agent_status", return_value={"completed": [], "active": []}
        ), patch("coord.notify.github_ops.post_issue_comment") as mock_post_comment:
            notify_mod.run(config)

        mock_post_comment.assert_called_once()
        args, _kwargs = mock_post_comment.call_args
        assert args[0] == "acme/vimcode"
        assert args[1] == 602
        assert "Pipeline row stalled" in args[2]

        notified = state_mod.load_notified()
        assert "work-602:stalled" in notified

    def test_run_is_idempotent_across_two_calls(
        self, config: Config, coord_db
    ) -> None:
        board = _vimcode_602_board()
        state_mod.save_board(board)

        with patch.object(
            notify_mod, "_agent_status", return_value={"completed": [], "active": []}
        ), patch("coord.notify.github_ops.post_issue_comment") as mock_post_comment:
            notify_mod.run(config)
            notify_mod.run(config)

        mock_post_comment.assert_called_once()

    def test_run_returns_stalled_as_fourth_tuple_element(
        self, config: Config, coord_db
    ) -> None:
        """`run()` must return the stalled detections to its caller, not just
        post a GitHub comment — the CLI (and any future board/TUI consumer)
        can only surface what it's handed back. Regression guard for the
        review finding that the sweep was invisible from `coord notify`'s own
        output even though it fired."""
        board = _vimcode_602_board()
        state_mod.save_board(board)

        with patch.object(
            notify_mod, "_agent_status", return_value={"completed": [], "active": []}
        ), patch("coord.notify.github_ops.post_issue_comment"):
            posted, stuck, needs_attention, stalled = notify_mod.run(config)

        assert posted == []
        assert stuck == []
        assert needs_attention == []
        assert len(stalled) == 1
        assert stalled[0].assignment_id == "work-602"
        assert stalled[0].reason == "review_request_changes_no_fix"


class TestNotifyCliSurfacesStalled:
    """#1441 review finding: detection alone isn't "surfacing" — the issue's
    explicit ask was CLI + board surfacing, mirroring how `detect_needs_
    attention` results are echoed by `coord notify`'s own CLI command
    (coord/commands/lifecycle.py). Drives the actual click command, not just
    `notify.run()`, so a future refactor that stops threading the stalled
    list through the CLI fails this test rather than shipping silently."""

    def test_notify_command_echoes_stalled_detection(
        self, config: Config, coord_db, capsys, monkeypatch
    ) -> None:
        from pathlib import Path

        from coord.commands import lifecycle

        board = _vimcode_602_board()
        state_mod.save_board(board)

        monkeypatch.setattr(lifecycle, "_load_config", lambda _p: config)

        with patch.object(
            notify_mod, "_agent_status", return_value={"completed": [], "active": []}
        ), patch("coord.notify.github_ops.post_issue_comment"):
            lifecycle.notify.callback(config_path=Path("unused"))

        out = capsys.readouterr().out
        assert "stalled-pipeline detection" in out
        assert "[stalled:review_request_changes_no_fix]" in out
        assert "vimcode #602" in out
        assert "work-602" in out
        # The "no new transitions" early-return guard must account for the
        # stalled set too — a stalled-only pass must never print the
        # misleading "nothing to do" message (the review's called-out
        # failure scenario).
        assert "No new transitions to notify." not in out


# ── Candidate stall state 4: merge queue entry stuck CONFLICT (#1478) ──────


class TestMergeConflictUnresolved:
    def test_flags_rebaseable_conflict_with_no_prior_fix(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert len(results) == 1
        assert results[0][0].reason == "merge_conflict_unresolved"

    def test_not_flagged_when_pending(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=PENDING,
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []

    def test_not_flagged_when_conflict_is_not_rebaseable(self, config: Config) -> None:
        """A permission/branch-protection error classifies as "human", not
        "rebaseable" — #1474's own classify-and-dispatch step marks it
        HUMAN_REQUIRED rather than retrying, so it isn't a stall a dispatch
        arm should touch."""
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="required status check is missing",
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []

    def test_not_flagged_when_conflict_fix_already_active(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
            Assignment(
                machine_name="mac-mini", repo_name="vimcode", issue_number=602,
                issue_title="[conflict-fix] t", assignment_id="cf-1", status="running",
                type="conflict-fix", review_of_assignment_id="work-1",
            ),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []

    def test_not_flagged_when_conflict_fix_retry_cap_hit(self, config: Config) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
            Assignment(
                machine_name="mac-mini", repo_name="vimcode", issue_number=602,
                issue_title="[conflict-fix] t", assignment_id="cf-1", status="failed",
                type="conflict-fix", review_of_assignment_id="work-1",
            ),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        results = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )
        assert results == []


# ── #1478: the dispatch arm ─────────────────────────────────────────────────
#
# `dispatch_stalled_pipeline_action` reuses the SAME dispatch machinery the
# on-time transition would have used for each reason — these tests assert
# the routing (right reused call, right result), not the internals of the
# reused function (already covered by test_auto_loop.py / test_review.py /
# test_merge_queue.py / test_conflict_fix.py).


class TestDispatchDisabledByDefault:
    def test_dispatch_declines_when_flag_off(self, config: Config) -> None:
        """`auto_dispatch_stalled` defaults to False — detection-only,
        #1441's shipped behaviour, must be unchanged by default."""
        assert config.pipeline.auto_dispatch_stalled is False
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "disabled"


class TestDispatchPerReason:
    def test_review_request_changes_no_fix_dispatches_fix(
        self, config: Config, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="request-changes"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "review_request_changes_no_fix"

        stub = MagicMock(return_value=[
            LoopAction(
                kind="fix_dispatched", assignment_id="review-1",
                detail="fix worker fix-9 dispatched to mac-mini (iteration 1/5)",
            ),
        ])
        monkeypatch.setattr("coord.auto_loop.process_review_completion", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "fix_dispatch_attempted"
        assert "fix_dispatched" in action.detail
        stub.assert_called_once()

    def test_done_no_review_dispatches_review(self, config: Config, monkeypatch) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(_work("work-1", test_state="passed"))
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "done_no_review"

        new_review = _review("work-1", aid="review-99", status="pending")
        stub = MagicMock(return_value=new_review)
        monkeypatch.setattr("coord.review.dispatch_review", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "review_dispatched"
        assert "review-99" in action.detail
        stub.assert_called_once()

    def test_approved_not_queued_enqueues(self, config: Config, monkeypatch) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )[0]
        assert detection.reason == "approved_not_queued"

        stub = MagicMock(return_value=["work-1"])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "enqueued"
        stub.assert_called_once_with(config, board)

    def test_merge_conflict_unresolved_dispatches_conflict_fix(
        self, config: Config, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        queued = [QueuedMerge(
            assignment_id="work-1", repo_name="vimcode", repo_github="acme/vimcode",
            branch="issue-602-fix", target_branch="main", issue_number=602,
            issue_title="t", state=CONFLICT, error="could not be rebased onto main",
        )]
        detection, work = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=queued
        )[0]
        assert detection.reason == "merge_conflict_unresolved"

        fix_assignment = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="[conflict-fix] t", assignment_id="cf-1", status="pending",
            type="conflict-fix",
        )
        stub = MagicMock(return_value=fix_assignment)
        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", stub)
        monkeypatch.setattr("coord.merge_queue.load_queue", lambda: queued)

        action = notify_mod.dispatch_stalled_pipeline_action(detection, work, board, config)

        assert action.kind == "conflict_fix_dispatched"
        assert "cf-1" in action.detail
        stub.assert_called_once()


class TestLiveSessionGuard:
    def test_dispatch_skipped_when_live_session_active(
        self, config: Config, monkeypatch
    ) -> None:
        """#602: never act on a row with a live (running/pending) session
        for the same (repo, issue) — an interactive smoke/fix/review
        session may be mid-flight and racing an auto-dispatch would
        duplicate or clobber it. `smoke` isn't a WORK_LIKE_TYPES type, so it
        doesn't change which assignment `_pipeline_heads` treats as the
        stalled row — it's purely a live-session signal."""
        config.pipeline.auto_dispatch_stalled = True
        work = _work("work-1", test_state="passed")
        review = _review("work-1", aid="review-1", review_verdict="approve")
        live_session = Assignment(
            machine_name="mac-mini", repo_name="vimcode", issue_number=602,
            issue_title="interactive smoke", assignment_id="smoke-live",
            status="running", type="smoke",
        )
        board = Board(active=[live_session], completed=[work, review])

        detections = notify_mod.detect_stalled_pipeline(
            config, board=board, merge_queue_items=[]
        )
        assert len(detections) == 1
        detection, work_row = detections[0]
        assert detection.reason == "approved_not_queued"

        stub = MagicMock(return_value=["work-1"])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", stub)

        action = notify_mod.dispatch_stalled_pipeline_action(
            detection, work_row, board, config
        )

        assert action.kind == "skipped_live_session"
        stub.assert_not_called()


class TestSweepStalledPipeline:
    """Integration-level tests for `_sweep_stalled_pipeline` — the function
    `run()` calls, wiring detection + post + dispatch + the one-shot ledger
    together."""

    def test_dispatches_once_and_marks_notified(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        state_mod.save_board(board)

        stub = MagicMock(return_value=["work-1"])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", stub)

        with patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            first = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})

        assert len(first) == 1
        assert first[0].reason == "approved_not_queued"
        stub.assert_called_once()
        mock_post.assert_called_once()
        args, _kwargs = mock_post.call_args
        assert "auto-dispatched" in args[2].lower()

        notified = state_mod.load_notified()
        assert "work-1:stalled" in notified

    def test_second_tick_does_not_redispatch(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        config.pipeline.auto_dispatch_stalled = True
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        state_mod.save_board(board)

        stub = MagicMock(return_value=["work-1"])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", stub)

        with patch("coord.notify.github_ops.post_issue_comment"):
            first = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})
            second = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})

        assert len(first) == 1
        assert second == []
        stub.assert_called_once()

    def test_no_dispatch_when_flag_off_default_behaviour_unchanged(
        self, config: Config, coord_db, monkeypatch
    ) -> None:
        board = _board(
            _work("work-1", test_state="passed"),
            _review("work-1", aid="review-1", review_verdict="approve"),
        )
        state_mod.save_board(board)

        stub = MagicMock(return_value=["work-1"])
        monkeypatch.setattr("coord.merge_queue.enqueue_approved_work", stub)

        with patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            posted = notify_mod._sweep_stalled_pipeline(config, terminal_cache={})

        assert len(posted) == 1
        stub.assert_not_called()
        args, _kwargs = mock_post.call_args
        assert "nothing was dispatched automatically" in args[2].lower()
