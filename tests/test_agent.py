"""Tests for the agent server core (no HTTP)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coord.agent import (
    CANCELLED,
    DONE,
    FAILED,
    RUNNING,
    AgentServer,
    AssignmentSpec,
)


def _spec(repo_path: Path, **overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path=str(repo_path),
        issue_number=1,
        issue_title="t",
        briefing="b",
        files_allowed=[],
        files_forbidden=[],
        branch="main",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


def _server(tmp_path: Path, *, argv: list[str] | None = None, **kwargs) -> AgentServer:
    if argv is None:
        argv = ["/bin/sh", "-c", "echo worker-output"]
    return AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv,
        **kwargs,
    )


def test_health_reports_machine(tmp_path: Path) -> None:
    server = _server(tmp_path)
    h = server.health()
    assert h["machine"] == "test"
    assert h["repos"] == ["api"]
    assert h["active"] == 0
    assert h["completed"] == 0


def test_assign_success(tmp_path: Path) -> None:
    server = _server(tmp_path)
    a = server.assign(_spec(tmp_path))
    final = server.wait_for(a.id)
    assert final.status == DONE
    assert final.exit_code == 0
    log = Path(final.log_path).read_text()
    assert "worker-output" in log
    server.shutdown()


def test_assign_failure_marks_failed(tmp_path: Path) -> None:
    server = _server(tmp_path, argv=["/bin/sh", "-c", "echo nope; exit 7"])
    a = server.assign(_spec(tmp_path))
    final = server.wait_for(a.id)
    assert final.status == FAILED
    assert final.exit_code == 7
    server.shutdown()


def test_assign_unknown_binary_marks_failed(tmp_path: Path) -> None:
    server = _server(tmp_path, argv=["/no/such/binary"])
    a = server.assign(_spec(tmp_path))
    final = server.wait_for(a.id)
    assert final.status == FAILED
    assert final.error is not None


def test_cancel_running_assignment(tmp_path: Path) -> None:
    server = _server(tmp_path, argv=["/bin/sh", "-c", "sleep 30"])
    a = server.assign(_spec(tmp_path))
    # Wait until it's actually running so cancel has something to terminate.
    for _ in range(50):
        if server.get(a.id).status == RUNNING:
            break
        time.sleep(0.02)
    server.cancel(a.id)
    final = server.get(a.id)
    assert final.status == CANCELLED
    server.shutdown()


def test_cancel_unknown_id_raises(tmp_path: Path) -> None:
    server = _server(tmp_path)
    with pytest.raises(KeyError):
        server.cancel("nope")


def test_rejects_unhandled_repo(tmp_path: Path) -> None:
    server = _server(tmp_path)
    with pytest.raises(ValueError, match="does not handle repo"):
        server.assign(_spec(tmp_path, repo_name="other"))


def test_rejects_missing_repo_path(tmp_path: Path) -> None:
    server = _server(tmp_path)
    with pytest.raises(ValueError, match="repo path does not exist"):
        server.assign(_spec(tmp_path / "missing"))


def test_state_persists_to_disk(tmp_path: Path) -> None:
    server = _server(tmp_path)
    a = server.assign(_spec(tmp_path))
    server.wait_for(a.id)
    state = json.loads((tmp_path / "state" / "agent_state.json").read_text())
    ids = [entry["id"] for entry in state["assignments"]]
    assert a.id in ids
    server.shutdown()


def test_orphaned_running_assignments_marked_failed_on_load(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "agent_state.json").write_text(
        json.dumps(
            {
                "machine": "test",
                "capabilities": [],
                "repos": ["api"],
                "assignments": [
                    {
                        "id": "abc123",
                        "status": "running",
                        "pid": 99999,
                        "started_at": 1.0,
                        "finished_at": None,
                        "exit_code": None,
                        "log_path": str(tmp_path / "abc123.log"),
                        "error": None,
                        "spec": {
                            "repo_name": "api",
                            "repo_path": str(tmp_path),
                            "issue_number": 1,
                            "issue_title": "t",
                            "briefing": "b",
                            "files_allowed": [],
                            "files_forbidden": [],
                            "branch": "main",
                        },
                    }
                ],
            }
        )
    )
    server = AgentServer(
        machine_name="test", repos=["api"], state_dir=state_dir
    )
    recovered = server.get("abc123")
    assert recovered is not None
    assert recovered.status == FAILED
    assert "restarted" in recovered.error
