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
    ADVISORY,
    CANCELLED,
    DONE,
    FAILED,
    RUNNING,
    AgentAssignment,
    AgentServer,
    AssignmentSpec,
    _COMPLETED_HISTORY_CAP,
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
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
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
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
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
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
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
    # Worker makes no commits → advisory (#448)
    assert final.status == ADVISORY
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


def test_stash_artifacts_skips_build_intermediates_and_dedupes_hash_copies(
    tmp_path: Path,
) -> None:
    """#436: object files, rlibs, rmeta, and hash-stamped duplicate binaries
    must be excluded from the stash.  Only the canonical binary survives."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    wt_path = state_dir / "worktrees" / "asgn-436"
    wt_path.mkdir(parents=True, exist_ok=True)

    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200  # fake ELF, 204 bytes

    # Canonical binary — should be kept
    (examples_dir / "tui_app").write_bytes(payload)
    # Hash-stamped duplicate — should be skipped (canonical sibling present)
    (examples_dir / "tui_app-abcdef0123456789").write_bytes(payload)
    # Incremental-codegen object — should be skipped (.o suffix)
    (examples_dir / "tui_app-abc123.rcgu.o").write_bytes(payload)
    # Compiler dependency file — should be skipped (.d suffix)
    (examples_dir / "tui_app.d").write_bytes(payload)
    # Tiny file — should be skipped (< 100 bytes)
    (examples_dir / "tui_app-tiny").write_bytes(b"hi")

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )
    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=436,
        issue_title="artifact stash junk",
        briefing="b",
        branch="main",
    )
    a = AgentAssignment(id="asgn-436", spec=spec, status=DONE, branch="issue-436-fix")
    a.worktree_path = str(wt_path)

    server._stash_artifacts(a)

    stash_dir = state_dir / "artifacts" / "quadraui" / "issue-436-fix"
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}

    assert stashed == {"tui_app"}, (
        f"expected only the canonical binary; got {stashed!r}"
    )


def test_stash_artifacts_keeps_lone_hash_suffixed_binary(tmp_path: Path) -> None:
    """#436: when ONLY the hash-stamped form exists (no canonical sibling),
    it must be kept — never silently drop the only copy of a binary."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    wt_path = state_dir / "worktrees" / "asgn-436b"
    wt_path.mkdir(parents=True, exist_ok=True)

    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200

    # Only the hash-stamped form exists — no canonical sibling
    (examples_dir / "tui_app-abcdef0123456789").write_bytes(payload)

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )
    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=436,
        issue_title="lone hash binary",
        briefing="b",
        branch="main",
    )
    a = AgentAssignment(id="asgn-436b", spec=spec, status=DONE, branch="issue-436b-fix")
    a.worktree_path = str(wt_path)

    server._stash_artifacts(a)

    stash_dir = state_dir / "artifacts" / "quadraui" / "issue-436b-fix"
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}

    assert "tui_app-abcdef0123456789" in stashed, (
        "lone hash-stamped binary must be kept when no canonical sibling exists"
    )


# ── #982: narrow_artifact_paths unit tests ───────────────────────────────────

def test_narrow_artifact_paths_replaces_glob_with_matching_name() -> None:
    """Smoke test names a specific example → glob is replaced with that path."""
    from coord.agent import narrow_artifact_paths

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["tui_submenu — run it — menu should appear"],
    )
    assert result == ["target/debug/examples/tui_submenu"]


def test_narrow_artifact_paths_multiple_examples_in_one_bullet() -> None:
    """Two example names in the same bullet → both specific paths in result."""
    from coord.agent import narrow_artifact_paths

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*", "target/debug/examples/gtk_*"],
        ["tui_submenu and gtk_scrollbar — run them — should render"],
    )
    assert sorted(result) == sorted([
        "target/debug/examples/tui_submenu",
        "target/debug/examples/gtk_scrollbar",
    ])


def test_narrow_artifact_paths_fallback_when_no_smoke_tests_none() -> None:
    """smoke_tests=None → original artifact_paths returned unchanged."""
    from coord.agent import narrow_artifact_paths

    paths = ["target/debug/examples/tui_*", "target/debug/coord-tui"]
    result = narrow_artifact_paths(paths, None)
    assert result == paths


def test_narrow_artifact_paths_fallback_when_smoke_tests_empty_list() -> None:
    """smoke_tests=[] (internal change) → original list returned unchanged."""
    from coord.agent import narrow_artifact_paths

    paths = ["target/debug/examples/tui_*"]
    result = narrow_artifact_paths(paths, [])
    assert result == paths


def test_narrow_artifact_paths_fallback_when_no_name_matches_glob() -> None:
    """No candidate name matches the glob → return original list unchanged."""
    from coord.agent import narrow_artifact_paths

    # Words like "run", "the", "tests", "check" don't match "tui_*"
    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["run the tests and check output carefully"],
    )
    assert result == ["target/debug/examples/tui_*"]


def test_narrow_artifact_paths_preserves_literal_paths() -> None:
    """Literal (non-glob) paths are always preserved unchanged."""
    from coord.agent import narrow_artifact_paths

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*", "target/debug/coord-tui"],
        ["tui_submenu — run it — check submenu"],
    )
    assert "target/debug/examples/tui_submenu" in result
    assert "target/debug/coord-tui" in result
    assert "target/debug/examples/tui_*" not in result


def test_narrow_artifact_paths_unmatched_glob_kept_unchanged() -> None:
    """A glob with no matching candidates is left in the list unchanged."""
    from coord.agent import narrow_artifact_paths

    # Only tui_* has a match; gtk_* has none — gtk glob stays
    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*", "target/debug/examples/gtk_*"],
        ["tui_submenu — run it — check menu"],
    )
    assert "target/debug/examples/tui_submenu" in result
    # glob narrowed
    assert "target/debug/examples/tui_*" not in result
    # unmatched glob kept (no gtk name in smoke tests)
    assert "target/debug/examples/gtk_*" in result


def test_narrow_artifact_paths_no_glob_in_list_returns_unchanged() -> None:
    """When artifact_paths contains no globs, return list unchanged."""
    from coord.agent import narrow_artifact_paths

    paths = ["target/debug/coord-tui", "target/debug/mybinary"]
    result = narrow_artifact_paths(paths, ["tui_submenu — run — check"])
    assert result == paths


def test_narrow_artifact_paths_empty_artifact_paths_returns_empty() -> None:
    """Empty artifact_paths → empty list returned."""
    from coord.agent import narrow_artifact_paths

    result = narrow_artifact_paths([], ["tui_submenu — run — check"])
    assert result == []


# ── #1248: narrow_artifact_paths disk-verification tests ─────────────────────


def test_narrow_artifact_paths_worktree_falls_back_when_absent(
    tmp_path: Path,
) -> None:
    """When named binary is absent on disk, the original broad glob is kept.

    #1248: text-matching alone is insufficient — if tui_submenu appears in
    SMOKE_TESTS but hasn't been built yet, pinning the stash to that path
    produces a 0-copy stash silently.  Passing worktree= forces a disk check.
    """
    from coord.agent import narrow_artifact_paths

    # worktree exists but tui_submenu was never built
    worktree = tmp_path / "worktree"
    (worktree / "target" / "debug" / "examples").mkdir(parents=True)

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["tui_submenu — run it — menu should appear"],
        worktree=worktree,
    )
    # name matches text but missing on disk → keep broad glob
    assert result == ["target/debug/examples/tui_*"]


def test_narrow_artifact_paths_worktree_narrows_when_present(
    tmp_path: Path,
) -> None:
    """When named binary exists on disk, the glob IS narrowed to that path.

    #1248: the disk check must not block narrowing when the binary is present.
    """
    from coord.agent import narrow_artifact_paths

    worktree = tmp_path / "worktree"
    examples = worktree / "target" / "debug" / "examples"
    examples.mkdir(parents=True)
    # build the binary so it's present on disk
    (examples / "tui_submenu").write_bytes(b"\x7fELF" + b"\x00" * 200)

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["tui_submenu — run it — menu should appear"],
        worktree=worktree,
    )
    assert result == ["target/debug/examples/tui_submenu"]
    assert "target/debug/examples/tui_*" not in result


def test_narrow_artifact_paths_worktree_partial_on_disk(
    tmp_path: Path,
) -> None:
    """Only on-disk names are used when the smoke tests name multiple binaries.

    #1248: if SMOKE_TESTS names tui_submenu and tui_colors but only tui_submenu
    was actually built, the narrowed result contains only tui_submenu (not the
    absent tui_colors and not the broad glob).
    """
    from coord.agent import narrow_artifact_paths

    worktree = tmp_path / "worktree"
    examples = worktree / "target" / "debug" / "examples"
    examples.mkdir(parents=True)
    (examples / "tui_submenu").write_bytes(b"\x7fELF" + b"\x00" * 200)
    # tui_colors intentionally NOT created on disk

    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["tui_submenu and tui_colors — run them — should render"],
        worktree=worktree,
    )
    # only the on-disk binary is in the result
    assert result == ["target/debug/examples/tui_submenu"]
    assert "target/debug/examples/tui_colors" not in result
    assert "target/debug/examples/tui_*" not in result


def test_narrow_artifact_paths_no_worktree_preserves_text_only_behaviour() -> None:
    """worktree=None (default) keeps the original text-only matching.

    #1248: backward compat — interactive/remote callers that pass no worktree
    must not be broken by the new parameter.
    """
    from coord.agent import narrow_artifact_paths

    # No worktree → text match wins even though no real files exist
    result = narrow_artifact_paths(
        ["target/debug/examples/tui_*"],
        ["tui_submenu — run it — check menu"],
    )
    assert result == ["target/debug/examples/tui_submenu"]


# ── #982: stash integration tests ────────────────────────────────────────────

def test_stash_artifacts_scoped_spec_stashes_only_named_binary(
    tmp_path: Path,
) -> None:
    """spec.artifact_paths with a specific binary name stashes only that
    binary, not all files matching the repo-wide glob.  #982."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    wt_path = state_dir / "worktrees" / "asgn-982-scoped"
    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200
    for name in ["tui_submenu", "tui_scrollbar", "tui_colors"]:
        (examples_dir / name).write_bytes(payload)

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        # Server-wide config: the broad glob
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )

    # Spec carries a narrowed list (as if dispatch used narrow_artifact_paths)
    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=982,
        issue_title="submenu scoped",
        briefing="b",
        branch="main",
        # Override: only stash tui_submenu
        artifact_paths=["target/debug/examples/tui_submenu"],
    )
    a = AgentAssignment(
        id="asgn-982-scoped",
        spec=spec,
        status=DONE,
        branch="issue-982-submenu-scoped",
    )
    a.worktree_path = str(wt_path)

    server._stash_artifacts(a)

    stash_dir = (
        state_dir / "artifacts" / "quadraui" / "issue-982-submenu-scoped"
    )
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}
    assert stashed == {"tui_submenu"}, (
        f"scoped spec should stash only tui_submenu; got {stashed!r}"
    )


def test_stash_artifacts_no_spec_override_uses_repo_wide_glob(
    tmp_path: Path,
) -> None:
    """With no spec.artifact_paths override, the server's repo-wide glob
    stashes all matching files.  #982: fallback path preserved."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    wt_path = state_dir / "worktrees" / "asgn-982-fallback"
    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200
    for name in ["tui_submenu", "tui_scrollbar"]:
        (examples_dir / name).write_bytes(payload)

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )

    # Spec has no artifact_paths override → falls back to server-wide glob
    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=983,
        issue_title="fallback glob",
        briefing="b",
        branch="main",
        artifact_paths=[],  # empty → use server config
    )
    a = AgentAssignment(
        id="asgn-982-fallback",
        spec=spec,
        status=DONE,
        branch="issue-983-fallback-glob",
    )
    a.worktree_path = str(wt_path)

    server._stash_artifacts(a)

    stash_dir = (
        state_dir / "artifacts" / "quadraui" / "issue-983-fallback-glob"
    )
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}
    assert stashed == {"tui_submenu", "tui_scrollbar"}, (
        f"fallback should stash all tui_* files; got {stashed!r}"
    )


def test_stash_artifacts_narrows_using_worker_own_smoke_tests_log(
    tmp_path: Path,
) -> None:
    """#982: _stash_artifacts must narrow the repo-wide glob using the
    worker's OWN just-completed SMOKE_TESTS block, parsed from
    assignment.log_path — this is the headless Work dispatch path
    (_dispatch_headless sends the full glob unmodified; narrowing has to
    happen here, since smoke tests don't exist until the worker's session
    ends). Regression test for the review finding that no call site
    actually narrowed the path that produces the reported bloat."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    wt_path = state_dir / "worktrees" / "asgn-982-headless"
    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200
    for name in ["tui_submenu", "tui_scrollbar", "tui_colors"]:
        (examples_dir / name).write_bytes(payload)

    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "asgn-982-headless.log"
    log_path.write_text(
        "worker output...\n"
        "SMOKE_TESTS:\n"
        "- submenu opens — run tui_submenu — submenu renders\n"
        "END_SMOKE_TESTS\n"
    )

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )

    # Mimics _dispatch_headless: the /assign payload carries the repo's
    # full, unmodified glob as spec.artifact_paths — nothing narrows it
    # before dispatch.
    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=982,
        issue_title="headless narrow",
        briefing="b",
        branch="main",
        artifact_paths=["target/debug/examples/tui_*"],
    )
    a = AgentAssignment(
        id="asgn-982-headless",
        spec=spec,
        status=DONE,
        branch="issue-982-headless-narrow",
    )
    a.worktree_path = str(wt_path)
    a.log_path = str(log_path)

    server._stash_artifacts(a)

    stash_dir = (
        state_dir / "artifacts" / "quadraui" / "issue-982-headless-narrow"
    )
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}
    assert stashed == {"tui_submenu"}, (
        f"headless dispatch should narrow to the smoke-tested binary "
        f"named in the worker's own log; got {stashed!r}"
    )


def test_stash_artifacts_no_log_path_falls_back_to_full_glob(
    tmp_path: Path,
) -> None:
    """#982: with no log_path recorded on the assignment, narrowing is
    skipped entirely (nothing to parse) and the full glob is stashed —
    same behavior as before this fix, just guarding against AttributeError
    or a crash when log_path is unset."""
    from coord.agent import DONE, AgentAssignment, AgentServer, AssignmentSpec

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    wt_path = state_dir / "worktrees" / "asgn-982-nolog"
    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200
    for name in ["tui_submenu", "tui_scrollbar"]:
        (examples_dir / name).write_bytes(payload)

    server = AgentServer(
        machine_name="test",
        repos=["quadraui"],
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/sh", "-c", "echo ok"],
        repo_paths={"quadraui": str(tmp_path / "repo")},
        artifact_paths={"quadraui": ["target/debug/examples/tui_*"]},
    )

    spec = AssignmentSpec(
        repo_name="quadraui",
        repo_path=str(tmp_path / "repo"),
        issue_number=982,
        issue_title="no log path",
        briefing="b",
        branch="main",
        artifact_paths=["target/debug/examples/tui_*"],
    )
    a = AgentAssignment(
        id="asgn-982-nolog",
        spec=spec,
        status=DONE,
        branch="issue-982-nolog",
    )
    a.worktree_path = str(wt_path)
    assert a.log_path is None

    server._stash_artifacts(a)

    stash_dir = state_dir / "artifacts" / "quadraui" / "issue-982-nolog"
    stashed = {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")}
    assert stashed == {"tui_submenu", "tui_scrollbar"}


def test_stash_artifacts_for_branch_prunes_stale_files_on_narrowed_restash(
    tmp_path: Path,
) -> None:
    """#982: a re-stash with a narrower pattern set must shrink an existing
    oversized stash, not just avoid growing it. First stash with the full
    glob (simulating the unnarrowed first headless Work dispatch), then
    re-stash the same branch with only one file named — the other files
    left over from the first stash must be pruned."""
    from coord.agent import stash_artifacts_for_branch

    state_dir = tmp_path / "state"
    wt_path = tmp_path / "worktree"
    examples_dir = wt_path / "target" / "debug" / "examples"
    examples_dir.mkdir(parents=True)

    payload = b"\x7fELF" + b"\x00" * 200
    for name in ["tui_submenu", "tui_scrollbar", "tui_colors"]:
        (examples_dir / name).write_bytes(payload)

    # First stash: broad glob, all three files land in the stash.
    count1 = stash_artifacts_for_branch(
        worktree_path=wt_path,
        branch="issue-982-prune",
        repo_name="quadraui",
        patterns=["target/debug/examples/tui_*"],
        state_dir=state_dir,
        assignment_id="asgn-1",
    )
    assert count1 == 3

    stash_dir = state_dir / "artifacts" / "quadraui" / "issue-982-prune"
    assert {p.name for p in stash_dir.iterdir() if not p.name.startswith(".")} == {
        "tui_submenu",
        "tui_scrollbar",
        "tui_colors",
    }

    # Re-stash the same branch, narrowed to a single named binary (as if a
    # later fix-of/rework-of session narrowed against smoke tests). The
    # stale tui_scrollbar / tui_colors copies must be pruned, not just left
    # in place alongside the freshly re-copied tui_submenu.
    count2 = stash_artifacts_for_branch(
        worktree_path=wt_path,
        branch="issue-982-prune",
        repo_name="quadraui",
        patterns=["target/debug/examples/tui_submenu"],
        state_dir=state_dir,
        assignment_id="asgn-2",
    )
    assert count2 == 1

    stashed_after = {
        p.name for p in stash_dir.iterdir() if not p.name.startswith(".")
    }
    assert stashed_after == {"tui_submenu"}, (
        f"narrowed re-stash should prune stale files; got {stashed_after!r}"
    )
    # The assignment_id marker (a dotfile) must survive the prune.
    assert (stash_dir / ".assignment_id").read_text() == "asgn-2"


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


# ── #914: _find_live_worktree + artifact_absence_reason ────────────────────────


def test_find_live_worktree_matches_by_current_branch(tmp_path: Path) -> None:
    """_find_live_worktree locates a real `git worktree add` checkout by
    its current (sanitized) branch name, independent of directory naming."""
    rp = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=rp)

    wt_path = tmp_path / "state" / "worktrees" / "asgn-xyz"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-99-fix", str(wt_path)],
        cwd=str(rp),
        check=True,
        capture_output=True,
    )

    found = server._find_live_worktree("api", "issue-99-fix")
    assert found == wt_path


def test_find_live_worktree_returns_none_for_unknown_repo(tmp_path: Path) -> None:
    """No repo_paths entry for the requested repo → no crash, just None."""
    server = _server(tmp_path)
    assert server._find_live_worktree("no-such-repo", "issue-1-x") is None


def test_find_live_worktree_returns_none_when_branch_not_checked_out(
    tmp_path: Path,
) -> None:
    """A configured repo with no worktree on the requested branch → None."""
    server = _server(tmp_path)
    assert server._find_live_worktree("api", "issue-404-nonexistent") is None


def test_find_live_worktree_expands_tilde_repo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#939: repo_paths entries configured with a literal ``~`` (as in
    coordinator.yml on hosts that keep repos under the home directory) must
    resolve the same way every other repo_paths consumer in this module
    does (see the ``.expanduser()`` calls elsewhere in agent.py). Before the
    fix, ``_find_live_worktree`` passed the raw ``~``-prefixed string straight
    to ``git``, which cannot resolve ``~`` itself, so a live worktree was
    silently reported as missing.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    rp = _init_repo(fake_home / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo worker-output"],
        repo_paths={"api": "~/repo"},
    )

    wt_path = tmp_path / "state" / "worktrees" / "asgn-xyz"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-939-fix", str(wt_path)],
        cwd=str(rp),
        check=True,
        capture_output=True,
    )

    found = server._find_live_worktree("api", "issue-939-fix")
    assert found == wt_path


def test_artifact_absence_reason_worktree_present_no_patterns(
    tmp_path: Path,
) -> None:
    """Reason names 'no artifact_paths configured' when a live worktree
    exists but the repo isn't configured to stash anything."""
    rp = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, repo_path=rp)  # no artifact_paths kwarg

    wt_path = tmp_path / "state" / "worktrees" / "asgn-noconf"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-1-noconf", str(wt_path)],
        cwd=str(rp),
        check=True,
        capture_output=True,
    )

    reason = server.artifact_absence_reason("api", "issue-1-noconf")
    assert "no artifact_paths configured" in reason


def test_artifact_absence_reason_genuinely_absent(tmp_path: Path) -> None:
    """Reason correctly reports 'genuinely absent' when no worktree matches."""
    server = _server(tmp_path, artifact_paths={"api": ["target/debug/foo"]})
    reason = server.artifact_absence_reason("api", "issue-1-never-existed")
    assert "already merged" in reason or "nothing was ever built" in reason


def test_artifact_absence_reason_rejects_bad_path_components(
    tmp_path: Path,
) -> None:
    """repo/branch names outside the safe path-component charset get the
    same guard as artifact_manifest, not a crash from a bad path lookup."""
    server = _server(tmp_path)
    assert server.artifact_absence_reason("a/b", "issue-1-x") == "invalid repo/branch name"
    assert server.artifact_absence_reason("api", "a/b") == "invalid repo/branch name"


def test_artifact_manifest_lazy_stashes_from_live_worktree(tmp_path: Path) -> None:
    """artifact_manifest() self-heals: a live worktree still on the requested
    branch gets stashed on demand when the persistent stash is empty (#914),
    mirroring the vimcode #552 'missed finalize' scenario end-to-end."""
    rp = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path, repo_path=rp, artifact_paths={"api": ["target/debug/mybinary"]}
    )

    wt_path = tmp_path / "state" / "worktrees" / "asgn-552"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-552-fix", str(wt_path)],
        cwd=str(rp),
        check=True,
        capture_output=True,
    )
    (wt_path / "target" / "debug").mkdir(parents=True)
    (wt_path / "target" / "debug" / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-552-fix"
    assert not stash_dir.exists()

    manifest = server.artifact_manifest("api", "issue-552-fix")
    assert manifest is not None
    assert [f["name"] for f in manifest["files"]] == ["mybinary"]
    assert manifest["built_by_assignment_id"] == "asgn-552"
    assert (stash_dir / "mybinary").exists()


def test_artifact_manifest_none_when_worktree_present_but_no_files_match(
    tmp_path: Path,
) -> None:
    """artifact_manifest() returns None (→ 404), not an empty-but-200
    manifest, when a live worktree exists and artifact_paths is configured
    but nothing on disk matches the glob yet (#914 review regression case).

    stash_artifacts_for_branch's mkdir(parents=True, exist_ok=True) is
    unconditional, so a naive `stash_dir.exists()` success check would
    treat the freshly-created empty directory as "stashed" and both return
    a misleading 200 with zero files AND permanently block future retries
    for this branch. This asserts the fix: no content → None, and the
    empty directory doesn't poison a subsequent successful stash attempt.
    """
    rp = _init_repo(tmp_path / "repo")
    server = _server(
        tmp_path, repo_path=rp, artifact_paths={"api": ["target/debug/mybinary"]}
    )

    wt_path = tmp_path / "state" / "worktrees" / "asgn-nomatch"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-914-nomatch", str(wt_path)],
        cwd=str(rp),
        check=True,
        capture_output=True,
    )
    # No target/debug/mybinary in the worktree — the glob matches nothing.

    assert server.artifact_manifest("api", "issue-914-nomatch") is None

    # A later, successful build must still self-heal (no self-poisoning).
    (wt_path / "target" / "debug").mkdir(parents=True)
    (wt_path / "target" / "debug" / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    manifest = server.artifact_manifest("api", "issue-914-nomatch")
    assert manifest is not None
    assert [f["name"] for f in manifest["files"]] == ["mybinary"]


# ── stash_artifacts_for_branch standalone function (#562) ─────────────────────


def test_stash_artifacts_for_branch_copies_file(tmp_path: Path) -> None:
    """stash_artifacts_for_branch (module-level) copies matching files."""
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    binary = wt / "target" / "debug" / "coord-tui"
    binary.write_bytes(b"\x7fELF" + b"\x00" * 200)

    state_dir = tmp_path / "state"
    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-562-fix",
        repo_name="coord-tui",
        patterns=["target/debug/coord-tui"],
        state_dir=state_dir,
        assignment_id="aid-test",
    )

    stash = state_dir / "artifacts" / "coord-tui" / "issue-562-fix"
    assert count == 1
    assert (stash / "coord-tui").exists()
    assert (stash / ".assignment_id").read_text() == "aid-test"


def test_stash_artifacts_for_branch_noop_empty_patterns(tmp_path: Path) -> None:
    """Returns 0 immediately when patterns list is empty."""
    from coord.agent import stash_artifacts_for_branch

    count = stash_artifacts_for_branch(
        worktree_path=tmp_path / "wt",
        branch="some-branch",
        repo_name="myrepo",
        patterns=[],
        state_dir=tmp_path / "state",
    )
    assert count == 0
    assert not (tmp_path / "state" / "artifacts").exists()


def test_stash_artifacts_for_branch_noop_missing_worktree(tmp_path: Path) -> None:
    """Returns 0 when the worktree directory doesn't exist."""
    from coord.agent import stash_artifacts_for_branch

    count = stash_artifacts_for_branch(
        worktree_path=tmp_path / "nonexistent",
        branch="some-branch",
        repo_name="myrepo",
        patterns=["target/debug/foo"],
        state_dir=tmp_path / "state",
    )
    assert count == 0


def test_agent_stash_artifacts_delegates_to_standalone(tmp_path: Path) -> None:
    """AgentServer._stash_artifacts delegates to stash_artifacts_for_branch (#562).

    Verify the refactored wrapper still produces the correct stash so we
    haven't broken the existing worker path while extracting the function.
    """
    server, a, wt_path = _make_done_assignment(tmp_path)

    target_dir = wt_path / "target" / "debug"
    target_dir.mkdir(parents=True)
    (target_dir / "mybinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

    server._stash_artifacts(a)

    stash_dir = server.state_dir / "artifacts" / "api" / "issue-1-my-feature"
    assert (stash_dir / "mybinary").exists(), "delegation broke the stash"
    assert (stash_dir / ".assignment_id").read_text() == "asgn-abc123"


# ── Debug-symbol stripping + oversize warning (#940) ─────────────────────────


def test_strip_debug_symbols_runs_strip_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_strip_debug_symbols shells out to `strip -S <file>` when on PATH."""
    from coord import agent as agent_mod

    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return "/usr/bin/strip" if name == "strip" else None

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(agent_mod.shutil, "which", fake_which)
    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)

    target = tmp_path / "mybinary"
    target.write_bytes(b"\x7fELF" + b"\x00" * 200)

    assert agent_mod._strip_debug_symbols(target) is True
    assert calls == [["/usr/bin/strip", "-S", str(target)]]


def test_strip_debug_symbols_noop_when_strip_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `strip` on PATH: skip silently, never shell out."""
    from coord import agent as agent_mod

    monkeypatch.setattr(agent_mod.shutil, "which", lambda name: None)

    def fail_if_called(cmd, **kwargs):
        raise AssertionError("subprocess.run should not be called when strip is missing")

    monkeypatch.setattr(agent_mod.subprocess, "run", fail_if_called)

    target = tmp_path / "mybinary"
    target.write_bytes(b"\x7fELF" + b"\x00" * 200)

    assert agent_mod._strip_debug_symbols(target) is False


def test_strip_debug_symbols_returns_false_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed/erroring strip is swallowed — the original copy is kept."""
    from coord import agent as agent_mod

    monkeypatch.setattr(agent_mod.shutil, "which", lambda name: "/usr/bin/strip")

    def raising_run(cmd, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(agent_mod.subprocess, "run", raising_run)

    target = tmp_path / "mybinary"
    target.write_bytes(b"\x7fELF" + b"\x00" * 200)

    assert agent_mod._strip_debug_symbols(target) is False
    assert target.exists(), "file must survive a failed strip attempt"


def test_stash_artifacts_for_branch_strips_each_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every copied file is passed through _strip_debug_symbols (#940)."""
    from coord import agent as agent_mod

    stripped: list[Path] = []
    monkeypatch.setattr(
        agent_mod, "_strip_debug_symbols", lambda p: stripped.append(p) or True
    )

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "tui_a").write_bytes(b"\x7fELF" + b"\x00" * 200)
    (wt / "target" / "debug" / "tui_b").write_bytes(b"\x7fELF" + b"\x00" * 200)

    state_dir = tmp_path / "state"
    count = agent_mod.stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-940-strip",
        repo_name="quadraui",
        patterns=["target/debug/tui_*"],
        state_dir=state_dir,
    )

    assert count == 2
    stash_dir = state_dir / "artifacts" / "quadraui" / "issue-940-strip"
    assert sorted(p.name for p in stripped) == ["tui_a", "tui_b"]
    assert all(p.parent == stash_dir for p in stripped), "must strip the STASHED copy, not the source"


def test_stash_artifacts_for_branch_logs_oversize_warning(tmp_path: Path) -> None:
    """A stash over _STASH_WARN_BYTES appends a WARNING line to the log (#940)."""
    from coord import agent as agent_mod

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "bigbin").write_bytes(b"\x00" * 500)

    log_path = tmp_path / "assignment.log"
    log_path.write_text("")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(agent_mod, "_STASH_WARN_BYTES", 100)  # force the warning path
        count = agent_mod.stash_artifacts_for_branch(
            worktree_path=wt,
            branch="issue-940-warn",
            repo_name="quadraui",
            patterns=["target/debug/bigbin"],
            state_dir=tmp_path / "state",
            log_path=str(log_path),
        )

    assert count == 1
    log_text = log_path.read_text()
    assert "# stash WARNING" in log_text
    assert "quadraui" in log_text
    assert "--only" in log_text  # points the reader at the escape hatch


def test_stash_artifacts_for_branch_no_warning_under_threshold(tmp_path: Path) -> None:
    """A small stash produces no WARNING line."""
    from coord import agent as agent_mod

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "smallbin").write_bytes(b"\x00" * 500)

    log_path = tmp_path / "assignment.log"
    log_path.write_text("")

    count = agent_mod.stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-940-nowarn",
        repo_name="quadraui",
        patterns=["target/debug/smallbin"],
        state_dir=tmp_path / "state",
        log_path=str(log_path),
    )

    assert count == 1
    assert "WARNING" not in log_path.read_text()


# ── #1248: stash 0-copy robustness tests ─────────────────────────────────────


def test_stash_artifacts_for_branch_zero_copy_no_marker(tmp_path: Path) -> None:
    """A 0-copy stash must not write the .assignment_id marker.

    #1248: writing a marker on an empty stash is misleading — the manifest
    endpoint would surface a build that copied nothing.
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    wt.mkdir(parents=True)
    # pattern resolves to nothing — no files in worktree match

    state_dir = tmp_path / "state"
    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1248-zero",
        repo_name="myrepo",
        patterns=["target/debug/nonexistent_binary"],
        state_dir=state_dir,
        assignment_id="aid-zero",
    )

    assert count == 0
    stash_dir = state_dir / "artifacts" / "myrepo" / "issue-1248-zero"
    # stash dir was created by mkdir(parents=True, exist_ok=True) — that's fine
    assert not (stash_dir / ".assignment_id").exists(), (
        ".assignment_id must not be written on a 0-copy stash"
    )


def test_stash_artifacts_for_branch_zero_copy_warning_logged(tmp_path: Path) -> None:
    """A 0-copy stash appends a '# stash WARNING: 0 files matched' line.

    #1248: the worker log should be loud about a missed stash so the operator
    can diagnose a mis-configured artifact_paths without digging through the
    stash directory.
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    wt.mkdir(parents=True)

    log_path = tmp_path / "assignment.log"
    log_path.write_text("")

    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1248-warn",
        repo_name="myrepo",
        patterns=["target/debug/ghost_binary"],
        state_dir=tmp_path / "state",
        assignment_id="aid-warn",
        log_path=str(log_path),
    )

    assert count == 0
    log_text = log_path.read_text()
    assert "# stash WARNING" in log_text
    assert "0 files matched" in log_text
    assert "ghost_binary" in log_text


def test_stash_artifacts_for_branch_nonzero_copy_marker_written(tmp_path: Path) -> None:
    """When files ARE copied the .assignment_id marker is still written.

    #1248: the >0-copy path must be byte-for-byte identical to before.
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    (wt / "target" / "debug").mkdir(parents=True)
    (wt / "target" / "debug" / "mybin").write_bytes(b"\x7fELF" + b"\x00" * 200)

    state_dir = tmp_path / "state"
    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1248-ok",
        repo_name="myrepo",
        patterns=["target/debug/mybin"],
        state_dir=state_dir,
        assignment_id="aid-ok",
    )

    assert count == 1
    stash_dir = state_dir / "artifacts" / "myrepo" / "issue-1248-ok"
    assert (stash_dir / ".assignment_id").read_text() == "aid-ok"


def test_stash_artifacts_for_branch_zero_copy_no_warning_without_log(
    tmp_path: Path,
) -> None:
    """A 0-copy stash with no log_path provided must not raise.

    The warning path is only exercised when log_path is set; without it the
    function should return 0 silently.
    """
    from coord.agent import stash_artifacts_for_branch

    wt = tmp_path / "worktree"
    wt.mkdir(parents=True)

    count = stash_artifacts_for_branch(
        worktree_path=wt,
        branch="issue-1248-nolog",
        repo_name="myrepo",
        patterns=["target/debug/nobody"],
        state_dir=tmp_path / "state",
        assignment_id="aid-nolog",
    )

    assert count == 0
    # no exception, no marker
    stash_dir = tmp_path / "state" / "artifacts" / "myrepo" / "issue-1248-nolog"
    assert not (stash_dir / ".assignment_id").exists()


def test_sanitize_branch_replaces_slashes(tmp_path: Path) -> None:
    """_sanitize_branch should replace slashes with dashes."""
    from coord.agent import _sanitize_branch

    assert _sanitize_branch("feature/my-thing") == "feature-my-thing"
    assert _sanitize_branch("issue-305-artifact-pull") == "issue-305-artifact-pull"
    assert _sanitize_branch("refs/heads/main") == "refs-heads-main"


def test_sanitize_branch_agrees_with_rust() -> None:
    """Pin Python _sanitize_branch against every case tested in tui/src/app.rs.

    The Python sanitizer (agent stash path) and the Rust sanitizer (TUI manifest
    lookup) must produce identical output for the same input; a divergence means
    the TUI fetches the wrong URL and the [a] badge never appears (#433).
    """
    from coord.agent import _sanitize_branch

    cases = [
        # clean inputs — no change
        ("issue-305", "issue-305"),
        ("feature_foo.bar", "feature_foo.bar"),
        ("abc123", "abc123"),
        # slashes → dashes (single per run)
        ("feature/my-thing", "feature-my-thing"),
        ("a//b", "a-b"),
        # refs/heads/<name> — the typical fallback branch name
        ("refs/heads/main", "refs-heads-main"),
        # leading/trailing separators stripped
        ("/leading", "leading"),
        ("trailing/", "trailing"),
        ("/both/", "both"),
        # spaces
        ("my branch name", "my-branch-name"),
        # long real-world name — all allowed chars, unchanged
        (
            "issue-305-artifact-pull-rsync-built-binaries-from",
            "issue-305-artifact-pull-rsync-built-binaries-from",
        ),
    ]
    for raw, expected in cases:
        result = _sanitize_branch(raw)
        assert result == expected, (
            f"_sanitize_branch({raw!r}) == {result!r}, want {expected!r} "
            "(Rust and Python sanitizers disagree — tui/src/app.rs sanitize_branch "
            "has a matching test; fix both together)"
        )


# ── #324: Provider-layer routing and capability gates ─────────────────────────


def _make_provider(
    *,
    enforces_deny_list: bool = True,
    resume: bool = True,
    inject: bool = True,
    build_argv: list[str] | None = None,
    initial_input_bytes: bytes | None = None,
):
    """Create a minimal duck-typed provider object for testing.

    Returns an object with the same interface as coord.providers.base.Provider
    without importing from coord.providers (keeps the test free of the cycle).
    """
    from coord.providers.base import Capabilities

    class _FakeProvider:
        def capabilities(self):
            return Capabilities(
                resume=resume,
                inject=inject,
                cost_reporting=False,
                true_system_prompt=True,
                enforces_deny_list=enforces_deny_list,
                billing_mode="unknown",
            )

        def build_command(self, spec, *, resolved_model=None, **_kwargs):
            if build_argv is not None:
                return list(build_argv)
            return ["/bin/sh", "-c", "echo provider-argv"]

        def initial_input(self, spec):
            if initial_input_bytes is not None:
                return initial_input_bytes
            import json as _json
            payload = {"type": "user", "message": {"role": "user", "content": spec.briefing}}
            return (_json.dumps(payload) + "\n").encode()

        def result_marker(self):
            return '"type":"result"'

        def env(self):
            return {}

        def parse_log(self, log_path, tail_bytes=65536):
            pass

    return _FakeProvider()


class TestProviderLayerDispatch:
    """#324: _spawn() routes through the provider layer for non-PTY providers."""

    def test_no_config_parity_uses_worker_command(self, tmp_path: Path) -> None:
        """When spec.provider is None, _spawn uses self.worker_command — the
        legacy path.  The argv captured at Popen time must be identical to
        what worker_command returns (no-config parity, #324 requirement #1)."""
        import coord.agent as agent_mod

        repo = _init_repo(tmp_path / "repo")
        sentinel_argv = ["/bin/sh", "-c", "echo legacy-path"]

        captured: list[list[str]] = []
        real_popen = agent_mod.subprocess.Popen

        def recording_popen(spawn_argv, *args, **kwargs):
            if kwargs.get("start_new_session"):
                captured.append(spawn_argv)
            return real_popen(spawn_argv, *args, **kwargs)

        server = _server(
            tmp_path, argv=sentinel_argv, repo_path=repo, bash_wrap_spawn=False
        )
        agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
        try:
            # spec.provider is None → no provider in registry → legacy path
            a = server.assign(_spec(repo))
            final = server.wait_for(a.id, timeout=5)
        finally:
            agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

        # Worker makes no commits → advisory (#448)
        assert final.status == ADVISORY
        assert captured, "Popen was not called"
        # The legacy path must use sentinel_argv directly (no provider seam).
        assert captured[0] == sentinel_argv, (
            f"no-config parity: expected {sentinel_argv!r}, got {captured[0]!r}"
        )
        server.shutdown()

    def test_no_config_parity_stdin_is_user_message_line(self, tmp_path: Path) -> None:
        """With spec.provider=None, the initial stdin must be _user_message_line
        of the briefing — byte-identical to the pre-#324 path."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(
            tmp_path,
            argv=["/bin/sh", "-c", "read line; echo $line"],
            repo_path=repo,
        )
        # No provider set → legacy path
        a = server.assign(_spec(repo, briefing="parity-check"))
        final = server.wait_for(a.id, timeout=5)
        log = Path(final.log_path).read_text()
        # The stdin line must be a stream-json user message (same as _user_message_line)
        assert '"type": "user"' in log or '"type":"user"' in log
        assert "parity-check" in log
        server.shutdown()

    def test_provider_in_registry_uses_build_command(self, tmp_path: Path) -> None:
        """When spec.provider names a provider in the registry, _spawn calls
        provider.build_command() instead of self.worker_command()."""
        import coord.agent as agent_mod

        repo = _init_repo(tmp_path / "repo")
        provider_argv = ["/bin/sh", "-c", "echo provider-path"]
        legacy_argv = ["/bin/sh", "-c", "echo legacy-SHOULD-NOT-APPEAR"]
        fake_provider = _make_provider(build_argv=provider_argv)

        captured: list[list[str]] = []
        real_popen = agent_mod.subprocess.Popen

        def recording_popen(spawn_argv, *args, **kwargs):
            if kwargs.get("start_new_session"):
                captured.append(spawn_argv)
            return real_popen(spawn_argv, *args, **kwargs)

        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: legacy_argv,
            repo_paths={"api": str(repo)},
            providers={"myprovider": fake_provider},
            bash_wrap_spawn=False,
        )
        agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
        try:
            spec = _spec(repo, provider="myprovider")
            a = server.assign(spec)
            final = server.wait_for(a.id, timeout=5)
        finally:
            agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

        # Worker makes no commits → advisory (#448)
        assert final.status == ADVISORY
        assert captured, "Popen was not called"
        assert captured[0] == provider_argv, (
            f"expected provider argv {provider_argv!r}, got {captured[0]!r}"
        )
        log = Path(final.log_path).read_text()
        assert "legacy-SHOULD-NOT-APPEAR" not in log
        assert "provider-path" in log
        server.shutdown()

    def test_provider_unknown_name_falls_back_to_legacy(self, tmp_path: Path) -> None:
        """When spec.provider names a provider NOT in the registry, _spawn uses
        the legacy path (worker_command) — unknown providers are silently ignored
        to avoid breaking deployments during registry propagation."""
        import coord.agent as agent_mod

        repo = _init_repo(tmp_path / "repo")
        legacy_argv = ["/bin/sh", "-c", "echo legacy-fallback"]
        captured: list[list[str]] = []
        real_popen = agent_mod.subprocess.Popen

        def recording_popen(spawn_argv, *args, **kwargs):
            if kwargs.get("start_new_session"):
                captured.append(spawn_argv)
            return real_popen(spawn_argv, *args, **kwargs)

        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: legacy_argv,
            repo_paths={"api": str(repo)},
            providers={},  # empty registry
            bash_wrap_spawn=False,
        )
        agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
        try:
            spec = _spec(repo, provider="nonexistent-provider")
            a = server.assign(spec)
            final = server.wait_for(a.id, timeout=5)
        finally:
            agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]

        # Worker makes no commits → advisory (#448)
        assert final.status == ADVISORY
        assert captured and captured[0] == legacy_argv, (
            "unknown provider should fall back to legacy worker_command"
        )
        server.shutdown()

    def test_provider_initial_input_reaches_worker_stdin(self, tmp_path: Path) -> None:
        """initial_input() from the provider is written to the worker's stdin."""
        repo = _init_repo(tmp_path / "repo")

        # The worker echoes its first stdin line to stdout; we capture via log.
        import json as _json
        custom_briefing = "provider-briefing-text"
        custom_bytes = (
            _json.dumps({
                "type": "user",
                "message": {"role": "user", "content": custom_briefing},
            }) + "\n"
        ).encode()

        fake_provider = _make_provider(
            build_argv=["/bin/sh", "-c", "read line; echo $line"],
            initial_input_bytes=custom_bytes,
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "read line; echo $line"],
            repo_paths={"api": str(repo)},
            providers={"myprovider": fake_provider},
        )
        spec = _spec(repo, provider="myprovider", briefing="should-not-appear")
        a = server.assign(spec)
        final = server.wait_for(a.id, timeout=5)
        log = Path(final.log_path).read_text()
        assert custom_briefing in log, f"provider.initial_input bytes not in log: {log!r}"
        server.shutdown()


class TestCapabilityGates:
    """#324/#425: assign() enforces capability gates before spawning."""

    def test_deny_list_gate_refuses_work_on_unverified_provider(
        self, tmp_path: Path
    ) -> None:
        """work type on enforces_deny_list=False provider must raise ValueError."""
        repo = _init_repo(tmp_path / "repo")
        unsafe_provider = _make_provider(enforces_deny_list=False)
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"unsafe": unsafe_provider},
        )
        with pytest.raises(ValueError, match="enforces_deny_list=False"):
            server.assign(_spec(repo, type="work", provider="unsafe"))

    def test_deny_list_gate_refuses_review_on_unverified_provider(
        self, tmp_path: Path
    ) -> None:
        """review type on enforces_deny_list=False provider must raise ValueError."""
        repo = _init_repo(tmp_path / "repo")
        unsafe_provider = _make_provider(enforces_deny_list=False)
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"unsafe": unsafe_provider},
        )
        with pytest.raises(ValueError, match="enforces_deny_list=False"):
            server.assign(_spec(repo, type="review", provider="unsafe"))

    def test_deny_list_gate_allows_plan_on_unverified_provider(
        self, tmp_path: Path
    ) -> None:
        """plan type is non-mutating; unverified provider is allowed."""
        repo = _init_repo(tmp_path / "repo")
        # plan type is read-only — safe even on providers that don't enforce deny list
        unsafe_provider = _make_provider(
            enforces_deny_list=False,
            build_argv=["/bin/sh", "-c", "exit 0"],
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"unsafe": unsafe_provider},
        )
        # Must NOT raise — plan is in non-WRITE_CAPABLE_SPEC_TYPES
        a = server.assign(_spec(repo, type="plan", provider="unsafe"))
        server.wait_for(a.id, timeout=5)
        server.shutdown()

    def test_resume_gate_refuses_when_provider_lacks_resume(
        self, tmp_path: Path
    ) -> None:
        """resume_session_id on a provider with capabilities().resume=False must raise."""
        repo = _init_repo(tmp_path / "repo")
        no_resume_provider = _make_provider(resume=False)
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"no-resume": no_resume_provider},
        )
        with pytest.raises(ValueError, match="resume=False"):
            server.assign(
                _spec(repo, provider="no-resume", resume_session_id="ses-123")
            )

    def test_resume_gate_passes_when_provider_supports_resume(
        self, tmp_path: Path
    ) -> None:
        """resume_session_id on a provider with capabilities().resume=True is allowed."""
        repo = _init_repo(tmp_path / "repo")
        resumable_provider = _make_provider(
            resume=True,
            build_argv=["/bin/sh", "-c", "exit 0"],
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"resumable": resumable_provider},
        )
        # Must NOT raise — provider supports resume
        a = server.assign(
            _spec(repo, provider="resumable", resume_session_id="ses-abc")
        )
        server.wait_for(a.id, timeout=5)
        server.shutdown()

    def test_resume_gate_no_op_when_no_provider(self, tmp_path: Path) -> None:
        """With spec.provider=None the resume gate is a no-op (legacy path)."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        # resume_session_id set but no named provider → no gate, runs the legacy path
        a = server.assign(_spec(repo, resume_session_id="ses-no-gate"))
        final = server.wait_for(a.id, timeout=5)
        # No exception raised, assignment completes; no commits → advisory (#448)
        assert final.status == ADVISORY
        server.shutdown()

    def test_resume_gate_no_op_when_provider_not_in_registry(
        self, tmp_path: Path
    ) -> None:
        """When spec.provider is set but not in registry, resume gate is skipped."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)  # no providers registry
        # Named provider but not in registry → falls back to legacy path, no gate
        a = server.assign(
            _spec(repo, provider="unknown", resume_session_id="ses-no-gate")
        )
        final = server.wait_for(a.id, timeout=5)
        # No commits → advisory (#448)
        assert final.status == ADVISORY
        server.shutdown()

    def test_inject_message_refused_when_provider_inject_is_false(
        self, tmp_path: Path
    ) -> None:
        """inject_message raises RuntimeError when provider.capabilities().inject=False.

        This gates stdin-injection on providers that don't expose it (e.g.
        PTY-only or batch backends) so callers get a clear error rather than
        silently writing to an unresponsive pipe (#324).
        """
        import time as _time

        repo = _init_repo(tmp_path / "repo")
        # Provider with inject=False; worker blocks on stdin so the assignment
        # stays RUNNING long enough for inject_message to be called.
        no_inject_provider = _make_provider(
            inject=False,
            build_argv=["/bin/sh", "-c", "read line; echo done"],
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"no-inject": no_inject_provider},
        )
        a = server.assign(_spec(repo, provider="no-inject"))
        # Wait until running
        for _ in range(50):
            if server.get(a.id).status == RUNNING:
                break
            _time.sleep(0.02)
        assert server.get(a.id).status == RUNNING, "assignment never reached RUNNING"
        with pytest.raises(RuntimeError, match="inject=False"):
            server.inject_message(a.id, "should be refused")
        # Clean shutdown: assignment will finish once we unblock or the server stops
        server.shutdown()

    def test_inject_message_allowed_when_provider_inject_is_true(
        self, tmp_path: Path
    ) -> None:
        """inject_message succeeds when provider.capabilities().inject=True."""
        import time as _time

        repo = _init_repo(tmp_path / "repo")
        # Provider with inject=True (the default); worker reads two lines.
        inject_provider = _make_provider(
            inject=True,
            build_argv=["/bin/sh", "-c", "read a; read b; echo $b"],
        )
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo)},
            providers={"yes-inject": inject_provider},
        )
        a = server.assign(_spec(repo, provider="yes-inject"))
        # Wait until running
        for _ in range(50):
            if server.get(a.id).status == RUNNING:
                break
            _time.sleep(0.02)
        # Should NOT raise
        server.inject_message(a.id, "injected")
        final = server.wait_for(a.id, timeout=5)
        # Worker makes no commits → advisory (#448)
        assert final.status == ADVISORY
        server.shutdown()


# ── #452: Completed-assignment history cap ─────────────────────────────────────


class TestCompletedHistoryCap:
    """Verify that terminal assignments are pruned to _COMPLETED_HISTORY_CAP (#452)."""

    def _make_spec(self, repo_path: Path) -> AssignmentSpec:
        return AssignmentSpec(
            repo_name="api",
            repo_path=str(repo_path),
            issue_number=1,
            issue_title="t",
            briefing="b",
            branch="main",
        )

    def test_persist_caps_terminal_assignments(self, tmp_path: Path) -> None:
        """100 terminal assignments → _persist() keeps only the most recent 50
        in both memory and on disk; oldest entries are evicted."""
        N = 100
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)

        # Inject N synthetic terminal assignments directly (bypasses worker spawn).
        # Use monotonically increasing finished_at so "recent" is well-defined.
        spec = self._make_spec(repo)
        for i in range(N):
            a = AgentAssignment(
                id=f"cap{i:04d}",
                spec=spec,
                status=DONE,
                started_at=float(i),
                finished_at=float(i),
                exit_code=0,
            )
            server._assignments[a.id] = a

        server._persist()

        # ── In-memory state must be bounded ──────────────────────────────────
        assert len(server._assignments) <= _COMPLETED_HISTORY_CAP, (
            f"in-memory assignments not capped: {len(server._assignments)} > "
            f"{_COMPLETED_HISTORY_CAP}"
        )

        # The most recent N/2 entries (highest finished_at) must survive.
        kept_ids = {a.id for a in server._assignments.values()}
        for i in range(N // 2, N):  # cap0050 … cap0099
            assert f"cap{i:04d}" in kept_ids, (
                f"recent assignment cap{i:04d} was incorrectly dropped"
            )

        # The oldest N/2 entries must be gone.
        for i in range(N // 2):  # cap0000 … cap0049
            assert f"cap{i:04d}" not in kept_ids, (
                f"old assignment cap{i:04d} was incorrectly retained"
            )

        # ── Persisted file must be bounded ────────────────────────────────────
        state = json.loads(server.state_path.read_text())
        assert len(state["assignments"]) <= _COMPLETED_HISTORY_CAP, (
            f"persisted assignments not capped: {len(state['assignments'])} > "
            f"{_COMPLETED_HISTORY_CAP}"
        )
        file_ids = {a["id"] for a in state["assignments"]}
        for i in range(N // 2, N):
            assert f"cap{i:04d}" in file_ids, (
                f"recent assignment cap{i:04d} missing from persisted state"
            )
        for i in range(N // 2):
            assert f"cap{i:04d}" not in file_ids, (
                f"old assignment cap{i:04d} should not be in persisted state"
            )

    def test_active_assignments_are_never_pruned(self, tmp_path: Path) -> None:
        """Active (pending/running) assignments must not be touched by the cap,
        even when terminal count is below the cap."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        # Inject a mix of terminal and active assignments.
        for i in range(30):
            a = AgentAssignment(
                id=f"done{i:03d}",
                spec=spec,
                status=DONE,
                finished_at=float(i),
                exit_code=0,
            )
            server._assignments[a.id] = a

        active_id = "active-sentinel"
        server._assignments[active_id] = AgentAssignment(
            id=active_id,
            spec=spec,
            status=RUNNING,
            started_at=999.0,
        )

        server._persist()

        # Active assignment must still be in memory and on disk.
        assert active_id in server._assignments, "active assignment was pruned"
        state = json.loads(server.state_path.read_text())
        assert any(a["id"] == active_id for a in state["assignments"]), (
            "active assignment missing from persisted state"
        )

    def test_load_state_prunes_bloated_file(self, tmp_path: Path) -> None:
        """A pre-existing state file with > _COMPLETED_HISTORY_CAP terminal
        assignments is pruned in-memory immediately on load, so the first
        /status poll after restart is already bounded (#452)."""
        N = 80  # above the cap
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Write a bloated state file directly (simulates a pre-fix agent).
        spec_dict = {
            "repo_name": "api",
            "repo_path": str(tmp_path),
            "issue_number": 1,
            "issue_title": "t",
            "briefing": "b",
            "files_allowed": [],
            "files_forbidden": [],
            "branch": "main",
        }
        assignments = []
        for i in range(N):
            assignments.append({
                "id": f"old{i:04d}",
                "status": DONE,
                "pid": None,
                "started_at": float(i),
                "finished_at": float(i),
                "exit_code": 0,
                "log_path": None,
                "error": None,
                "branch": None,
                "worktree_path": None,
                "claude_session_id": None,
                "spec": dict(spec_dict),
            })
        (state_dir / "agent_state.json").write_text(
            json.dumps({"machine": "test", "capabilities": [], "repos": ["api"],
                        "assignments": assignments})
        )

        # Instantiate the server — _load_state() should prune immediately.
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=state_dir,
        )

        assert len(server._assignments) <= _COMPLETED_HISTORY_CAP, (
            f"_load_state() did not prune bloated file: "
            f"{len(server._assignments)} > {_COMPLETED_HISTORY_CAP}"
        )
        # The most recent entries (highest finished_at) must be kept.
        kept_ids = set(server._assignments.keys())
        for i in range(N - _COMPLETED_HISTORY_CAP, N):  # old0030 … old0079
            assert f"old{i:04d}" in kept_ids, (
                f"recent assignment old{i:04d} was incorrectly dropped on load"
            )

    def test_list_assignments_completed_is_bounded(self, tmp_path: Path) -> None:
        """list_assignments()['completed'] must not exceed the cap after _persist."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        for i in range(70):
            a = AgentAssignment(
                id=f"la{i:04d}",
                spec=spec,
                status=DONE,
                finished_at=float(i),
                exit_code=0,
            )
            server._assignments[a.id] = a

        server._persist()  # triggers in-memory prune

        listing = server.list_assignments()
        assert len(listing["completed"]) <= _COMPLETED_HISTORY_CAP, (
            f"list_assignments() returned {len(listing['completed'])} completed items, "
            f"expected ≤ {_COMPLETED_HISTORY_CAP}"
        )


# ── #667: list_assignments includes token counts in completed entries ──────────


class TestListAssignmentsTokens:
    """list_assignments()['completed'] entries include token counts parsed from
    the worker log so the coordinator can capture them without the log file."""

    def _make_spec(self, repo_path: Path) -> AssignmentSpec:
        return AssignmentSpec(
            repo_name="api",
            repo_path=str(repo_path),
            issue_number=1,
            issue_title="t",
            briefing="b",
            branch="main",
        )

    def _write_stream_json_log(
        self,
        log_path: Path,
        *,
        cost: float = 0.10,
        input_tokens: int = 500,
        output_tokens: int = 100,
        cache_creation_tokens: int = 20,
        cache_read_tokens: int = 80,
    ) -> None:
        """Write a minimal stream-json result event so parse_log picks it up."""
        import json as _json

        payload = {
            "type": "result",
            "subtype": "success",
            "result": "done",
            "total_cost_usd": cost,
            "num_turns": 2,
            "duration_ms": 5000,
            "session_id": "test-session",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
        }
        log_path.write_text(_json.dumps(payload) + "\n", encoding="utf-8")

    def test_token_counts_appear_in_completed_entry(self, tmp_path: Path) -> None:
        """When a completed assignment's stream-json log has token counts,
        list_assignments() includes them in the completed entry dict."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        log_path = tmp_path / "tok1.log"
        self._write_stream_json_log(
            log_path,
            input_tokens=1234,
            output_tokens=567,
            cache_creation_tokens=89,
            cache_read_tokens=321,
        )

        a = AgentAssignment(
            id="tok1",
            spec=spec,
            status=DONE,
            finished_at=1.0,
            exit_code=0,
            log_path=str(log_path),
        )
        server._assignments[a.id] = a

        listing = server.list_assignments()
        completed = listing["completed"]
        assert len(completed) == 1
        entry = completed[0]
        assert entry.get("input_tokens") == 1234
        assert entry.get("output_tokens") == 567
        assert entry.get("cache_creation_tokens") == 89
        assert entry.get("cache_read_tokens") == 321

    def test_no_tokens_when_log_absent(self, tmp_path: Path) -> None:
        """When the log path is absent, completed entry has no token fields
        (the coordinator handles missing keys gracefully)."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = self._make_spec(repo)

        a = AgentAssignment(
            id="tok2",
            spec=spec,
            status=DONE,
            finished_at=1.0,
            exit_code=0,
            log_path=None,
        )
        server._assignments[a.id] = a

        listing = server.list_assignments()
        completed = listing["completed"]
        assert len(completed) == 1
        entry = completed[0]
        # No token keys expected when log is absent — coordinator should
        # treat missing keys as 0, same as older agents.
        assert entry.get("input_tokens", 0) == 0
        assert entry.get("output_tokens", 0) == 0
