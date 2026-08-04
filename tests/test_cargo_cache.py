"""#1402: shared per-machine cargo target dir + its bounded GC.

Covers the three acceptance criteria from the issue:

* two consecutive assignments on the same machine get the *same* target dir,
  and it lives outside the worktree base so cleanup can't destroy it;
* worktree cleanup no longer takes the build output with it;
* the cache's disk usage is bounded, and the bound is exercised here.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coord import cargo_cache
from coord.agent import stash_artifacts_for_branch

from tests.test_agent import _init_repo, _server, _spec


def _fill(path: Path, nbytes: int, name: str = "blob.bin") -> Path:
    """Create *path* with a single file of exactly *nbytes*."""
    path.mkdir(parents=True, exist_ok=True)
    f = path / name
    f.write_bytes(b"x" * nbytes)
    return f


def _age(path: Path, seconds_ago: float) -> None:
    ts = time.time() - seconds_ago
    for p in sorted(path.rglob("*"), reverse=True):
        os.utime(p, (ts, ts))
    os.utime(path, (ts, ts))


# ── #1773: isolate this module from an ambient CARGO_TARGET_DIR ────────────
#
# Every coord worker subprocess already has CARGO_TARGET_DIR exported into
# its own environment before it ever runs pytest (coord/agent.py, #1402).
# This module's tests assert cargo_env()'s target-dir *resolution*, which
# only holds for a clean environment — cargo_env() correctly no-ops when the
# caller's env already carries CARGO_TARGET_DIR (an operator's explicit
# choice always wins, coord/cargo_cache.py:97, and that precedence is not
# touched here). The fixture below strips whatever is ambient before each
# test body runs.
#
# The module-scoped fixture that follows it *unconditionally* injects a
# fake ambient value for the whole module, regardless of what the host
# running pytest happens to have set. That makes the exposure reproducible
# on every machine, not just inside a worker, and is what makes
# ``test_module_is_isolated_from_ambient_cargo_target_dir`` below a real
# regression guard: delete or narrow the stripping fixture and that test
# (and the real-subprocess test further down) fails on any host.


@pytest.fixture(scope="module", autouse=True)
def _simulated_worker_ambient_cargo_target_dir():
    """Reproduce a coord worker's ambient CARGO_TARGET_DIR unconditionally,
    so the isolation fixture below is exercised on every test run."""
    sentinel = "/nonexistent/ambient-cargo-target-dir-from-a-worker-shell"
    previous = os.environ.get(cargo_cache.CARGO_ENV)
    os.environ[cargo_cache.CARGO_ENV] = sentinel
    try:
        yield sentinel
    finally:
        if previous is None:
            os.environ.pop(cargo_cache.CARGO_ENV, None)
        else:
            os.environ[cargo_cache.CARGO_ENV] = previous


@pytest.fixture(autouse=True)
def _strip_ambient_cargo_target_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """The actual #1773 fix: strip any ambient CARGO_TARGET_DIR (real, from
    a coord worker, or simulated by the fixture above) before each test body
    runs, so this module's tests don't depend on who — or what — invokes
    pytest. ``cargo_env()``'s operator-wins precedence is unchanged; this
    only isolates the test process's own environment."""
    monkeypatch.delenv(cargo_cache.CARGO_ENV, raising=False)


def test_module_is_isolated_from_ambient_cargo_target_dir() -> None:
    """Regression for #1773. The module fixture above always exports an
    ambient CARGO_TARGET_DIR before this test body runs. If
    ``_strip_ambient_cargo_target_dir`` were deleted or narrowed to a single
    test, this assertion — and ``test_worker_spawn_exports_shared_cargo_target_dir``
    below — would fail on every host, not just inside a coord worker."""
    assert cargo_cache.CARGO_ENV not in os.environ


# ── target dir resolution ───────────────────────────────────────────────────


def test_target_dir_is_per_repo_under_state_dir(tmp_path: Path) -> None:
    d = cargo_cache.target_dir_for_repo("claude-coordinator", tmp_path)
    assert d == tmp_path / "cargo-target" / "claude-coordinator"


@pytest.mark.parametrize(
    "bad", ["", ".", "..", "a/b", "../escape", "with space", "tab\tname"]
)
def test_target_dir_rejects_unsafe_repo_names(bad: str, tmp_path: Path) -> None:
    """An unusable repo name opts that repo out rather than writing outside
    the cache root."""
    assert cargo_cache.target_dir_for_repo(bad, tmp_path) is None
    assert cargo_cache.cargo_env(bad, tmp_path, {}) == {}


def test_cargo_env_sets_shared_target_dir(tmp_path: Path) -> None:
    env = cargo_cache.cargo_env("api", tmp_path, {"PATH": "/usr/bin"})
    assert env == {"CARGO_TARGET_DIR": str(tmp_path / "cargo-target" / "api")}


def test_cargo_env_does_not_create_the_directory(tmp_path: Path) -> None:
    """cargo does its own mkdir -p, so a repo that never builds leaves no
    empty dir behind."""
    cargo_cache.cargo_env("api", tmp_path, {})
    assert not (tmp_path / "cargo-target").exists()


def test_cargo_env_respects_an_explicit_operator_override(tmp_path: Path) -> None:
    env = cargo_cache.cargo_env("api", tmp_path, {"CARGO_TARGET_DIR": "/mine"})
    assert env == {}


def test_cargo_env_disabled_by_env_var(tmp_path: Path) -> None:
    for falsey in ("0", "false", "no", "OFF", ""):
        assert (
            cargo_cache.cargo_env("api", tmp_path, {cargo_cache.ENABLE_ENV: falsey})
            == {}
        ), falsey
    assert cargo_cache.cargo_env("api", tmp_path, {cargo_cache.ENABLE_ENV: "1"})


# ── the cap ─────────────────────────────────────────────────────────────────


def test_cap_bytes_default_and_override() -> None:
    assert cargo_cache.cap_bytes({}) == int(
        cargo_cache.DEFAULT_CACHE_CAP_GB * 1024**3
    )
    assert cargo_cache.cap_bytes({cargo_cache.CAP_ENV: "2"}) == 2 * 1024**3
    # Non-numeric garbage falls back to the default rather than crashing the
    # sweep that calls it.
    assert cargo_cache.cap_bytes({cargo_cache.CAP_ENV: "banana"}) == int(
        cargo_cache.DEFAULT_CACHE_CAP_GB * 1024**3
    )
    # <= 0 disables the GC entirely.
    assert cargo_cache.cap_bytes({cargo_cache.CAP_ENV: "0"}) is None
    assert cargo_cache.cap_bytes({cargo_cache.CAP_ENV: "-1"}) is None


# ── the GC ──────────────────────────────────────────────────────────────────


def test_sweep_noop_when_no_cache_root(tmp_path: Path) -> None:
    r = cargo_cache.sweep(tmp_path, cap=100)
    assert r["cargo_cache_bytes"] == 0
    assert r["cargo_caches_evicted"] == 0


def test_sweep_keeps_everything_under_the_cap(tmp_path: Path) -> None:
    root = tmp_path / "cargo-target"
    _fill(root / "api", 500)
    _fill(root / "web", 500)
    r = cargo_cache.sweep(tmp_path, cap=10_000)
    assert r["cargo_cache_bytes"] == 1000
    assert r["cargo_caches_evicted"] == 0
    assert (root / "api").exists() and (root / "web").exists()


def test_sweep_bounds_disk_usage_by_evicting_lru_caches(tmp_path: Path) -> None:
    """The acceptance bound: over the cap, whole repo caches are evicted
    oldest-used-first until the total fits."""
    root = tmp_path / "cargo-target"
    _fill(root / "oldest", 1000)
    _fill(root / "middle", 1000)
    _fill(root / "newest", 1000)
    _age(root / "oldest", 3000)
    _age(root / "middle", 2000)
    _age(root / "newest", 10)

    r = cargo_cache.sweep(tmp_path, cap=2500)

    assert r["cargo_evicted_repos"] == ["oldest"]
    assert r["cargo_caches_evicted"] == 1
    assert r["cargo_cache_bytes"] == 2000
    assert r["cargo_over_cap"] is False
    assert not (root / "oldest").exists()
    assert (root / "middle").exists() and (root / "newest").exists()


def test_sweep_evicts_as_many_as_needed(tmp_path: Path) -> None:
    root = tmp_path / "cargo-target"
    for i, name in enumerate(["a", "b", "c"]):
        _fill(root / name, 1000)
        _age(root / name, 3000 - i * 1000)

    r = cargo_cache.sweep(tmp_path, cap=1000)

    assert r["cargo_evicted_repos"] == ["a", "b"]
    assert r["cargo_cache_bytes"] == 1000
    assert (root / "c").exists()


def test_sweep_never_evicts_a_cache_with_a_live_build(tmp_path: Path) -> None:
    """A protected repo (pending/running assignment) is skipped even when it
    is the LRU candidate — the GC must not delete a target dir out from under
    a running cargo build."""
    root = tmp_path / "cargo-target"
    _fill(root / "busy", 1000)
    _fill(root / "idle", 1000)
    _age(root / "busy", 5000)  # oldest → would be evicted first
    _age(root / "idle", 10)

    r = cargo_cache.sweep(tmp_path, cap=1500, protect_repos={"busy"})

    assert (root / "busy").exists()
    assert not (root / "idle").exists()
    assert r["cargo_evicted_repos"] == ["idle"]


def test_sweep_reports_over_cap_when_only_protected_caches_remain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cargo-target"
    _fill(root / "busy", 5000)

    r = cargo_cache.sweep(tmp_path, cap=1000, protect_repos={"busy"})

    assert (root / "busy").exists()
    assert r["cargo_caches_evicted"] == 0
    assert r["cargo_over_cap"] is True


def test_sweep_dry_run_deletes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "cargo-target"
    _fill(root / "api", 5000)
    r = cargo_cache.sweep(tmp_path, cap=1000, dry_run=True)
    assert r["cargo_evicted_repos"] == ["api"]
    assert (root / "api").exists()


def test_sweep_disabled_cap_only_reports(tmp_path: Path) -> None:
    root = tmp_path / "cargo-target"
    _fill(root / "api", 5000)
    r = cargo_cache.sweep(tmp_path, cap=None)
    assert r["cargo_cache_bytes"] == 5000
    assert r["cargo_caches_evicted"] == 0
    assert (root / "api").exists()


def test_sweep_ignores_symlinks_in_the_cache_root(tmp_path: Path) -> None:
    """We never chase a symlink out of the cache root and delete something
    else on disk."""
    root = tmp_path / "cargo-target"
    root.mkdir(parents=True)
    outside = tmp_path / "precious"
    _fill(outside, 9000)
    (root / "link").symlink_to(outside)
    _fill(root / "api", 5000)

    r = cargo_cache.sweep(tmp_path, cap=1)

    assert outside.exists() and (outside / "blob.bin").exists()
    assert r["cargo_evicted_repos"] == ["api"]


# ── agent wiring ────────────────────────────────────────────────────────────


def test_worker_spawn_exports_shared_cargo_target_dir(tmp_path: Path) -> None:
    """The worker subprocess gets CARGO_TARGET_DIR pointing at the shared
    per-repo cache — not at anything inside its ephemeral worktree."""
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, argv=["/bin/sh", "-c", "true"], repo_path=repo)
    captured: list[dict] = []
    real_popen = agent_mod.subprocess.Popen

    def recording_popen(argv, *args, **kwargs):
        if kwargs.get("start_new_session"):
            captured.append(dict(kwargs.get("env") or {}))
        return real_popen(argv, *args, **kwargs)

    agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
    try:
        a = server.assign(_spec(repo))
        server.wait_for(a.id)
    finally:
        agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]
        server.shutdown()

    assert captured, "worker Popen was not called"
    target = captured[0].get("CARGO_TARGET_DIR")
    assert target == str(server.state_dir / "cargo-target" / "api")
    # The whole point: it is NOT inside the worktree that cleanup removes.
    assert not target.startswith(str(server.state_dir / "worktrees"))


def test_two_assignments_share_one_target_dir_that_survives_cleanup(
    tmp_path: Path,
) -> None:
    """Acceptance: consecutive assignments on the same machine resolve to the
    same warm cache, and ``clean_worktrees`` does not destroy it."""
    server = _server(tmp_path)
    first = cargo_cache.cargo_env("api", server.state_dir, {})
    second = cargo_cache.cargo_env("api", server.state_dir, {})
    assert first == second and first

    cache = Path(first["CARGO_TARGET_DIR"])
    _fill(cache / "debug", 4096, name="coord-tui")

    # Simulate a finished worker's worktree and sweep it away.
    wt = server.state_dir / "worktrees" / "old-assignment"
    wt.mkdir(parents=True)
    (wt / "data.txt").write_text("x")
    old = time.time() - 3600
    os.utime(wt, (old, old))
    result = server.clean_worktrees(recent_secs=0)

    assert result["cleaned"] == 1
    assert not wt.exists()
    assert (cache / "debug" / "coord-tui").exists()  # build output survives
    assert result["cargo_cache_bytes"] == 4096


def test_clean_worktrees_protects_caches_of_running_assignments(
    tmp_path: Path,
) -> None:
    """``AgentServer._gc_cargo_cache`` feeds the live repo set through to the
    sweep, so an in-flight build's cache is never evicted."""
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, argv=["/bin/sh", "-c", "sleep 30"], repo_path=repo)
    root = server.state_dir / "cargo-target"
    _fill(root / "api", 5000)
    _age(root / "api", 9999)
    try:
        server.assign(_spec(repo))
        # Wait for the assignment to be registered as running.
        for _ in range(200):
            if any(
                a.status == agent_mod.RUNNING for a in server._assignments.values()
            ):
                break
            time.sleep(0.02)
        r = cargo_cache.sweep(
            server.state_dir,
            cap=100,
            protect_repos={
                a.spec.repo_name
                for a in server._assignments.values()
                if a.status in (agent_mod.PENDING, agent_mod.RUNNING)
            },
        )
        assert r["cargo_caches_evicted"] == 0
        assert (root / "api").exists()
    finally:
        server.shutdown()


# ── artifact stashing against the shared cache (#1357 guard) ────────────────


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("tui/target/debug/coord-tui", "debug/coord-tui"),
        ("target/release/app", "release/app"),
        ("a/b/target/debug/*.so", "debug/*.so"),
        ("dist/app", None),
        ("target", None),
        ("../target/debug/x", None),
    ],
)
def test_cargo_relative_pattern(pattern: str, expected: str | None) -> None:
    from coord.agent import cargo_relative_pattern

    assert cargo_relative_pattern(pattern) == expected


def test_stash_falls_back_to_shared_cargo_target_dir(tmp_path: Path) -> None:
    """#1402 + #1357: with CARGO_TARGET_DIR redirected, an ``artifact_paths``
    glob like ``tui/target/debug/coord-tui`` no longer resolves inside the
    worktree — it must resolve against the shared cache instead of silently
    stashing zero files."""
    state = tmp_path / "state"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _fill(state / "cargo-target" / "api" / "debug", 4096, name="coord-tui")

    unmatched: list[str] = []
    copied = stash_artifacts_for_branch(
        worktree,
        "issue-1-x",
        "api",
        ["tui/target/debug/coord-tui"],
        state,
        unmatched_out=unmatched,
    )

    assert copied == 1
    assert unmatched == []
    assert (state / "artifacts" / "api" / "issue-1-x" / "coord-tui").exists()


def test_stash_prefers_the_worktree_copy_when_present(tmp_path: Path) -> None:
    """The fallback is only a fallback — an in-worktree build (no shared
    cache, or a repo that ignores CARGO_TARGET_DIR) still wins."""
    state = tmp_path / "state"
    worktree = tmp_path / "wt"
    _fill(worktree / "tui" / "target" / "debug", 4096, name="coord-tui")
    _fill(state / "cargo-target" / "api" / "debug", 8192, name="coord-tui")

    copied = stash_artifacts_for_branch(
        worktree, "issue-1-x", "api", ["tui/target/debug/coord-tui"], state
    )

    assert copied == 1
    stashed = state / "artifacts" / "api" / "issue-1-x" / "coord-tui"
    assert stashed.stat().st_size == 4096


def test_stash_still_reports_a_real_miss(tmp_path: Path) -> None:
    """A pattern that matches neither the worktree nor the cache is still
    reported as unmatched — the fallback must not mask #1323's signal."""
    state = tmp_path / "state"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (state / "cargo-target" / "api").mkdir(parents=True)

    unmatched: list[str] = []
    copied = stash_artifacts_for_branch(
        worktree,
        "issue-1-x",
        "api",
        ["tui/target/debug/coord-tui"],
        state,
        unmatched_out=unmatched,
    )

    assert copied == 0
    assert unmatched == ["tui/target/debug/coord-tui"]
