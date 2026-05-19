"""Tests for coord.notify — polling agents and posting GH comments."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.config import Config
from coord.models import Machine, Proposal, Repo
from coord import notify as notify_mod
from coord import state as state_mod


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )


@pytest.fixture
def coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ~/.coord state files to tmp_path."""
    monkeypatch.setattr(state_mod, "COORD_DIR", tmp_path)
    monkeypatch.setattr(state_mod, "PROPOSALS_FILE", tmp_path / "pending_proposals.json")
    monkeypatch.setattr(state_mod, "DISPATCHED_FILE", tmp_path / "dispatched.json")
    monkeypatch.setattr(state_mod, "NOTIFIED_FILE", tmp_path / "notified.json")
    return tmp_path


def _record(coord_dir: Path, assignment_id: str) -> None:
    proposal = Proposal(
        id=1, machine_name="laptop", repo_name="api",
        issue_number=42, issue_title="t", rationale="r",
        files_likely=["src/a.py"], briefing="b",
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github="acme/api",
    )


def _agent_completed(assignment_id: str, status: str, **overrides) -> dict:
    base = {
        "id": assignment_id,
        "status": status,
        "exit_code": 0 if status == "done" else 1,
        "started_at": 1000.0,
        "finished_at": 1004.0,
        "log_path": f"/var/log/{assignment_id}.log",
        "error": None,
    }
    base.update(overrides)
    return base


class TestDetectTransitions:
    def test_no_dispatched_returns_empty(self, coord_dir: Path, config: Config) -> None:
        assert notify_mod.detect_transitions(config) == []

    def test_done_transition_detected(self, coord_dir: Path, config: Config) -> None:
        _record(coord_dir, "abc123")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("abc123", "done")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            transitions = notify_mod.detect_transitions(config)
        assert len(transitions) == 1
        t, _, _ = transitions[0]
        assert t.event == "completion"
        assert t.assignment_id == "abc123"

    def test_failed_transition_detected(self, coord_dir: Path, config: Config) -> None:
        _record(coord_dir, "xyz789")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("xyz789", "failed", error="boom")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            transitions = notify_mod.detect_transitions(config)
        assert transitions[0][0].event == "failure"

    def test_already_notified_skipped(self, coord_dir: Path, config: Config) -> None:
        _record(coord_dir, "abc")
        state_mod.mark_notified("abc", "completion")
        agent_status = {"active": [], "completed": [_agent_completed("abc", "done")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            assert notify_mod.detect_transitions(config) == []

    def test_offline_machine_yields_no_transitions(self, coord_dir: Path, config: Config) -> None:
        _record(coord_dir, "abc")
        with patch.object(notify_mod, "_agent_status", return_value=None):
            assert notify_mod.detect_transitions(config) == []


class TestRun:
    def test_posts_completion_and_marks_notified(self, coord_dir: Path, config: Config) -> None:
        _record(coord_dir, "abc")
        agent_status = {"active": [], "completed": [_agent_completed("abc", "done")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck = notify_mod.run(config)
        assert len(posted) == 1
        mock_post.assert_called_once()
        # Comment body includes the completion marker
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Complete" in body
        # Notified ledger persisted
        assert "abc" in state_mod.load_notified()

    def test_idempotent_second_run_posts_nothing(self, coord_dir: Path, config: Config) -> None:
        _record(coord_dir, "abc")
        agent_status = {"active": [], "completed": [_agent_completed("abc", "done")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
            posted_again, _stuck = notify_mod.run(config)
        # Comment posted exactly once across both runs
        assert mock_post.call_count == 1
        assert posted_again == []

    def test_failure_posts_failure_comment(self, coord_dir: Path, config: Config) -> None:
        _record(coord_dir, "xyz")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("xyz", "failed", error="bad config")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Failed" in body
        assert "bad config" in body


class TestDispatchedLedger:
    def test_record_and_load_roundtrip(self, coord_dir: Path) -> None:
        _record(coord_dir, "abc")
        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["assignment_id"] == "abc"
        assert records[0]["repo_github"] == "acme/api"
        assert records[0]["files_likely"] == ["src/a.py"]
