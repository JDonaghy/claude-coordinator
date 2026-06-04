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
    _worker_subprocess_env,
    default_worker_command,
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


def test_worker_env_strips_agent_venv_from_path() -> None:
    # #402: when the agent runs inside a venv, the venv's bin must not be on
    # the worker's PATH (else a worker `pip install -e .` clobbers the agent).
    env = _worker_subprocess_env(
        {"PATH": "/venv/bin:/home/u/.local/bin:/usr/bin:/bin"},
        prefix="/venv",
        base_prefix="/usr",
    )
    parts = env["PATH"].split(os.pathsep)
    assert "/venv/bin" not in parts
    assert parts == ["/home/u/.local/bin", "/usr/bin", "/bin"]


def test_worker_env_clears_virtualenv_markers() -> None:
    env = _worker_subprocess_env(
        {"PATH": "/usr/bin", "VIRTUAL_ENV": "/venv", "PYTHONHOME": "/venv"},
        prefix="/venv",
        base_prefix="/usr",
    )
    assert "VIRTUAL_ENV" not in env
    assert "PYTHONHOME" not in env


def test_worker_env_preserves_path_when_not_in_venv() -> None:
    # System-Python agent (prefix == base_prefix): never strip /usr/bin & co.
    original = "/usr/local/bin:/usr/bin:/bin"
    env = _worker_subprocess_env(
        {"PATH": original},
        prefix="/usr",
        base_prefix="/usr",
    )
    assert env["PATH"] == original


def test_worker_env_keeps_unrelated_entries() -> None:
    env = _worker_subprocess_env(
        {"PATH": "/venv/bin:/opt/cargo/bin:/usr/bin", "EDITOR": "vim"},
        prefix="/venv",
        base_prefix="/usr",
    )
    assert env["PATH"] == "/opt/cargo/bin:/usr/bin"
    assert env["EDITOR"] == "vim"


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


# ── #315: resume_session_id / claude_session_id ────────────────────────────


def test_default_worker_command_resume_flag_absent_by_default() -> None:
    """No --resume flag when resume_session_id is not set."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
    )
    argv = default_worker_command(spec)
    assert "--resume" not in argv


def test_default_worker_command_resume_flag_present() -> None:
    """--resume <session_id> appended when resume_session_id is set."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
        resume_session_id="ses-abc123",
    )
    argv = default_worker_command(spec)
    assert "--resume" in argv
    idx = argv.index("--resume")
    assert argv[idx + 1] == "ses-abc123"


def test_reap_captures_claude_session_id(tmp_path: Path) -> None:
    """_reap populates AgentAssignment.claude_session_id from a system.init log line."""
    repo = _init_repo(tmp_path / "repo")
    session_id = "ses-xyz-test"

    # Worker emits a stream-json system.init line with the session_id then exits.
    init_line = json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "apiKeySource": "test",
    })
    worker_sh = f'echo \'{init_line}\'; exit 0'
    server = AgentServer(
        machine_name="test",
        repos=["api"],
        repo_paths={"api": str(repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", worker_sh],
    )

    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo),
        issue_number=42,
        issue_title="t",
        briefing="b",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == DONE
    assert final.claude_session_id == session_id

    # Also visible in the /status serialisation (to_dict)
    status = server.list_assignments()
    completed = status["completed"]
    assert any(c["claude_session_id"] == session_id for c in completed)
    server.shutdown()


def test_assignment_spec_accepts_resume_session_id() -> None:
    """AssignmentSpec round-trips resume_session_id through to_dict / from dict."""
    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="b",
        resume_session_id="ses-resume",
    )
    assert spec.resume_session_id == "ses-resume"


def test_claude_session_id_survives_persist_load(tmp_path: Path) -> None:
    """claude_session_id round-trips through the agent state JSON."""
    from dataclasses import asdict
    from coord.agent import AgentAssignment

    a = AgentAssignment(
        id="test-123",
        spec=AssignmentSpec(
            repo_name="api",
            repo_path="/tmp",
            issue_number=1,
            issue_title="t",
            briefing="b",
        ),
        claude_session_id="ses-persist",
    )
    d = a.to_dict()
    assert d["claude_session_id"] == "ses-persist"

    # Reconstruct from dict (mirrors _load_state logic).
    spec_data = d.pop("spec")
    spec = AssignmentSpec(**spec_data)
    a2 = AgentAssignment(spec=spec, **d)
    assert a2.claude_session_id == "ses-persist"


# ── Artifact stash (#305) ───────────────────────────────────────────────────


def _make_done_assignment(
    tmp_path: Path,
    *,
    repo_name: str = "api",
    branch: str = "issue-1-my-feature",
) -> tuple[AgentServer, AgentAssignment, Path]:
    """Create a server + a fake DONE assignment with a real worktree directory."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Fake worktree with some files
    wt_path = state_dir / "worktrees" / "asgn-abc123"
    wt_path.mkdir(parents=True, exist_ok=True)

    server = AgentServer(
        machine_name="test",
        repos=[repo_name],
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={repo_name: str(tmp_path / "repo")},
        artifact_paths={repo_name: ["target/debug/mybinary*", "*.d"]},
    )

    spec = AssignmentSpec(
        repo_name=repo_name,
        repo_path=str(tmp_path / "repo"),
        issue_number=1,
        issue_title="my feature",
        briefing="b",
        branch="main",
    )
    a = AgentAssignment(id="asgn-abc123", spec=spec, status=DONE, branch=branch)
    a.worktree_path = str(wt_path)

    return server, a, wt_path


def test_stash_artifacts_copies_matching_files(tmp_path: Path) -> None:
    """Matching files over 100B should be copied to the stash dir."""
    server, a, wt_path = _make_done_assignment(tmp_path)

    # Create a file that matches the glob and is large enough
    target_dir = wt_path / "target" / "debug"
    target_dir.mkdir(parents=True)
    bin_file = target_dir / "mybinary"
    bin_file.write_bytes(b"\x7fELF" + b"\x00" * 200)  # fake ELF, 204 bytes

    server._stash_artifacts(a)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    assert (stash_dir / "mybinary").exists(), "binary not copied to stash"
    assert (stash_dir / ".assignment_id").read_text() == "asgn-abc123"


def test_stash_artifacts_skips_small_files(tmp_path: Path) -> None:
    """Files under 100 bytes should be skipped (not real binaries)."""
    server, a, wt_path = _make_done_assignment(tmp_path)

    target_dir = wt_path / "target" / "debug"
    target_dir.mkdir(parents=True)
    tiny = target_dir / "mybinary"
    tiny.write_bytes(b"hi")  # only 2 bytes

    server._stash_artifacts(a)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    assert not (stash_dir / "mybinary").exists(), "tiny file should have been skipped"


def test_stash_artifacts_skips_dot_d_files(tmp_path: Path) -> None:
    """.d suffix files (compiler dependency files) should always be skipped."""
    server, a, wt_path = _make_done_assignment(tmp_path)

    target_dir = wt_path / "target" / "debug"
    target_dir.mkdir(parents=True)
    dep_file = target_dir / "mybinary.d"
    dep_file.write_bytes(b"dep " + b"x" * 200)  # large enough but .d suffix

    server._stash_artifacts(a)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    assert not (stash_dir / "mybinary.d").exists(), ".d file should have been skipped"


def test_stash_artifacts_noop_for_failed_assignment(tmp_path: Path) -> None:
    """FAILED assignments should not trigger any stash activity."""
    from coord.agent import FAILED

    server, a, wt_path = _make_done_assignment(tmp_path)
    a.status = FAILED

    target_dir = wt_path / "target" / "debug"
    target_dir.mkdir(parents=True)
    (target_dir / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    server._stash_artifacts(a)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    assert not stash_dir.exists(), "stash dir should not have been created for FAILED"


def test_stash_artifacts_noop_when_no_patterns(tmp_path: Path) -> None:
    """No-op when the repo has no artifact_paths configured."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    wt_path = state_dir / "worktrees" / "asgn-xyz"
    wt_path.mkdir(parents=True)

    server = AgentServer(
        machine_name="test",
        repos=["api"],
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"api": str(tmp_path / "repo")},
        artifact_paths={},  # empty
    )

    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(tmp_path / "repo"),
        issue_number=1,
        issue_title="t",
        briefing="b",
        branch="main",
    )
    a = AgentAssignment(id="asgn-xyz", spec=spec, status=DONE, branch="issue-1-t")
    a.worktree_path = str(wt_path)

    server._stash_artifacts(a)

    stash_base = server.state_dir / "artifacts"
    assert not stash_base.exists(), "no stash dir should be created when no patterns"


def test_stash_artifacts_prefers_spec_over_server_config(tmp_path: Path) -> None:
    """_stash_artifacts should prefer spec's artifact_paths over server
    self.artifact_paths (the local-dev config fallback).  #305."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    wt_path = state_dir / "worktrees" / "asgn-spec-override"
    wt_path.mkdir(parents=True, exist_ok=True)

    # Server has self.artifact_paths configured (local-dev case)
    server = AgentServer(
        machine_name="test",
        repos=["api"],
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"api": str(tmp_path / "repo")},
        artifact_paths={"api": ["old_pattern/*.txt"]},  # Server's fallback config
    )

    # But the spec has its own artifact_paths (from coordinator dispatch)
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(tmp_path / "repo"),
        issue_number=1,
        issue_title="t",
        briefing="b",
        branch="main",
        artifact_paths=["new_pattern/*.bin"],  # Spec overrides server config
    )
    a = AgentAssignment(id="asgn-spec-override", spec=spec, status=DONE, branch="issue-1-t")
    a.worktree_path = str(wt_path)

    # Create a file that matches the SPEC's pattern (not server's pattern)
    target_dir = wt_path / "new_pattern"
    target_dir.mkdir(parents=True)
    bin_file = target_dir / "test.bin"
    bin_file.write_bytes(b"\x00" * 200)

    server._stash_artifacts(a)

    # Should copy the file matched by spec's pattern
    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-t"
    assert (stash_dir / "test.bin").exists(), (
        "spec's artifact_paths should be used, not server's self.artifact_paths"
    )


def test_stash_artifacts_falls_back_to_server_config_when_spec_empty(
    tmp_path: Path,
) -> None:
    """_stash_artifacts should fall back to server self.artifact_paths when
    spec's artifact_paths is empty.  #305: local-dev backward compat."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    wt_path = state_dir / "worktrees" / "asgn-fallback"
    wt_path.mkdir(parents=True, exist_ok=True)

    # Server has self.artifact_paths configured
    server = AgentServer(
        machine_name="test",
        repos=["api"],
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"api": str(tmp_path / "repo")},
        artifact_paths={"api": ["fallback_pattern/*.txt"]},
    )

    # Spec has empty artifact_paths (old dispatch or local-dev)
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(tmp_path / "repo"),
        issue_number=2,
        issue_title="t2",
        briefing="b",
        branch="main",
        artifact_paths=[],  # Empty: should fall back to server config
    )
    a = AgentAssignment(id="asgn-fallback", spec=spec, status=DONE, branch="issue-2-t2")
    a.worktree_path = str(wt_path)

    # Create a file that matches the SERVER's fallback pattern
    fallback_dir = wt_path / "fallback_pattern"
    fallback_dir.mkdir(parents=True)
    txt_file = fallback_dir / "data.txt"
    txt_file.write_bytes(b"x" * 200)

    server._stash_artifacts(a)

    # Should copy the file matched by server's fallback pattern
    stash_dir = server.state_dir / "artifacts" / "api" / "issue-2-t2"
    assert (stash_dir / "data.txt").exists(), (
        "should fall back to server's self.artifact_paths when spec is empty"
    )


def test_gc_artifacts_removes_old_directories(tmp_path: Path) -> None:
    """_gc_artifacts should remove stash dirs older than ttl_days."""
    import os
    import time as _time

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    server = AgentServer(
        machine_name="test",
        repos=[],
        state_dir=state_dir,
        worker_command=lambda spec: [],
    )

    # Create an artifact stash dir and manually backdate its mtime
    stash = state_dir / "artifacts" / "api" / "old-branch"
    stash.mkdir(parents=True)
    (stash / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    # Age the directory to 4 days ago (past the 3-day default TTL)
    old_time = _time.time() - 4 * 86400
    os.utime(stash, (old_time, old_time))

    removed = server._gc_artifacts(ttl_days=3.0)
    assert removed == 1
    assert not stash.exists()


def test_gc_artifacts_keeps_recent_directories(tmp_path: Path) -> None:
    """_gc_artifacts must not remove stash dirs within the TTL window."""
    import os
    import time as _time

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    server = AgentServer(
        machine_name="test",
        repos=[],
        state_dir=state_dir,
        worker_command=lambda spec: [],
    )

    stash = state_dir / "artifacts" / "api" / "recent-branch"
    stash.mkdir(parents=True)
    (stash / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    # Age the directory to only 1 day ago (well within the 3-day TTL)
    recent_time = _time.time() - 1 * 86400
    os.utime(stash, (recent_time, recent_time))

    removed = server._gc_artifacts(ttl_days=3.0)
    assert removed == 0
    assert stash.exists()


def test_health_includes_artifact_bytes(tmp_path: Path) -> None:
    """health() should include an artifact_bytes key."""
    server = _server(tmp_path)
    h = server.health()
    assert "artifact_bytes" in h
    assert isinstance(h["artifact_bytes"], int)
    assert h["artifact_bytes"] == 0  # no stash yet


def test_artifact_manifest_returns_none_when_missing(tmp_path: Path) -> None:
    """artifact_manifest returns None when no stash dir exists."""
    server = _server(tmp_path)
    result = server.artifact_manifest("api", "issue-1-nonexistent")
    assert result is None


def test_artifact_manifest_returns_file_list(tmp_path: Path) -> None:
    """artifact_manifest returns the correct manifest dict when files exist."""
    server = _server(tmp_path)

    # Manually create a stash directory
    stash = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    stash.mkdir(parents=True)
    (stash / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)
    (stash / ".assignment_id").write_text("asgn-123")

    manifest = server.artifact_manifest("api", "issue-1-my-feature")
    assert manifest is not None
    assert manifest["built_by_assignment_id"] == "asgn-123"
    assert len(manifest["files"]) == 1
    assert manifest["files"][0]["name"] == "mybinary"
    assert manifest["total_bytes"] == manifest["files"][0]["size"]


def test_sanitize_branch_replaces_slashes(tmp_path: Path) -> None:
    """_sanitize_branch should replace slashes with dashes."""
    from coord.agent import _sanitize_branch

    assert _sanitize_branch("feature/my-thing") == "feature-my-thing"
    assert _sanitize_branch("issue-305-artifact-pull") == "issue-305-artifact-pull"
    assert _sanitize_branch("refs/heads/main") == "refs-heads-main"
