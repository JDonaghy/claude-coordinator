"""Tests for compute_pipeline() and the /api/pipeline dashboard endpoints."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from coord.config import Config, PipelineConfig
from coord.dashboard.server import build_app
from coord.merge_queue import QueuedMerge, PENDING, MERGED, MERGING
from coord.models import Assignment, Board, Machine, Repo
from coord.pipeline import PipelineView, PipelineStage, PipelineGate, compute_pipeline
from coord.state import save_board


# ── Test helpers ────────────────────────────────────────────────────────────


def _config(default_gates: list[str] | None = None) -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(
            name="laptop", host="laptop.tailnet", repos=["api"],
            repo_paths={"api": "/tmp/api"},
        )],
        pipeline=PipelineConfig(
            default_gates=default_gates if default_gates is not None else ["review", "merge"],
        ),
    )


def _work(
    aid: str = "work-1",
    status: str = "running",
    smoke_test: str | None = None,
    required_gates: list[str] | None = None,
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Fix auth",
        assignment_id=aid,
        status=status,
        type="work",
        smoke_test=smoke_test,
        required_gates=required_gates if required_gates is not None else [],
    )


def _review(of_aid: str, status: str = "running", aid: str = "rev-1") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="[review] Fix auth",
        assignment_id=aid,
        status=status,
        type="review",
        review_of_assignment_id=of_aid,
    )


def _smoke(of_aid: str, status: str = "running", aid: str = "smk-1") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="[smoke] Fix auth",
        assignment_id=aid,
        status=status,
        type="smoke",
        review_of_assignment_id=of_aid,
    )


def _mq_entry(
    aid: str = "work-1",
    state: str = PENDING,
) -> QueuedMerge:
    return QueuedMerge(
        assignment_id=aid,
        repo_name="api",
        repo_github="acme/api",
        branch="issue-42-fix",
        target_branch="main",
        issue_number=42,
        issue_title="Fix auth",
        state=state,
    )


def _board(*assignments: Assignment) -> Board:
    active = [a for a in assignments if a.status in ("running", "pending")]
    completed = [a for a in assignments if a.status not in ("running", "pending")]
    return Board(active=active, completed=completed)


# ── Stage transition tests ───────────────────────────────────────────────────


class TestComputePipeline:
    def test_running_assignment_gives_coding_stage(self) -> None:
        a = _work(status="running")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "coding"
        coding = next(s for s in pv.stages if s.name == "coding")
        assert coding.status == "active"
        assert coding.is_current

    def test_pipeline_view_carries_issue_title_and_machine_name(self) -> None:
        """PipelineView must expose issue_title and machine_name so the dashboard
        card can render without a second API call."""
        a = _work(status="running")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.issue_title == "Fix auth"
        assert pv.machine_name == "laptop"

    def test_done_no_downstream_gives_done_stage(self) -> None:
        a = _work(status="done")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "done"
        # default_gates=["review", "merge"] — review offered, smoke not offered
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_review" in gate_actions
        assert "dispatch_smoke" not in gate_actions  # "smoke" not in default_gates
        assert "enqueue" in gate_actions

    def test_done_with_active_review_gives_review_running(self) -> None:
        a = _work(status="done")
        rev = _review(of_aid="work-1", status="running")
        board = _board(rev)
        board.completed.append(a)
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "review_running"
        review = next(s for s in pv.stages if s.name == "review")
        assert review.status == "active"
        assert review.is_current

    def test_done_with_completed_review_gives_review_done(self) -> None:
        a = _work(status="done")
        rev = _review(of_aid="work-1", status="done")
        board = Board(active=[], completed=[a, rev])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "review_done"
        review = next(s for s in pv.stages if s.name == "review")
        assert review.status == "completed"
        assert review.is_current
        # Gate: queue for merge
        gate_actions = {g.action for g in pv.available_gates}
        assert "enqueue" in gate_actions

    def test_smoke_test_pass_gives_smoke_passed(self) -> None:
        a = _work(status="done", smoke_test="pass")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "smoke_passed"
        smoke = next(s for s in pv.stages if s.name == "smoke")
        # smoke is skipped (not in default required_gates = ["review", "merge"])
        assert smoke.status == "skipped"
        # Gates should offer enqueue
        gate_actions = {g.action for g in pv.available_gates}
        assert "enqueue" in gate_actions

    def test_smoke_test_pass_with_smoke_gate(self) -> None:
        """smoke_passed when 'smoke' is in required_gates → smoke stage completed."""
        a = _work(status="done", smoke_test="pass", required_gates=["smoke", "merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "smoke_passed"
        smoke = next(s for s in pv.stages if s.name == "smoke")
        assert smoke.status == "completed"

    def test_smoke_test_fail_gives_smoke_failed(self) -> None:
        a = _work(status="done", smoke_test="fail")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "smoke_failed"
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_fix" in gate_actions

    def test_active_smoke_assignment_gives_smoke_running(self) -> None:
        a = _work(status="done")
        smk = _smoke(of_aid="work-1", status="running")
        board = Board(active=[smk], completed=[a])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "smoke_running"

    def test_merge_queue_pending_gives_merge_ready(self) -> None:
        a = _work(status="done")
        mq = [_mq_entry(state=PENDING)]
        pv = compute_pipeline(a, _board(a), mq, _config())
        assert pv.current_stage == "merge_ready"
        merge = next(s for s in pv.stages if s.name == "merge")
        assert merge.status == "active"
        assert merge.is_current
        gate_actions = {g.action for g in pv.available_gates}
        assert "merge" in gate_actions

    def test_merge_queue_merging_gives_merging(self) -> None:
        a = _work(status="done")
        mq = [_mq_entry(state=MERGING)]
        pv = compute_pipeline(a, _board(a), mq, _config())
        assert pv.current_stage == "merging"

    def test_merge_queue_merged_gives_merged(self) -> None:
        a = _work(status="done")
        mq = [_mq_entry(state=MERGED)]
        pv = compute_pipeline(a, _board(a), mq, _config())
        assert pv.current_stage == "merged"
        assert pv.progress_pct == 100
        merge = next(s for s in pv.stages if s.name == "merge")
        assert merge.status == "completed"

    def test_failed_assignment_gives_failed_stage(self) -> None:
        a = _work(status="failed")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "failed"
        coding = next(s for s in pv.stages if s.name == "coding")
        assert coding.is_current
        gate_actions = {g.action for g in pv.available_gates}
        assert "retry" in gate_actions

    def test_label_override_merge_only_skips_review_smoke(self) -> None:
        """required_gates=["merge"] — review and smoke stages are skipped."""
        a = _work(status="done", required_gates=["merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        review = next(s for s in pv.stages if s.name == "review")
        smoke = next(s for s in pv.stages if s.name == "smoke")
        merge = next(s for s in pv.stages if s.name == "merge")
        assert review.status == "skipped"
        assert smoke.status == "skipped"
        assert merge.status == "waiting"  # next action after done

    def test_required_gates_from_config_default_when_empty(self) -> None:
        """Empty required_gates on assignment → fall back to config.pipeline.default_gates."""
        cfg = _config(default_gates=["merge"])
        a = _work(status="done", required_gates=[])  # empty = use config default
        pv = compute_pipeline(a, _board(a), [], cfg)
        review = next(s for s in pv.stages if s.name == "review")
        assert review.status == "skipped"  # "review" not in ["merge"]

    def test_progress_pct_increases_through_pipeline(self) -> None:
        a_running = _work(status="running")
        a_done = _work(status="done")
        a_merged = _work(status="done")
        mq = [_mq_entry(state=MERGED)]

        pv_running = compute_pipeline(a_running, _board(a_running), [], _config())
        pv_done = compute_pipeline(a_done, _board(a_done), [], _config())
        pv_merged = compute_pipeline(a_merged, _board(a_merged), mq, _config())

        assert pv_running.progress_pct < pv_done.progress_pct
        assert pv_done.progress_pct < pv_merged.progress_pct
        assert pv_merged.progress_pct == 100

    def test_pipeline_view_contains_all_four_stages(self) -> None:
        a = _work(status="running")
        pv = compute_pipeline(a, _board(a), [], _config())
        stage_names = [s.name for s in pv.stages]
        assert stage_names == ["coding", "review", "smoke", "merge"]

    def test_available_gates_have_correct_endpoint(self) -> None:
        a = _work(status="done")
        pv = compute_pipeline(a, _board(a), [], _config())
        for gate in pv.available_gates:
            assert gate.endpoint == "/api/pipeline/action"

    # ── Issue #1: failed review → review_failed ──────────────────────────────

    def test_failed_review_gives_review_failed_stage(self) -> None:
        """A review assignment with status='failed' must yield review_failed,
        not review_done (which would incorrectly show 'Queue for Merge')."""
        a = _work(status="done")
        rev = _review(of_aid="work-1", status="failed")
        board = Board(active=[], completed=[a, rev])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "review_failed"
        review = next(s for s in pv.stages if s.name == "review")
        assert review.status == "active"
        assert review.is_current
        # Gate: re-dispatch review (not enqueue)
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_review" in gate_actions
        assert "enqueue" not in gate_actions

    # ── Issue #3: failed smoke assignment → smoke_failed ─────────────────────

    def test_failed_smoke_assignment_gives_smoke_failed(self) -> None:
        """A smoke assignment with status='failed' (infra failure) must yield
        smoke_failed, not silently fall through to check review_assignment."""
        a = _work(status="done")
        smk = _smoke(of_aid="work-1", status="failed")
        board = Board(active=[], completed=[a, smk])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "smoke_failed"
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_fix" in gate_actions

    def test_failed_smoke_assignment_does_not_fall_through_to_review(self) -> None:
        """Ensure a failed smoke assignment isn't confused with no smoke at all."""
        a = _work(status="done")
        rev = _review(of_aid="work-1", status="done")
        smk = _smoke(of_aid="work-1", status="failed")
        board = Board(active=[], completed=[a, rev, smk])
        pv = compute_pipeline(a, board, [], _config())
        # smoke_failed takes priority over review state
        assert pv.current_stage == "smoke_failed"

    # ── Issue #5: available_gates filtered by required_gates ─────────────────

    def test_done_with_all_gates_shows_review_and_smoke(self) -> None:
        """required_gates=["review","smoke","merge"] → both review and smoke gates."""
        a = _work(status="done", required_gates=["review", "smoke", "merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_review" in gate_actions
        assert "dispatch_smoke" in gate_actions
        assert "enqueue" in gate_actions

    def test_done_merge_only_shows_enqueue_not_review_or_smoke(self) -> None:
        """required_gates=["merge"] → only enqueue, no review or smoke gates."""
        a = _work(status="done", required_gates=["merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_review" not in gate_actions
        assert "dispatch_smoke" not in gate_actions
        assert "enqueue" in gate_actions

    def test_done_review_only_gates_shows_only_review_and_enqueue(self) -> None:
        """required_gates=["review","merge"] → review + enqueue, no smoke."""
        a = _work(status="done", required_gates=["review", "merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_review" in gate_actions
        assert "dispatch_smoke" not in gate_actions
        assert "enqueue" in gate_actions


# ── Required-gates persistence ──────────────────────────────────────────────


class TestRequiredGatesPersistence:
    def test_save_and_load_board_preserves_required_gates(self, coord_db) -> None:
        from coord.state import load_board, save_board

        a = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Test",
            assignment_id="abc",
            status="done",
            type="work",
            required_gates=["merge"],
        )
        board = Board(completed=[a])
        save_board(board)

        loaded = load_board()
        assert loaded is not None
        assert loaded.completed[0].required_gates == ["merge"]

    def test_build_board_from_ledger_preserves_required_gates(
        self, coord_db
    ) -> None:
        from coord.models import Proposal
        from coord.state import build_board, record_dispatched

        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=5,
            issue_title="Ledger test",
            rationale="",
            required_gates=["merge"],
        )
        record_dispatched(
            assignment_id="xyz",
            proposal=p,
            repo_github="acme/api",
        )
        board = build_board()
        assert board.active[0].required_gates == ["merge"]


# ── Dashboard API tests ──────────────────────────────────────────────────────


def _dashboard_client(cfg: Config | None = None):
    from starlette.testclient import TestClient

    return TestClient(build_app(cfg or _config()))


class TestPipelineAPI:
    def test_get_pipeline_returns_list(self) -> None:
        board = Board(
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=1, issue_title="Running",
                    assignment_id="a1", status="running", type="work",
                ),
            ],
            completed=[],
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 1
        pv = data[0]
        assert pv["assignment_id"] == "a1"
        assert pv["current_stage"] == "coding"
        assert "stages" in pv
        assert "available_gates" in pv
        assert "progress_pct" in pv
        # Fields added in #701 so the dashboard card renders without a 2nd call.
        assert pv["issue_title"] == "Running"
        assert pv["machine_name"] == "laptop"

    def test_get_pipeline_excludes_review_type(self) -> None:
        board = Board(
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=1, issue_title="Work",
                    assignment_id="w1", status="running", type="work",
                ),
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=1, issue_title="Review",
                    assignment_id="r1", status="running", type="review",
                ),
            ],
            completed=[],
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        data = r.json()
        # Only work assignments returned
        ids = [pv["assignment_id"] for pv in data]
        assert "w1" in ids
        assert "r1" not in ids

    def test_get_pipeline_empty_board(self) -> None:
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=None),
            patch("coord.dashboard.server.build_board", return_value=Board()),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        assert r.json() == []

    def test_pipeline_stages_structure(self) -> None:
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=2, issue_title="Done work",
                assignment_id="w2", status="done", type="work",
            ),
        ])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        data = r.json()
        assert len(data) == 1
        pv = data[0]
        stage_names = [s["name"] for s in pv["stages"]]
        assert stage_names == ["coding", "review", "smoke", "merge"]
        # Each stage has required fields
        for s in pv["stages"]:
            assert "name" in s
            assert "status" in s
            assert "is_current" in s


class TestPipelineActionAPI:
    def test_missing_fields_returns_400(self) -> None:
        client = _dashboard_client()
        r = client.post("/api/pipeline/action", json={"assignment_id": "x"})
        assert r.status_code == 400

    def test_unknown_assignment_returns_404(self) -> None:
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=Board()),
            patch("coord.dashboard.server.build_board", return_value=Board()),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "nonexistent", "action": "enqueue"},
            )
        assert r.status_code == 404

    def test_invalid_json_returns_400(self) -> None:
        client = _dashboard_client()
        r = client.post(
            "/api/pipeline/action",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400

    def test_unknown_action_returns_400(self) -> None:
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=1, issue_title="t",
                assignment_id="a1", status="running", type="work",
            ),
        ])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "bogus_action"},
            )
        assert r.status_code == 400

    def test_enqueue_action_calls_enqueue(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="done", type="work",
            branch="feat/x",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.merge_queue.save_queue") as mock_save,
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "enqueue"},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        mock_save.assert_called_once()

    def test_retry_returns_501(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="failed", type="work",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with patch("coord.dashboard.server.load_board", return_value=board):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "retry"},
            )
        assert r.status_code == 501

    def test_merge_not_in_queue_returns_404(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="done", type="work",
            branch="feat/x",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "merge"},
            )
        assert r.status_code == 404

    def test_dispatch_review_action_succeeds(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="done", type="work",
            branch="feat/x",
        )
        board = Board(completed=[a])
        mock_review = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="[review] t",
            assignment_id="rev-1", status="running", type="review",
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.dashboard.server.save_board"),
            patch("coord.review.dispatch_review", return_value=mock_review),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "dispatch_review"},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_dispatch_fix_from_test_fail_succeeds(self) -> None:
        """dispatch_fix with parent_type=work dispatches a headless fix worker."""
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Break auth",
            assignment_id="w1", status="done", type="work",
            branch="issue-1-break-auth",
            smoke_test="fail",
            test_reason="AssertionError on line 42",
        )
        board = Board(completed=[a])
        mock_fix = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="[fix-1] Break auth",
            assignment_id="fix-1", status="running", type="work",
            branch="issue-1-break-auth",
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.dashboard.server.save_board"),
            patch("coord.review.dispatch_headless_fix", return_value=mock_fix) as mock_dhf,
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w1", "action": "dispatch_fix",
                      "parent_type": "work"},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["branch"] == "issue-1-break-auth"
        assert data["assignment_id"] == "fix-1"
        mock_dhf.assert_called_once()
        _, call_kwargs = mock_dhf.call_args
        assert call_kwargs["parent_type"] == "work"

    def test_dispatch_fix_from_request_changes_succeeds(self) -> None:
        """dispatch_fix with parent_type=review dispatches a headless fix worker."""
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=2, issue_title="Add logging",
            assignment_id="w2", status="done", type="work",
            branch="issue-2-add-logging",
        )
        board = Board(completed=[a])
        mock_fix = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=2, issue_title="[fix-1] Add logging",
            assignment_id="fix-2", status="running", type="work",
            branch="issue-2-add-logging",
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.dashboard.server.save_board"),
            patch("coord.review.dispatch_headless_fix", return_value=mock_fix) as mock_dhf,
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w2", "action": "dispatch_fix",
                      "parent_type": "review"},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["branch"] == "issue-2-add-logging"
        _, call_kwargs = mock_dhf.call_args
        assert call_kwargs["parent_type"] == "review"

    def test_dispatch_fix_invalid_parent_type_returns_400(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="w1", status="done", type="work",
            branch="issue-1-t",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with patch("coord.dashboard.server.load_board", return_value=board):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w1", "action": "dispatch_fix",
                      "parent_type": "bogus"},
            )
        assert r.status_code == 400

    def test_dispatch_fix_no_branch_returns_400(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="w1", status="done", type="work",
            branch=None,
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with patch("coord.dashboard.server.load_board", return_value=board):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w1", "action": "dispatch_fix"},
            )
        assert r.status_code == 400

    def test_dispatch_fix_returns_501_replaced_by_implementation(self) -> None:
        """Confirm the old 501 stub is gone — dispatch_fix no longer returns 501."""
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="w1", status="done", type="work",
            branch="issue-1-t",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        mock_fix = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="[fix-1] t",
            assignment_id="fx-1", status="running", type="work",
            branch="issue-1-t",
        )
        with (
            patch("coord.dashboard.server.load_board", return_value=board),
            patch("coord.dashboard.server.save_board"),
            patch("coord.review.dispatch_headless_fix", return_value=mock_fix),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w1", "action": "dispatch_fix"},
            )
        assert r.status_code != 501


# ── dispatch_headless_fix unit tests ────────────────────────────────────────


class TestDispatchHeadlessFix:
    """Unit tests for coord.review.dispatch_headless_fix.

    These tests mock _dispatch_fix (the agent HTTP call) and verify that:
    - The correct briefing text is assembled for each parent_type.
    - The existing branch is passed as target_branch (not a fresh branch).
    - Iteration accounting is correct.
    - Guard conditions (no branch, terminal, max-iter) short-circuit cleanly.
    """

    def _make_config(self) -> Config:
        from coord.config import PipelineConfig
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            )],
            pipeline=PipelineConfig(default_gates=["review", "merge"]),
        )

    def test_test_fail_briefing_contains_reason(self) -> None:
        """Briefing for parent_type=work includes the operator's test_reason."""
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=5, issue_title="Cache bug",
            assignment_id="w5", status="done", type="work",
            branch="issue-5-cache-bug",
            smoke_test="fail",
            test_reason="KeyError in cache.get()",
        )
        board = Board(completed=[work])
        config = self._make_config()

        captured: dict = {}

        def fake_dispatch(w, briefing, b, cfg, iteration, *, model=None, http_client=None):
            captured["briefing"] = briefing
            captured["branch"] = w.branch
            captured["iteration"] = iteration
            fix = Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=5, issue_title="[fix-1] Cache bug",
                assignment_id="fx-5", status="running", type="work",
                branch=w.branch,
            )
            b.active.append(fix)
            return fix

        with (
            patch("coord.auto_loop._dispatch_fix", fake_dispatch),
            patch("coord.auto_loop._work_is_terminal", return_value=False),
            patch("coord.state.issue_context_block", return_value=""),
        ):
            result = dispatch_headless_fix(work, board, config, parent_type="work")

        assert result is not None
        assert result.branch == "issue-5-cache-bug"
        assert "KeyError in cache.get()" in captured["briefing"]
        assert "FAILED" in captured["briefing"]
        assert captured["iteration"] == 1

    def test_test_fail_briefing_fallback_when_no_reason(self) -> None:
        """Briefing for parent_type=work without test_reason uses generic text."""
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=6, issue_title="Slow query",
            assignment_id="w6", status="done", type="work",
            branch="issue-6-slow-query",
            smoke_test="fail",
            test_reason=None,
        )
        board = Board(completed=[work])
        config = self._make_config()

        captured: dict = {}

        def fake_dispatch(w, briefing, b, cfg, iteration, *, model=None, http_client=None):
            captured["briefing"] = briefing
            fix = Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=6, issue_title="[fix-1] Slow query",
                assignment_id="fx-6", status="running", type="work",
                branch=w.branch,
            )
            b.active.append(fix)
            return fix

        with (
            patch("coord.auto_loop._dispatch_fix", fake_dispatch),
            patch("coord.auto_loop._work_is_terminal", return_value=False),
            patch("coord.state.issue_context_block", return_value=""),
        ):
            result = dispatch_headless_fix(work, board, config, parent_type="work")

        assert result is not None
        assert "FAILED" in captured["briefing"]
        assert "no reason" in captured["briefing"]

    def test_review_parent_type_loads_findings_and_builds_briefing(self) -> None:
        """Briefing for parent_type=review contains the review findings body."""
        from coord.review import dispatch_headless_fix, ReviewFindings

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=7, issue_title="Auth fix",
            assignment_id="w7", status="done", type="work",
            branch="issue-7-auth-fix",
        )
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=7, issue_title="[review] Auth fix",
            assignment_id="rev-7", status="done", type="review",
            review_of_assignment_id="w7",
            review_verdict="request-changes",
        )
        board = Board(completed=[work, rev])
        config = self._make_config()

        captured: dict = {}

        def fake_dispatch(w, briefing, b, cfg, iteration, *, model=None, http_client=None):
            captured["briefing"] = briefing
            fix = Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=7, issue_title="[fix-1] Auth fix",
                assignment_id="fx-7", status="running", type="work",
                branch=w.branch,
            )
            b.active.append(fix)
            return fix

        fake_findings = ReviewFindings(
            verdict="request-changes",
            body="## Blocking\n- Missing input validation on /login",
        )

        with (
            patch("coord.auto_loop._dispatch_fix", fake_dispatch),
            patch("coord.auto_loop._work_is_terminal", return_value=False),
            patch("coord.auto_loop._load_review_findings", return_value=fake_findings),
            patch("coord.state.issue_context_block", return_value=""),
        ):
            result = dispatch_headless_fix(work, board, config, parent_type="review")

        assert result is not None
        assert result.branch == "issue-7-auth-fix"
        assert "Missing input validation" in captured["briefing"]
        # Verify the briefing instructs to stay on the same branch.
        assert "issue-7-auth-fix" in captured["briefing"]

    def test_review_parent_type_fallback_briefing_when_no_findings(self) -> None:
        """When findings can't be loaded, a generic fallback briefing is used."""
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=8, issue_title="Rate limiting",
            assignment_id="w8", status="done", type="work",
            branch="issue-8-rate-limiting",
        )
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=8, issue_title="[review] Rate limiting",
            assignment_id="rev-8", status="done", type="review",
            review_of_assignment_id="w8",
            review_verdict="request-changes",
        )
        board = Board(completed=[work, rev])
        config = self._make_config()

        captured: dict = {}

        def fake_dispatch(w, briefing, b, cfg, iteration, *, model=None, http_client=None):
            captured["briefing"] = briefing
            fix = Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=8, issue_title="[fix-1] Rate limiting",
                assignment_id="fx-8", status="running", type="work",
                branch=w.branch,
            )
            b.active.append(fix)
            return fix

        with (
            patch("coord.auto_loop._dispatch_fix", fake_dispatch),
            patch("coord.auto_loop._work_is_terminal", return_value=False),
            patch("coord.auto_loop._load_review_findings", return_value=None),
            patch("coord.state.issue_context_block", return_value=""),
        ):
            result = dispatch_headless_fix(work, board, config, parent_type="review")

        assert result is not None
        # Fallback text should mention the review assignment and the verdict.
        assert "rev-8" in captured["briefing"]
        assert "request-changes" in captured["briefing"]

    def test_no_branch_returns_none(self) -> None:
        """Returns None when work has no branch (can't continue)."""
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=9, issue_title="No branch",
            assignment_id="w9", status="done", type="work",
            branch=None,
        )
        board = Board(completed=[work])
        config = self._make_config()

        result = dispatch_headless_fix(work, board, config, parent_type="work")
        assert result is None

    def test_max_iterations_returns_none(self) -> None:
        """Returns None when the review_iteration has already hit the limit."""
        from coord.review import dispatch_headless_fix
        from coord.config import PipelineConfig

        config = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            )],
            pipeline=PipelineConfig(
                default_gates=["review", "merge"],
                max_review_iterations=2,
            ),
        )
        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Maxed out",
            assignment_id="w10", status="done", type="work",
            branch="issue-10-maxed-out",
            # Already at 2 fix iterations; next would be 3 > max=2.
            review_iteration=2,
        )
        board = Board(completed=[work])

        with patch("coord.auto_loop._work_is_terminal", return_value=False):
            result = dispatch_headless_fix(work, board, config, parent_type="work")
        assert result is None

    def test_review_parent_type_no_linked_review_returns_none(self) -> None:
        """Returns None when parent_type=review but no linked review on board."""
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=11, issue_title="Orphaned",
            assignment_id="w11", status="done", type="work",
            branch="issue-11-orphaned",
        )
        # No review assignment on the board linked to w11.
        board = Board(completed=[work])
        config = self._make_config()

        with patch("coord.auto_loop._work_is_terminal", return_value=False):
            result = dispatch_headless_fix(work, board, config, parent_type="review")
        assert result is None

    def test_target_branch_is_existing_branch_not_fresh(self) -> None:
        """The fix worker targets the EXISTING issue branch, not a fresh one.

        This is the core correctness guarantee: the agent payload must carry
        ``target_branch=work.branch`` so the worker adds commits to the
        reviewed branch rather than branching off main.  We verify by
        inspecting what _dispatch_fix receives as its first argument (work)
        and confirming the branch matches the original work branch.
        """
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=12, issue_title="Branch check",
            assignment_id="w12", status="done", type="work",
            branch="issue-12-branch-check",
            smoke_test="fail",
            test_reason="test broke",
        )
        board = Board(completed=[work])
        config = self._make_config()

        dispatched_work_branch: list[str] = []

        def fake_dispatch(w, briefing, b, cfg, iteration, *, model=None, http_client=None):
            dispatched_work_branch.append(w.branch or "")
            fix = Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=12, issue_title="[fix-1] Branch check",
                assignment_id="fx-12", status="running", type="work",
                branch=w.branch,
            )
            b.active.append(fix)
            return fix

        with (
            patch("coord.auto_loop._dispatch_fix", fake_dispatch),
            patch("coord.auto_loop._work_is_terminal", return_value=False),
            patch("coord.state.issue_context_block", return_value=""),
        ):
            result = dispatch_headless_fix(work, board, config, parent_type="work")

        assert result is not None
        # The work object passed to _dispatch_fix must carry the ORIGINAL branch —
        # _dispatch_fix sets target_branch=work.branch in the agent payload.
        assert dispatched_work_branch == ["issue-12-branch-check"]
        assert result.branch == "issue-12-branch-check"


# ── pipeline.py gate: dispatch_fix for request-changes review ───────────────


class TestDispatchFixGateForRequestChanges:
    """Verify compute_pipeline exposes dispatch_fix when review verdict is
    request-changes so the phone knows the action is available (#699)."""

    def test_review_done_request_changes_shows_dispatch_fix(self) -> None:
        """dispatch_fix gate appears when review verdict is request-changes."""
        work = _work(aid="w1", status="done")
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="[review] Fix auth",
            assignment_id="rev-1", status="done", type="review",
            review_of_assignment_id="w1",
            review_verdict="request-changes",
        )
        board = Board(completed=[work, rev])
        pv = compute_pipeline(work, board, [], _config())
        assert pv.current_stage == "review_done"
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_fix" in gate_actions

    def test_review_done_approved_does_not_show_dispatch_fix(self) -> None:
        """dispatch_fix gate must NOT appear when review verdict is approve."""
        work = _work(aid="w1", status="done")
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="[review] Fix auth",
            assignment_id="rev-1", status="done", type="review",
            review_of_assignment_id="w1",
            review_verdict="approve",
        )
        board = Board(completed=[work, rev])
        pv = compute_pipeline(work, board, [], _config())
        assert pv.current_stage == "review_done"
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_fix" not in gate_actions
        assert "enqueue" in gate_actions  # Merge gate still available.

    def test_review_done_no_verdict_does_not_show_dispatch_fix(self) -> None:
        """dispatch_fix gate must NOT appear when review verdict is unknown/None."""
        work = _work(aid="w1", status="done")
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="[review] Fix auth",
            assignment_id="rev-1", status="done", type="review",
            review_of_assignment_id="w1",
            review_verdict=None,
        )
        board = Board(completed=[work, rev])
        pv = compute_pipeline(work, board, [], _config())
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_fix" not in gate_actions
