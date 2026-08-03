"""#1729 (H-6): self-healing graph rebuild — poll the state, don't chase events.

The git hooks are event-driven and structurally cannot cover every ref-moving
operation (rebase/merge/cherry-pick `exit 0`, `git reset --hard` fires no
hook at all, every failure path is a silent `exit 0`) — see
`coord.graph_health`'s module docstring. H-5's `graph` check already computes
a total STATE predicate (stale vs HEAD); this suite exercises the companion
piece that reacts to it: `AgentServer._self_heal_stale_graphs`, wired into
the existing cached `/health` tick (`_cached_local_health`).

Four guards are load-bearing and each gets a dedicated test:

1. idle-gate (only rebuild with zero RUNNING assignments)
2. base checkouts only (never a linked worktree)
3. once per HEAD sha (never a retry loop)
4. fail loud, never `--force`

Plus the #1625 decision 3 / #1485-precedent requirement that health must
stay advisory: a dispatch arriving mid-rebuild must not be delayed.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from coord.agent import (
    PENDING,
    RUNNING,
    AgentServer,
    AssignmentSpec,
    _git,
    _is_linked_worktree,
)
from coord.health.models import CheckResult, Checkout, HealthContext, Severity
from coord.health.registry import HealthReport


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def _write_graph(repo: Path, built_sha: str) -> None:
    out = repo / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text("{}")
    (out / "GRAPH_REPORT.md").write_text(f"- Built from commit: `{built_sha}`\n")


def _health_config(repo_path: Path, repo_name: str = "api") -> SimpleNamespace:
    """A fake coordinator.yml config resolving *repo_name* to *repo_path*.

    Uses a machine name guaranteed not to match this test-runner's hostname
    so `local_checkouts`'s fallback pass includes it unconditionally,
    without needing to monkeypatch `socket.gethostname` (see
    `tests/test_health_context.py` for the pattern this mirrors).
    """
    repo_paths = {repo_name: str(repo_path)}
    return SimpleNamespace(
        repos=[SimpleNamespace(name=repo_name, default_branch="main", develop_branch=None)],
        machines=[
            SimpleNamespace(
                name="definitely-not-this-test-runner",
                host="definitely-not-this-test-runner.ts.net",
                repos=[repo_name],
                repo_paths=repo_paths,
                repo_path=lambda rn, _p=repo_paths: _p.get(rn),
            )
        ],
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


def _server(tmp_path: Path, repo_path: Path, **kwargs) -> AgentServer:
    return AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo worker-output"],
        repo_paths={"api": str(repo_path)},
        health_config=_health_config(repo_path),
        **kwargs,
    )


def _graph_result(server: AgentServer) -> dict:
    (result,) = [r for r in server.health()["health"]["results"] if r["check_id"] == "graph"]
    return result


# ── guard 1: idle-gate ───────────────────────────────────────────────────────


def test_stale_checkout_on_idle_machine_self_heals(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)  # deliberately not HEAD
    server = _server(tmp_path, repo)

    calls = []

    def _fake_update(repo_path: Path):
        calls.append(repo_path)
        head = _git(repo_path, "rev-parse", "HEAD")
        _write_graph(repo_path, built_sha=head)
        return True, "No code-graph topology changes detected"

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)

    result = _graph_result(server)
    assert calls == [repo]
    assert result["severity"] == "ok"
    assert result["values"]["stale"] is False


def test_machine_with_a_running_assignment_does_not_rebuild(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)
    server = _server(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        "coord.agent._graphify_update",
        lambda repo_path: (calls.append(repo_path), (True, "ok"))[1],
    )

    spec = _spec(repo)
    with server._lock:
        from coord.agent import AgentAssignment  # noqa: PLC0415

        server._assignments["fake-running"] = AgentAssignment(
            id="fake-running", spec=spec, status=RUNNING
        )

    result = _graph_result(server)
    assert calls == []
    assert result["severity"] in ("warn", "crit")
    assert result["values"]["stale"] is True


# ── guard 2: base checkouts only, never a linked worktree ───────────────────


def test_is_linked_worktree_predicate(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature", str(wt)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    assert _is_linked_worktree(repo) is False
    assert _is_linked_worktree(wt) is True


def test_linked_worktree_is_never_rebuilt(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature", str(wt)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    server = _server(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        "coord.agent._graphify_update",
        lambda repo_path: (calls.append(repo_path), (True, "ok"))[1],
    )

    ctx = HealthContext(
        thresholds=SimpleNamespace(),
        home=tmp_path,
        coord_dir=tmp_path / ".coord",
        now=1_800_000_000.0,
        checkouts=(Checkout(name="wt", path=wt),),
    )
    report = HealthReport(
        results=[
            CheckResult(
                check_id="graph",
                scope="checkout",
                severity=Severity.CRIT,
                headroom="stale",
                subject="wt",
                values={
                    "path": str(wt),
                    "stale": True,
                    "head_sha": "deadbeef",
                    "is_symlink": False,
                },
            )
        ]
    )

    server._self_heal_stale_graphs(ctx, report)
    assert calls == [], "must never rebuild in a linked worktree, under any condition"
    assert report.results[0].severity == Severity.CRIT  # untouched


# ── guard 3: once per HEAD sha, never a retry loop ──────────────────────────


def test_failed_rebuild_is_attempted_exactly_once_per_head(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)
    server = _server(tmp_path, repo)

    calls = []
    refusal = (
        "[graphify] WARNING: new graph has 20748 nodes but existing graph.json "
        "has 20757. Refusing to overwrite"
    )

    def _fake_update(repo_path: Path):
        calls.append(repo_path)
        return False, refusal

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)

    # Three separate polls against the same HEAD: only the first actually
    # attempts a rebuild.
    for _ in range(3):
        server._local_health_cache = None
        result = _graph_result(server)

    assert len(calls) == 1
    assert result["severity"] == "warn"
    assert "self-heal failed" in result["headroom"]
    assert refusal in result["detail"]

    # HEAD moves -> a fresh attempt is made (and fails the same way here).
    (repo / "file2").write_text("more\n")
    subprocess.run(["git", "add", "file2"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "second"], cwd=str(repo), check=True, capture_output=True
    )
    server._local_health_cache = None
    _graph_result(server)
    assert len(calls) == 2


# ── guard 4: fail loud, never --force ────────────────────────────────────────


def test_graphify_update_never_passes_force(monkeypatch, tmp_path: Path) -> None:
    """The actual command line the self-heal path shells out to — never
    ``--force``, the flag that exists solely to defeat graphify's own
    node-count refusal guard (the guard the 2026-08-02 incident hit)."""
    import coord.agent as agent_mod

    captured: dict = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(agent_mod.subprocess, "run", _fake_run)

    ok, _detail = agent_mod._graphify_update(tmp_path)
    assert ok is True
    assert captured["argv"] == ["graphify", "update", "."]
    assert "--force" not in captured["argv"]


# ── #1625 decision 3 / #1485 precedent: health must stay advisory ──────────


def test_assignment_lock_is_released_before_the_rebuild_subprocess_runs(
    tmp_path: Path, monkeypatch
) -> None:
    """A rebuild in progress must never make a dispatch wait on it.

    Simulates a slow `graphify update .` and asserts the same lock `assign()`
    takes (`server._lock`) is acquirable *while the rebuild is still running*
    — i.e. the self-heal pass never holds it across the subprocess call.
    """
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)
    server = _server(tmp_path, repo)

    rebuild_started = threading.Event()
    release_rebuild = threading.Event()

    def _fake_update(repo_path: Path):
        rebuild_started.set()
        release_rebuild.wait(timeout=5.0)
        return True, "ok"

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)

    t = threading.Thread(target=server.health, daemon=True)
    t.start()
    try:
        assert rebuild_started.wait(timeout=5.0), "fake rebuild never started"
        acquired = server._lock.acquire(timeout=2.0)
        assert acquired, "self-heal held the assignment lock across the rebuild subprocess"
        server._lock.release()
    finally:
        release_rebuild.set()
        t.join(timeout=5.0)


def test_dispatch_arriving_mid_rebuild_is_accepted_not_delayed(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)
    server = _server(tmp_path, repo)

    rebuild_started = threading.Event()
    release_rebuild = threading.Event()

    def _fake_update(repo_path: Path):
        rebuild_started.set()
        release_rebuild.wait(timeout=5.0)
        head = _git(repo_path, "rev-parse", "HEAD")
        _write_graph(repo_path, built_sha=head)
        return True, "ok"

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)

    t = threading.Thread(target=server.health, daemon=True)
    t.start()
    try:
        assert rebuild_started.wait(timeout=5.0), "fake rebuild never started"
        assignment = server.assign(_spec(repo))
        assert assignment.status in (PENDING, RUNNING)
    finally:
        release_rebuild.set()
        t.join(timeout=5.0)
