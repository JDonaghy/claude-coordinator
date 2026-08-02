"""Tests for coord.gates (#1657) — `coord gates <repo> <issue>`'s read-only
core: the raw board-column dump plus the live review/test/merge gate
decision, including #1479 staleness.
"""

from __future__ import annotations

import json

import pytest

from coord.config import Config, PipelineConfig, ReviewsConfig
from coord.gates import (
    REVIEW_REQUIRED,
    SMOKE_REQUIRED,
    build_gate_report,
    format_gate_report,
    report_to_dict,
)
from coord.models import Assignment, Board, Machine, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="precision.tailnet", repos=["api"])],
    )


def _work(
    *,
    aid: str = "w1",
    issue: int = 42,
    branch: str | None = "issue-42-foo",
    status: str = "done",
    test_state: str | None = None,
    test_reason: str | None = None,
    test_head_sha: str | None = None,
    test_base_sha: str | None = None,
    test_patch_id: str | None = None,
    review_state: str | None = None,
    review_verdict: str | None = None,
    required_gates: list[str] | None = None,
    dispatched_at: float | None = 1.0,
) -> Assignment:
    return Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=issue,
        issue_title="t",
        assignment_id=aid,
        type="work",
        status=status,
        branch=branch,
        test_state=test_state,
        test_reason=test_reason,
        test_head_sha=test_head_sha,
        test_base_sha=test_base_sha,
        test_patch_id=test_patch_id,
        review_state=review_state,
        review_verdict=review_verdict,
        required_gates=required_gates or [],
        dispatched_at=dispatched_at,
    )


def _review(
    of_aid: str,
    *,
    aid: str = "r1",
    issue: int = 42,
    verdict: str | None = "approve",
    review_head_sha: str | None = None,
    review_patch_id: str | None = None,
    dispatched_at: float | None = 2.0,
) -> Assignment:
    return Assignment(
        machine_name="dellserver",
        repo_name="api",
        issue_number=issue,
        issue_title="t",
        assignment_id=aid,
        type="review",
        status="done",
        review_of_assignment_id=of_aid,
        review_verdict=verdict,
        review_head_sha=review_head_sha,
        review_patch_id=review_patch_id,
        dispatched_at=dispatched_at,
    )


class FakeGh:
    """Stub gh_ops — returns fixed SHAs/patch-ids, records calls made."""

    def __init__(self, *, branch_sha="branchsha", base_sha="basesha", patch_id="patchid"):
        self.branch_sha = branch_sha
        self.base_sha = base_sha
        self.patch_id = patch_id
        self.sha_calls: list[tuple[str, str]] = []
        self.patch_calls: list[tuple[str, str, str]] = []

    def get_branch_sha(self, repo: str, branch: str) -> str | None:
        self.sha_calls.append((repo, branch))
        return self.branch_sha if branch != "main" else self.base_sha

    def get_branch_patch_id(self, repo: str, base: str, branch: str) -> str | None:
        self.patch_calls.append((repo, base, branch))
        return self.patch_id


# ── raw column dump ─────────────────────────────────────────────────────────

class TestRows:
    def test_no_assignments_found(self, config: Config) -> None:
        board = Board(active=[], completed=[])
        report = build_gate_report(board, config, "api", 42)
        assert report.rows == []
        assert report.decisions == []
        assert any("no assignments found" in n for n in report.notes)

    def test_row_dump_matches_assignment_columns(self, config: Config) -> None:
        work = _work(
            test_state="passed", test_reason="headless smoke",
            review_state="done", review_verdict="approve",
        )
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, config, "api", 42)
        assert len(report.rows) == 1
        row = report.rows[0]
        assert row.assignment_id == "w1"
        assert row.type == "work"
        assert row.status == "done"
        assert row.branch == "issue-42-foo"
        assert row.test_state == "passed"
        assert row.test_reason == "headless smoke"
        assert row.review_state == "done"
        assert row.review_verdict == "approve"
        assert row.review_of_assignment_id is None

    def test_rows_scoped_to_repo_and_issue(self, config: Config) -> None:
        matching = _work(aid="w1", issue=42)
        other_issue = _work(aid="w2", issue=99)
        other_repo = Assignment(
            machine_name="m", repo_name="shared", issue_number=42,
            issue_title="t", assignment_id="w3", type="work",
        )
        board = Board(active=[], completed=[matching, other_issue, other_repo])
        report = build_gate_report(board, config, "api", 42)
        assert [r.assignment_id for r in report.rows] == ["w1"]

    def test_rows_sorted_chronologically(self, config: Config) -> None:
        first = _work(aid="w1", dispatched_at=5.0)
        second = _work(aid="fix-w1", dispatched_at=1.0)
        board = Board(active=[], completed=[first, second])
        report = build_gate_report(board, config, "api", 42)
        assert [r.assignment_id for r in report.rows] == ["fix-w1", "w1"]

    def test_repo_not_in_config_still_dumps_rows(self) -> None:
        # A board row whose repo_name is real, but coordinator.yml doesn't
        # (yet, or anymore) carry that repo — the raw columns must still be
        # readable even though the live gate decision can't be computed.
        bare_config = Config(repos=[], machines=[])
        work = _work()
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, bare_config, "api", 42)
        assert len(report.rows) == 1
        assert report.decisions == []
        assert any("not in coordinator.yml" in n for n in report.notes)

    def test_no_work_like_assignment_leaves_decision_empty(self, config: Config) -> None:
        review = _review("ghost")
        board = Board(active=[], completed=[review])
        report = build_gate_report(board, config, "api", 42)
        assert len(report.rows) == 1
        assert report.decisions == []
        assert any("no work-like assignment" in n for n in report.notes)

    def test_winner_with_no_branch_leaves_decision_empty(self, config: Config) -> None:
        work = _work(branch=None)
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, config, "api", 42)
        assert report.decisions == []
        assert any("no branch" in n for n in report.notes)

    def test_winner_picks_most_recently_dispatched_work_row(self, config: Config) -> None:
        # A bounce/fix chain: two work-like rows on the same issue — the gate
        # decision must track the most recent one's branch, not the first.
        original = _work(aid="w1", branch="issue-42-orig", dispatched_at=1.0)
        fix = _work(aid="fix-w1", branch="issue-42-fix", dispatched_at=2.0)
        board = Board(active=[], completed=[original, fix])
        report = build_gate_report(board, config, "api", 42)
        assert report.branch == "issue-42-fix"


# ── gate decision ────────────────────────────────────────────────────────────

class TestDecision:
    def test_review_and_test_pass_merge_ready(self, config: Config) -> None:
        work = _work(test_state="passed")
        review = _review("w1", verdict="approve")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["review"].ok is True
        assert by_gate["test"].ok is True
        assert by_gate["merge"].ok is True
        assert by_gate["merge"].reason is None
        assert any("CI checks" in n for n in report.notes)

    def test_review_not_approved_blocks_merge(self, config: Config) -> None:
        work = _work(test_state="passed")
        board = Board(active=[], completed=[work])  # no review at all
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["review"].required is True
        assert by_gate["review"].ok is False
        assert by_gate["merge"].ok is False
        assert by_gate["merge"].reason == REVIEW_REQUIRED

    def test_smoke_missing_blocks_merge(self, config: Config) -> None:
        work = _work(test_state=None)  # never tested
        review = _review("w1", verdict="approve")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["test"].required is True
        assert by_gate["test"].ok is False
        assert by_gate["test"].anchor is None  # MISSING, not STALE
        assert by_gate["merge"].reason == SMOKE_REQUIRED

    def test_stale_base_names_1479_and_shas(self, config: Config) -> None:
        # #1479: the verdict was recorded against an old base SHA; the base
        # has since moved. The branch's own head/patch-id are unchanged.
        work = _work(
            test_state="passed", test_reason="headless smoke",
            test_head_sha="branchsha", test_base_sha="oldbase",
        )
        review = _review("w1", verdict="approve", review_head_sha="branchsha")
        board = Board(active=[], completed=[work, review])
        gh = FakeGh(branch_sha="branchsha", base_sha="newbase", patch_id="samepatch")
        report = build_gate_report(board, config, "api", 42, gh_ops=gh)

        by_gate = {d.gate: d for d in report.decisions}
        test_decision = by_gate["test"]
        assert test_decision.ok is False
        assert test_decision.anchor == "base"
        assert test_decision.recorded_sha == "oldbase"
        assert test_decision.current_sha == "newbase"
        assert "#1479" not in (test_decision.reason or "")  # reason is merge_queue's own wording
        assert by_gate["merge"].reason == SMOKE_REQUIRED
        # gh_ops was actually consulted for both the branch and the base.
        assert ("acme/api", "issue-42-foo") in gh.sha_calls
        assert ("acme/api", "main") in gh.sha_calls

    def test_stale_branch_content_change(self, config: Config) -> None:
        # Branch content changed (patch-id differs) since the test ran —
        # anchor="branch", not "base".
        work = _work(
            test_state="passed",
            test_head_sha="oldbranchsha", test_base_sha="basesha",
            test_patch_id="oldpatch",
        )
        review = _review("w1", verdict="approve", review_head_sha="newbranchsha",
                          review_patch_id="newpatch")
        board = Board(active=[], completed=[work, review])
        gh = FakeGh(branch_sha="newbranchsha", base_sha="basesha", patch_id="newpatch")
        report = build_gate_report(board, config, "api", 42, gh_ops=gh)

        by_gate = {d.gate: d for d in report.decisions}
        test_decision = by_gate["test"]
        assert test_decision.ok is False
        assert test_decision.anchor == "branch"
        assert test_decision.recorded_sha == "oldbranchsha"
        assert test_decision.current_sha == "newbranchsha"

    def test_gates_disabled_merge_ready_with_no_evidence(self, config: Config) -> None:
        config.reviews = ReviewsConfig(enabled=False)
        config.pipeline = PipelineConfig(default_gates=["merge"])
        work = _work(test_state=None)  # never tested — but gate is off
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["review"].required is False
        assert by_gate["test"].required is False
        assert by_gate["merge"].ok is True

    def test_gh_ops_none_skips_live_lookups_fail_open(self, config: Config) -> None:
        # #1479-review: without gh_ops, the staleness comparison has no live
        # SHAs to compare against — the recorded verdict is trusted as-is
        # (the pre-#1479 fail-open convention), matching how has_approved_review/
        # evaluate_smoke_verdict behave when handed no gh_ops.
        work = _work(
            test_state="passed",
            test_head_sha="branchsha", test_base_sha="oldbase",
        )
        review = _review("w1", verdict="approve", review_head_sha="branchsha")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=None)

        by_gate = {d.gate: d for d in report.decisions}
        assert by_gate["test"].ok is True
        assert by_gate["merge"].ok is True
        assert report.target_branch == "main"  # falls back, no milestone lookup


# ── is_interactive enrichment (#748/#632: not an Assignment dataclass field) ─

class TestIsInteractive:
    def test_backfills_from_assignments_table(self, config: Config, coord_db) -> None:
        from coord.state import _mark_assignment_interactive_local, save_board

        work = _work()
        board = Board(active=[], completed=[work])
        save_board(board)
        _mark_assignment_interactive_local("w1")

        report = build_gate_report(board, config, "api", 42)
        assert report.rows[0].is_interactive is True

    def test_none_when_row_not_persisted(self, config: Config, coord_db) -> None:
        work = _work()
        board = Board(active=[], completed=[work])
        report = build_gate_report(board, config, "api", 42)
        assert report.rows[0].is_interactive is None


# ── formatting / JSON round-trip ────────────────────────────────────────────

class TestFormatting:
    def test_format_includes_stale_wording(self, config: Config) -> None:
        work = _work(
            test_state="passed", test_head_sha="branchsha", test_base_sha="oldbase",
        )
        review = _review("w1", verdict="approve", review_head_sha="branchsha")
        board = Board(active=[], completed=[work, review])
        gh = FakeGh(branch_sha="branchsha", base_sha="newbase", patch_id="samepatch")
        report = build_gate_report(board, config, "api", 42, gh_ops=gh)

        text = format_gate_report(report)
        assert "STALE" in text
        assert "#1479" in text
        assert "oldbase"[:7] in text
        assert "newbase"[:7] in text
        assert "BLOCKED" in text

    def test_report_to_dict_is_json_serializable(self, config: Config) -> None:
        work = _work(test_state="passed")
        review = _review("w1", verdict="approve")
        board = Board(active=[], completed=[work, review])
        report = build_gate_report(board, config, "api", 42, gh_ops=FakeGh())

        payload = report_to_dict(report)
        # Must round-trip through json.dumps with no dataclass instances left.
        text = json.dumps(payload)
        reloaded = json.loads(text)
        assert reloaded["repo_name"] == "api"
        assert reloaded["issue_number"] == 42
        assert len(reloaded["rows"]) == 2
        assert len(reloaded["decisions"]) == 3

    def test_never_mutates_board_or_calls_write_seams(
        self, config: Config, monkeypatch,
    ) -> None:
        """Read-only guarantee: build_gate_report must never call
        save_board/save_queue — the whole point of #1657 (see the sibling
        #coord-diagnose-writes issue this explicitly calls out)."""
        import coord.state as state_mod
        import coord.merge_queue as mq_mod

        def _boom(*a, **k):
            raise AssertionError("build_gate_report must never write")

        monkeypatch.setattr(state_mod, "save_board", _boom, raising=False)
        monkeypatch.setattr(mq_mod, "save_queue", _boom, raising=False)

        work = _work(test_state="passed")
        review = _review("w1", verdict="approve")
        board = Board(active=[], completed=[work, review])
        build_gate_report(board, config, "api", 42, gh_ops=FakeGh())
