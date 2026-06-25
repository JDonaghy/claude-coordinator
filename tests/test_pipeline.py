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
