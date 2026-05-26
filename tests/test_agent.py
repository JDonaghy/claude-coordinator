"""Tests for the agent server core (no HTTP)."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

import pytest

from coord.agent import (
    CANCELLED,
    DONE,
    FAILED,
    RUNNING,
    AgentServer,
    AssignmentSpec,
)


def _init_repo(path: Path) -> Path:
    """Create a minimal git repo with one commit so worktrees can be created."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


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


def _server(tmp_path: Path, *, argv: list[str] | None = None, repo_path: Path | None = None, **kwargs) -> AgentServer:
    if argv is None:
        argv = ["/bin/sh", "-c", "echo worker-output"]
    # Ensure we have a git repo for worktree support
    rp = repo_path or _init_repo(tmp_path / "repo")
    return AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv,
        repo_paths={"api": str(rp)},
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
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)
    assert final.status == DONE
    assert final.exit_code == 0
    assert final.worktree_path is not None
    log = Path(final.log_path).read_text()
    assert "worker-output" in log
    server.shutdown()


def test_assign_failure_marks_failed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, argv=["/bin/sh", "-c", "echo nope; exit 7"], repo_path=repo)
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)
    assert final.status == FAILED
    assert final.exit_code == 7
    server.shutdown()


def test_assign_unknown_binary_marks_failed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, argv=["/no/such/binary"], repo_path=repo)
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)
    assert final.status == FAILED
    assert final.error is not None


def test_initial_briefing_is_written_to_worker_stdin(tmp_path: Path) -> None:
    """The briefing must reach the worker via stdin as a stream-json line."""
    repo = _init_repo(tmp_path / "repo")
    # Read exactly one line from stdin into the log, then exit.
    server = _server(
        tmp_path,
        argv=["/bin/sh", "-c", "read line; echo $line"],
        repo_path=repo,
    )
    a = server.assign(_spec(repo, briefing="hello world"))
    final = server.wait_for(a.id)
    log = Path(final.log_path).read_text()
    assert '"type": "user"' in log, "stream-json envelope missing from stdin echo"
    assert "hello world" in log, "briefing text missing from stdin echo"


def test_inject_message_writes_to_worker_stdin(tmp_path: Path) -> None:
    """inject_message writes a stream-json user line to the worker's stdin."""
    import time as _time
    repo = _init_repo(tmp_path / "repo")
    # Worker reads two lines (initial briefing + injection) then exits.
    server = _server(
        tmp_path,
        argv=["/bin/sh", "-c", "read a; echo got1=$a; read b; echo got2=$b"],
        repo_path=repo,
    )
    a = server.assign(_spec(repo, briefing="first"))
    # Give Popen a moment to wire stdin and consume the first line.
    _time.sleep(0.3)
    server.inject_message(a.id, "second message")
    final = server.wait_for(a.id, timeout=5.0)
    log = Path(final.log_path).read_text()
    assert "got1=" in log and "first" in log
    assert "got2=" in log and "second message" in log
    assert "# inject: second message" in log, "inject marker missing from log"


def test_inject_message_unknown_id_raises(tmp_path: Path) -> None:
    server = _server(tmp_path)
    with pytest.raises(KeyError):
        server.inject_message("does-not-exist", "hi")


def test_inject_message_on_finished_assignment_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    a = server.assign(_spec(repo))
    server.wait_for(a.id)  # let it finish
    with pytest.raises((RuntimeError, BrokenPipeError)):
        server.inject_message(a.id, "too late")


def test_cancel_running_assignment(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, argv=["/bin/sh", "-c", "sleep 30"], repo_path=repo)
    a = server.assign(_spec(repo))
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
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    with pytest.raises(ValueError, match="does not handle repo"):
        server.assign(_spec(repo, repo_name="other"))


def test_rejects_missing_repo_path(tmp_path: Path) -> None:
    server = _server(tmp_path)
    with pytest.raises(ValueError, match="repo path does not exist"):
        server.assign(_spec(tmp_path / "missing"))


def test_state_persists_to_disk(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    a = server.assign(_spec(repo))
    server.wait_for(a.id)
    state = json.loads((tmp_path / "state" / "agent_state.json").read_text())
    ids = [entry["id"] for entry in state["assignments"]]
    assert a.id in ids
    # worktree_path should be persisted
    entry = next(e for e in state["assignments"] if e["id"] == a.id)
    assert entry["worktree_path"] is not None
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
                        "branch": None,
                        "worktree_path": None,
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


# ── Tests for health().worktree_bytes and clean_worktrees() ──────────────────

def test_health_includes_worktree_bytes(tmp_path: Path) -> None:
    """health() always includes worktree_bytes (0 when no worktrees exist)."""
    server = _server(tmp_path)
    h = server.health()
    assert "worktree_bytes" in h
    assert h["worktree_bytes"] == 0


def test_health_worktree_bytes_reflects_disk_usage(tmp_path: Path) -> None:
    """health() worktree_bytes increases when files exist under worktrees/."""
    server = _server(tmp_path)
    wt_dir = server.state_dir / "worktrees" / "fake-id"
    wt_dir.mkdir(parents=True)
    (wt_dir / "big.bin").write_bytes(b"X" * 4096)

    h = server.health()
    assert h["worktree_bytes"] >= 4096


def test_clean_worktrees_empty_base(tmp_path: Path) -> None:
    """clean_worktrees returns zero counts when no worktrees directory exists."""
    server = _server(tmp_path)
    result = server.clean_worktrees()
    assert result == {"cleaned": 0, "kept": 0, "bytes_freed": 0}


def test_clean_worktrees_removes_orphan(tmp_path: Path) -> None:
    """Orphaned worktrees (no matching assignment) are removed."""
    server = _server(tmp_path)
    orphan = server.state_dir / "worktrees" / "no-such-assignment"
    orphan.mkdir(parents=True)
    (orphan / "file.txt").write_text("data")

    result = server.clean_worktrees()
    assert result["cleaned"] == 1
    assert result["bytes_freed"] > 0
    assert not orphan.exists()


def test_clean_worktrees_keeps_running(tmp_path: Path) -> None:
    """Worktrees for running assignments are never touched."""
    repo = _init_repo(tmp_path / "repo")
    # Use a worker that sleeps long enough for us to inspect state.
    server = _server(tmp_path, argv=["/bin/sh", "-c", "sleep 10"], repo_path=repo)
    a = server.assign(_spec(repo))

    # Give the worker a moment to start and create its worktree.
    time.sleep(0.5)

    result = server.clean_worktrees()
    assert result["kept"] >= 1
    assert result["cleaned"] == 0
    server.shutdown()


def test_clean_worktrees_removes_stale_done(tmp_path: Path) -> None:
    """Worktrees whose assignment is done and old (> recent_secs) are removed.

    Simulates a crash-recovery scenario: the agent recorded the assignment
    as done but the worktree directory was not cleaned up before the crash.
    """
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)

    # The agent's normal cleanup already removed the worktree.  Re-create it
    # to simulate an unclean shutdown where cleanup didn't run.
    stale_wt = server.state_dir / "worktrees" / final.id
    stale_wt.mkdir(parents=True, exist_ok=True)
    (stale_wt / "leftover.txt").write_text("stale data")

    # recent_secs=0 means even a just-finished assignment is eligible.
    result = server.clean_worktrees(recent_secs=0)
    assert result["cleaned"] >= 1
    assert not stale_wt.exists()
    server.shutdown()
