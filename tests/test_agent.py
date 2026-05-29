"""Tests for the agent server core (no HTTP)."""

from __future__ import annotations

import json
import os
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
    # bash_wrap_spawn=False so the unknown binary surfaces as a FileNotFoundError
    # at Popen time (the spawn-failed path sets assignment.error). With the
    # bash-wrap on, bash spawns fine and `exec` fails inside the child, which is
    # covered separately as a non-zero exit → FAILED.
    server = _server(
        tmp_path, argv=["/no/such/binary"], repo_path=repo, bash_wrap_spawn=False
    )
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)
    assert final.status == FAILED
    assert final.error is not None


def test_assign_unknown_binary_bash_wrapped_marks_failed(tmp_path: Path) -> None:
    """With the bash-wrap on, an unknown binary fails via bash exec's non-zero
    exit (#299) — the assignment still ends up FAILED."""
    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path, argv=["/no/such/binary"], repo_path=repo, bash_wrap_spawn=True
    )
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)
    assert final.status == FAILED
    assert final.exit_code not in (0, None)


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


def test_maybe_bash_wrap_helper() -> None:
    """The pure wrap helper produces bash -c 'exec ...' when enabled (#299)."""
    from coord.agent import _maybe_bash_wrap

    argv = ["claude", "-p", "--allowedTools", "Read,Bash"]
    assert _maybe_bash_wrap(argv, enabled=False) == argv
    wrapped = _maybe_bash_wrap(argv, enabled=True)
    assert wrapped == ["bash", "-c", "exec claude -p --allowedTools Read,Bash"]


def test_spawn_bash_wrap_enabled_routes_through_bash(tmp_path: Path) -> None:
    """With bash_wrap_spawn=True, _spawn launches via bash -c 'exec ...'."""
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path,
        argv=["/bin/sh", "-c", "echo worker-output"],
        repo_path=repo,
        bash_wrap_spawn=True,
    )
    captured: list[list[str]] = []
    real_popen = agent_mod.subprocess.Popen

    def recording_popen(spawn_argv, *args, **kwargs):
        # Only record the worker spawn (started in its own session); the
        # assign flow also runs git via Popen-backed subprocess.run.
        if kwargs.get("start_new_session"):
            captured.append(spawn_argv)
        return real_popen(spawn_argv, *args, **kwargs)

    agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
    try:
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id)
    finally:
        agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]
    assert final.status == DONE
    assert captured, "Popen was not called"
    assert captured[0][:2] == ["bash", "-c"]
    assert captured[0][2] == "exec /bin/sh -c 'echo worker-output'"
    # The wrapped command still produced the worker's output.
    assert "worker-output" in Path(final.log_path).read_text()
    server.shutdown()


def test_spawn_bash_wrap_disabled_uses_bare_argv(tmp_path: Path) -> None:
    """With bash_wrap_spawn=False, _spawn launches the bare argv."""
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path,
        argv=["/bin/sh", "-c", "echo worker-output"],
        repo_path=repo,
        bash_wrap_spawn=False,
    )
    captured: list[list[str]] = []
    real_popen = agent_mod.subprocess.Popen

    def recording_popen(spawn_argv, *args, **kwargs):
        # Only record the worker spawn (started in its own session); the
        # assign flow also runs git via Popen-backed subprocess.run.
        if kwargs.get("start_new_session"):
            captured.append(spawn_argv)
        return real_popen(spawn_argv, *args, **kwargs)

    agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
    try:
        a = server.assign(_spec(repo))
        final = server.wait_for(a.id)
    finally:
        agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]
    assert final.status == DONE
    assert captured and captured[0] == ["/bin/sh", "-c", "echo worker-output"]
    server.shutdown()


def test_agent_server_defaults_bash_wrap_and_timeout(tmp_path: Path) -> None:
    """AgentServer defaults: bash_wrap_spawn on, first_output_timeout 600 (#299)."""
    server = _server(tmp_path)
    assert server.bash_wrap_spawn is True
    assert server.first_output_timeout == 600.0
    server.shutdown()


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
    """Orphaned worktrees (no matching assignment) are removed.

    Uses ``recent_secs=0`` to bypass the race-window mtime guard that
    normally protects just-created directories from being deleted out
    from under a still-spawning worker.
    """
    server = _server(tmp_path)
    orphan = server.state_dir / "worktrees" / "no-such-assignment"
    orphan.mkdir(parents=True)
    (orphan / "file.txt").write_text("data")

    result = server.clean_worktrees(recent_secs=0)
    assert result["cleaned"] == 1
    assert result["bytes_freed"] > 0
    assert not orphan.exists()


def test_clean_worktrees_keeps_fresh_orphan(tmp_path: Path) -> None:
    """Race protection: an orphan whose mtime is within recent_secs is kept.

    Closes the window where ``_setup_worktree`` has created the
    directory but ``assign()`` hasn't yet inserted the assignment into
    ``self._assignments`` — without this guard a concurrent
    ``clean_worktrees`` would ``git worktree remove`` the freshly-made
    tree out from under the spawning worker.
    """
    server = _server(tmp_path)
    # mtime is "now" — within the default 5-minute recent_secs window.
    fresh = server.state_dir / "worktrees" / "racing-id"
    fresh.mkdir(parents=True)
    (fresh / "file.txt").write_text("partial")

    result = server.clean_worktrees(recent_secs=300)
    assert result["cleaned"] == 0
    assert result["kept"] == 1
    assert fresh.exists()


def test_clean_worktrees_removes_aged_orphan(tmp_path: Path) -> None:
    """An orphan with old mtime is removed under the default recent_secs."""
    server = _server(tmp_path)
    aged = server.state_dir / "worktrees" / "old-orphan"
    aged.mkdir(parents=True)
    (aged / "leftover.txt").write_text("stale")
    # Back-date the directory mtime to simulate an orphan from a
    # previous agent session.
    old = time.time() - 3600  # 1 hour ago
    os.utime(aged, (old, old))

    result = server.clean_worktrees(recent_secs=300)
    assert result["cleaned"] == 1
    assert not aged.exists()


def test_clean_worktrees_keeps_recently_finished(tmp_path: Path) -> None:
    """Recently-finished assignments are kept (worker may still be tearing down)."""
    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=repo)
    a = server.assign(_spec(repo))
    final = server.wait_for(a.id)

    # Re-create the worktree dir so we have something to potentially clean.
    stale_wt = server.state_dir / "worktrees" / final.id
    stale_wt.mkdir(parents=True, exist_ok=True)
    (stale_wt / "leftover.txt").write_text("stale data")
    # The assignment record's finished_at is "now-ish"; default
    # recent_secs=300 should keep the worktree.
    result = server.clean_worktrees(recent_secs=300)
    assert result["kept"] >= 1
    assert result["cleaned"] == 0
    assert stale_wt.exists()
    server.shutdown()


def test_health_worktree_bytes_is_cached(tmp_path: Path) -> None:
    """health()'s worktree_bytes is cached so /health doesn't rglob every call.

    Files added after the first call should not be visible until the
    cache TTL expires (or is invalidated).
    """
    server = _server(tmp_path)
    wt_dir = server.state_dir / "worktrees" / "cache-test"
    wt_dir.mkdir(parents=True)
    (wt_dir / "a.bin").write_bytes(b"X" * 1024)

    first = server.health()["worktree_bytes"]
    assert first >= 1024

    # Add a much bigger file after the cache has been populated.
    (wt_dir / "b.bin").write_bytes(b"Y" * 8192)
    second = server.health()["worktree_bytes"]
    # Cache TTL is ~30 s by default — the new file should not be visible yet.
    assert second == first

    # Force-expire the cache and the new size becomes visible.
    server._worktree_bytes_cache = None
    third = server.health()["worktree_bytes"]
    assert third >= first + 8192


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
