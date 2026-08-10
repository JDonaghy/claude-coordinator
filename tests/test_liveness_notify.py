"""Black-box tests for #2048's ``coord.notify.detect_liveness_stall`` /
``post_liveness_stall`` — the running path that wires the cheap per-turn
liveness auditor (``coord/liveness_auditor.py``) into the same detect/post
shape ``detect_needs_attention``/``detect_stalled_pipeline`` already use.

Mirrors ``tests/test_needs_attention.py``'s fixtures. ``run_audit`` (the
only thing that actually spawns a subprocess) is mocked throughout — these
tests exercise everything AROUND it: debounce, the three-strike boundary,
context isolation (no STATUS:/STUCK: lines reach the audit), one-shot
notification, and — the acceptance-bar invariant — that raising the event
never touches board status/review_state/test_state.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from coord import notify as notify_mod
from coord import state as state_mod
from coord.comments import EVENT_LIVENESS_STALL
from coord.config import Config, LivenessAuditorConfig, PipelineConfig
from coord.liveness_auditor import AuditOutcome
from coord.models import Proposal, Repo, Machine


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
        ],
        pipeline=PipelineConfig(
            liveness_auditor=LivenessAuditorConfig(
                enabled=True, strikes=3, debounce_seconds=60.0,
            ),
        ),
    )


@pytest.fixture
def disabled_config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[
            Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
        ],
        pipeline=PipelineConfig(),  # liveness_auditor.enabled defaults False
    )


def _record(assignment_id: str, *, briefing: str = "Fix the flaky test") -> None:
    proposal = Proposal(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Fix the flaky test",
        rationale="r",
        files_likely=["src/a.py"],
        briefing=briefing,
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id, proposal=proposal, repo_github="acme/api",
    )


def _log_with_turn(tmp_path: Path, text: str, *, name: str = "log.jsonl") -> Path:
    p = tmp_path / name
    p.write_text(
        json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }) + "\n"
    )
    return p


def _active_status(assignment_id: str, log_path: Path) -> dict:
    return {"active": [{"id": assignment_id, "log_path": str(log_path)}]}


# ── disabled by default ──────────────────────────────────────────────────────


class TestDisabledByDefault:
    def test_no_op_when_disabled(self, tmp_path: Path, disabled_config: Config) -> None:
        _record("abc123")
        log_path = _log_with_turn(tmp_path, "editing files")
        with patch.object(
            notify_mod, "_agent_status",
            return_value=_active_status("abc123", log_path),
        ), patch("coord.liveness_auditor.run_audit") as mock_audit:
            results = notify_mod.detect_liveness_stall(disabled_config)
        assert results == []
        mock_audit.assert_not_called()


# ── three-strike boundary ────────────────────────────────────────────────────


class TestThreeStrikeBoundary:
    def test_two_blocked_silent_third_raises(
        self, tmp_path: Path, config: Config
    ) -> None:
        _record("abc123")
        log_path = _log_with_turn(tmp_path, "repeating the same failed command again")
        t0 = 1_000_000.0

        with patch.object(
            notify_mod, "_agent_status",
            return_value=_active_status("abc123", log_path),
        ), patch(
            "coord.liveness_auditor.run_audit",
            return_value=AuditOutcome(verdict="blocked", raw_output="blocked"),
        ), patch.object(notify_mod, "github_ops") as mock_gh:
            r1 = notify_mod.detect_liveness_stall(config, now=t0)
            assert r1 == []
            r2 = notify_mod.detect_liveness_stall(config, now=t0 + 61)
            assert r2 == []
            r3 = notify_mod.detect_liveness_stall(config, now=t0 + 122)

        assert len(r3) == 1
        detection, record = r3[0]
        assert detection.assignment_id == "abc123"
        assert detection.consecutive_blocked == 3
        assert record["repo_github"] == "acme/api"
        # detect_liveness_stall is detection-only — no comment posted yet.
        mock_gh.post_issue_comment.assert_not_called()

    def test_continue_verdict_resets_streak(self, tmp_path: Path, config: Config) -> None:
        _record("abc123")
        log_path = _log_with_turn(tmp_path, "made real progress")
        t0 = 2_000_000.0

        verdicts = ["blocked", "blocked", "continue", "blocked", "blocked"]
        with patch.object(
            notify_mod, "_agent_status",
            return_value=_active_status("abc123", log_path),
        ), patch(
            "coord.liveness_auditor.run_audit",
            side_effect=[
                AuditOutcome(verdict=v, raw_output=v) for v in verdicts
            ],
        ):
            results = []
            for i in range(5):
                results.append(
                    notify_mod.detect_liveness_stall(config, now=t0 + i * 61)
                )
        # Never reached 3 in a row (2, then reset, then 2) — never raises.
        assert all(r == [] for r in results)

    def test_raised_assignment_is_not_audited_again(
        self, tmp_path: Path, config: Config
    ) -> None:
        """Once raised, the ledger entry is one-shot — no further subprocess
        spawns for this assignment even though it's still running and
        still blocked."""
        _record("abc123")
        log_path = _log_with_turn(tmp_path, "stuck in a loop")
        t0 = 3_000_000.0

        with patch.object(
            notify_mod, "_agent_status",
            return_value=_active_status("abc123", log_path),
        ), patch(
            "coord.liveness_auditor.run_audit",
            return_value=AuditOutcome(verdict="blocked", raw_output="blocked"),
        ) as mock_audit:
            for i in range(3):
                notify_mod.detect_liveness_stall(config, now=t0 + i * 61)
            assert mock_audit.call_count == 3

            # detect + post, mirroring how coord.notify.run() would drive it.
            state_mod.mark_notified(
                notify_mod._liveness_notified_key("abc123"), EVENT_LIVENESS_STALL,
            )
            results = notify_mod.detect_liveness_stall(config, now=t0 + 4 * 61)
            assert results == []
            assert mock_audit.call_count == 3  # no new spawn


# ── debounce ──────────────────────────────────────────────────────────────────


class TestDebounce:
    def test_second_call_within_window_skips_audit(
        self, tmp_path: Path, config: Config
    ) -> None:
        _record("abc123")
        log_path = _log_with_turn(tmp_path, "working")
        t0 = 4_000_000.0

        with patch.object(
            notify_mod, "_agent_status",
            return_value=_active_status("abc123", log_path),
        ), patch(
            "coord.liveness_auditor.run_audit",
            return_value=AuditOutcome(verdict="continue", raw_output="continue"),
        ) as mock_audit:
            notify_mod.detect_liveness_stall(config, now=t0)
            notify_mod.detect_liveness_stall(config, now=t0 + 5)  # well under 60s
            assert mock_audit.call_count == 1


# ── context isolation ────────────────────────────────────────────────────────


class TestContextIsolation:
    def test_status_and_stuck_lines_are_stripped_before_audit(
        self, tmp_path: Path, config: Config
    ) -> None:
        turn = (
            "editing coord/foo.py\n"
            "STATUS: definitely on track → next: ship it → confidence: high\n"
            "STUCK: actually nothing works\n"
        )
        _record("abc123", briefing="Fix the flaky test")
        log_path = _log_with_turn(tmp_path, turn)
        with patch.object(
            notify_mod, "_agent_status",
            return_value=_active_status("abc123", log_path),
        ), patch(
            "coord.liveness_auditor.run_audit",
            return_value=AuditOutcome(verdict="continue", raw_output="continue"),
        ) as mock_audit:
            notify_mod.detect_liveness_stall(config, now=5_000_000.0)

        assert mock_audit.call_count == 1
        call_kwargs = mock_audit.call_args
        objective, turn_text = call_kwargs.args
        assert objective == "Fix the flaky test"
        assert "STATUS:" not in turn_text
        assert "STUCK:" not in turn_text
        assert "editing coord/foo.py" in turn_text

    def test_no_running_turn_yet_skips_audit(self, tmp_path: Path, config: Config) -> None:
        _record("abc123")
        p = tmp_path / "empty.jsonl"
        p.write_text(
            json.dumps({"type": "system", "subtype": "init", "model": "x"}) + "\n"
        )
        with patch.object(
            notify_mod, "_agent_status", return_value=_active_status("abc123", p),
        ), patch("coord.liveness_auditor.run_audit") as mock_audit:
            results = notify_mod.detect_liveness_stall(config, now=6_000_000.0)
        assert results == []
        mock_audit.assert_not_called()


# ── never gates: the acceptance-bar invariant ───────────────────────────────


class TestNeverGatesBoardState:
    def test_raising_never_touches_assignment_status_or_gates(
        self, tmp_path: Path, config: Config, coord_db,
    ) -> None:
        """The auditor is a tripwire, not a gate (#2048's core acceptance
        bar): reaching 3 strikes and posting the comment must leave
        Assignment.status/review_state/test_state completely untouched."""
        _record("abc123")
        log_path = _log_with_turn(tmp_path, "spinning")
        t0 = 7_000_000.0

        with patch.object(
            notify_mod, "_agent_status",
            return_value=_active_status("abc123", log_path),
        ), patch(
            "coord.liveness_auditor.run_audit",
            return_value=AuditOutcome(verdict="blocked", raw_output="blocked"),
        ), patch.object(notify_mod, "github_ops") as mock_gh:
            for i in range(3):
                results = notify_mod.detect_liveness_stall(config, now=t0 + i * 61)
            for detection, record in results:
                notify_mod.post_liveness_stall(detection, record)

        assert mock_gh.post_issue_comment.called

        row = coord_db.execute(
            "SELECT status, review_state, test_state FROM assignments "
            "WHERE assignment_id='abc123'"
        ).fetchone()
        assert row["status"] == "running"
        assert row["review_state"] is None
        assert row["test_state"] is None

        # And the one-shot ledger entry is keyed under the composite id —
        # never under the bare assignment_id (which is reserved for
        # completion/failure/advisory).
        notified = state_mod.load_notified()
        assert "abc123" not in notified
        assert notified["abc123:liveness"]["event"] == EVENT_LIVENESS_STALL

    def test_double_post_liveness_stall_is_idempotent_via_ledger(
        self, tmp_path: Path, config: Config,
    ) -> None:
        _record("abc123")
        log_path = _log_with_turn(tmp_path, "spinning")
        t0 = 8_000_000.0
        with patch.object(
            notify_mod, "_agent_status",
            return_value=_active_status("abc123", log_path),
        ), patch(
            "coord.liveness_auditor.run_audit",
            return_value=AuditOutcome(verdict="blocked", raw_output="blocked"),
        ):
            for i in range(3):
                results = notify_mod.detect_liveness_stall(config, now=t0 + i * 61)

        with patch.object(notify_mod, "github_ops") as mock_gh:
            for detection, record in results:
                notify_mod.post_liveness_stall(detection, record)
            # A later pass must not re-detect (already-raised + notified).
            again = notify_mod.detect_liveness_stall(config, now=t0 + 1000)
            assert again == []
