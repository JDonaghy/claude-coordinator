"""Regression tests for #255 — worker branches must start at the origin's
default-branch SHA, never at a local-only ref.

The pre-fix behavior silently fell back to `git rev-parse <branch>` (local)
when `origin/<branch>` couldn't be resolved.  Combined with the dispatch
path defaulting to a hardcoded "main" instead of `repo.default_branch`,
that meant a worker on a machine with unpushed local commits on `develop`
would create a feature branch that included those unpushed commits — the
exact symptom from quadraui#233.

Also contains regression tests for #460 — git-worktree branch collision
when serial fix/retry/PR-worker dispatches use the same branch name.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from coord.agent import (
    ADVISORY,
    DONE,
    FAILED,
    PENDING,
    AgentAssignment,
    AgentServer,
    AssignmentSpec,
    _git_worktree_add,
    _free_branch_in_worktrees,
    _parse_worktree_porcelain,
    _GitError,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Set up: bare remote + clone with `origin` configured.

    Returns (clone_path, remote_path).  Initial commit is pushed to
    `origin/develop` so the worker has something to branch from.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "develop", str(remote)],
        check=True, capture_output=True,
    )

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "develop")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "README").write_text("v1\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "remote", "add", "origin", str(remote))
    _git(clone, "push", "-u", "origin", "develop")
    return clone, remote


def test_worker_branches_from_origin_not_local(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """When local `develop` has unpushed commits ahead of `origin/develop`,
    the worker's branch base must equal `origin/develop`'s SHA — not the
    local-only commits.  This is the core regression for #255."""
    clone, _remote = repo_with_remote

    # The SHA we EXPECT the worker to branch from.
    origin_sha = _git(clone, "rev-parse", "origin/develop")

    # Now simulate the bug condition: user has unpushed WIP on local develop.
    (clone / "WIP.txt").write_text("unpushed\n")
    _git(clone, "add", "WIP.txt")
    _git(clone, "commit", "-m", "wip: not pushed yet")
    local_sha = _git(clone, "rev-parse", "develop")
    assert local_sha != origin_sha, "fixture setup: local should be ahead"

    # The worker runs inside the worktree (cwd is set by the agent), so a
    # plain `git rev-parse HEAD` here records the branch's base SHA.  We
    # write to a path outside the worktree so the value survives the
    # post-reap cleanup.
    out_file = tmp_path / "worker_state.txt"
    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "sh", "-c",
            f"echo BASE_SHA=$(git rev-parse HEAD) >> {out_file}; "
            f"[ -f WIP.txt ] && echo WIP_LEAKED=yes >> {out_file} "
            f"|| echo WIP_LEAKED=no >> {out_file}",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(clone),
        issue_number=42, issue_title="add x", briefing="b",
        branch="develop",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no git commits (only echoes to a file) → advisory (#448)
    assert final.status in (DONE, ADVISORY), f"assignment failed: {final.error}"

    recorded = out_file.read_text()
    assert f"BASE_SHA={origin_sha}" in recorded, (
        f"worker's branch did not start at origin/develop. recorded:\n"
        f"{recorded}\norigin_sha={origin_sha}, local_sha={local_sha}"
    )
    assert "WIP_LEAKED=no" in recorded, (
        f"unpushed WIP.txt leaked into the worker's branch — #255 regression\n"
        f"{recorded}"
    )
    server.shutdown()


def test_dispatch_fails_when_origin_configured_but_unreachable(
    tmp_path: Path,
) -> None:
    """If `origin` is configured but `origin/<default>` can't be resolved
    (e.g. fetch failed and the ref was never cached locally), the agent
    must fail the dispatch with a clear error rather than silently
    falling back to a local ref."""
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "README").write_text("v1\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")
    # Point origin at a nonexistent path.  Fetch will fail, and there's
    # no cached `origin/main` ref, so rev-parse must fail too.
    _git(clone, "remote", "add", "origin", str(tmp_path / "nonexistent.git"))

    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(clone),
        issue_number=1, issue_title="t", briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == FAILED, "expected failure, got DONE"
    assert "origin/main" in (final.error or ""), (
        f"expected error to mention origin/main, got: {final.error!r}"
    )
    server.shutdown()


def test_no_remote_falls_back_to_local(
    tmp_path: Path,
) -> None:
    """When `origin` is not configured at all (test fixtures, local-only
    repos), the agent falls back to the local branch — without this the
    pre-existing test suite (which uses bare local repos) would break."""
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "README").write_text("v1\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")
    # No `git remote add` — origin is not configured.

    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(clone),
        issue_number=1, issue_title="t", briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no commits, no remote → advisory via local fallback (#448)
    assert final.status == ADVISORY, f"assignment failed: {final.error}"
    server.shutdown()


def test_unpushed_commits_warning_in_log(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """When the worker's machine has unpushed commits on the default
    branch, a warning gets written to the assignment log — the user's
    WIP is safe (the worker isn't using it) but they should know it's
    sitting there."""
    clone, _remote = repo_with_remote
    (clone / "WIP.txt").write_text("unpushed\n")
    _git(clone, "add", "WIP.txt")
    _git(clone, "commit", "-m", "wip: not pushed yet")

    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(clone),
        issue_number=1, issue_title="t", briefing="b",
        branch="develop",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no commits → advisory (#448); unpushed-commits warning
    # is in the log regardless of advisory status.
    assert final.status in (DONE, ADVISORY)
    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "ahead of origin/develop" in log_text, (
        f"expected unpushed-commits warning in log, got:\n{log_text}"
    )
    server.shutdown()


def test_stale_remote_tracking_ref_does_not_hijack_fresh_branch(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """#412: a stale ``refs/remotes/origin/<branch>`` (branch deleted on origin
    but not pruned) must NOT become the base for a 'fresh' worker.  Pre-fix, a
    prune-less fetch left the dead ref behind, the origin_has_branch check
    matched it, and the worker silently branched off the old deleted-branch SHA.
    """
    clone, _remote = repo_with_remote
    origin_sha = _git(clone, "rev-parse", "origin/develop")

    # Fabricate an old feature commit and plant a STALE remote-tracking ref for
    # a branch that does NOT exist on origin — exactly the state a prune-less
    # fetch leaves after the upstream branch is deleted.
    _git(clone, "checkout", "-b", "tmp-old")
    (clone / "OLD.txt").write_text("stale feature work\n")
    _git(clone, "add", "OLD.txt")
    _git(clone, "commit", "-m", "old #42 work (deleted on origin)")
    stale_sha = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "develop")
    _git(clone, "branch", "-D", "tmp-old")
    # Agent derives branch name issue-42-add-x from (number, title).
    _git(clone, "update-ref", "refs/remotes/origin/issue-42-add-x", stale_sha)
    assert stale_sha != origin_sha

    out_file = tmp_path / "worker_state.txt"
    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "sh", "-c",
            f"echo BASE_SHA=$(git rev-parse HEAD) >> {out_file}; "
            f"[ -f OLD.txt ] && echo STALE_LEAKED=yes >> {out_file} "
            f"|| echo STALE_LEAKED=no >> {out_file}",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(clone),
        issue_number=42, issue_title="add x", briefing="b",
        branch="develop",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no git commits (only echoes to a file) → advisory (#448)
    assert final.status in (DONE, ADVISORY), f"assignment failed: {final.error}"

    recorded = out_file.read_text()
    assert f"BASE_SHA={origin_sha}" in recorded, (
        f"worker branched off the STALE ref instead of origin/develop (#412).\n"
        f"{recorded}\norigin_sha={origin_sha} stale_sha={stale_sha}"
    )
    assert "STALE_LEAKED=no" in recorded, (
        f"stale deleted-branch work (OLD.txt) leaked into the worker's branch "
        f"— #412 regression\n{recorded}"
    )
    server.shutdown()


# ── #460 — worktree branch-collision tests ────────────────────────────────────


def _init_local_repo(path: Path) -> Path:
    """Create a minimal local-only git repo (no remote) with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def test_parse_worktree_porcelain_basic(tmp_path: Path) -> None:
    """_parse_worktree_porcelain correctly extracts path and branch."""
    repo = _init_local_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature", str(wt), "HEAD"],
        cwd=str(repo), check=True, capture_output=True,
    )
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    entries = _parse_worktree_porcelain(out)
    paths = {e["worktree"] for e in entries}
    assert str(repo.resolve()) in paths
    assert str(wt.resolve()) in paths
    feature_entry = next((e for e in entries if e.get("branch") == "feature"), None)
    assert feature_entry is not None, f"no feature branch in entries: {entries}"
    assert feature_entry["worktree"] == str(wt.resolve())


def test_free_branch_removes_stale_worktree(tmp_path: Path) -> None:
    """_free_branch_in_worktrees removes a worktree holding the target branch
    (but leaves worktrees on other branches untouched)."""
    repo = _init_local_repo(tmp_path / "repo")
    stale = tmp_path / "stale"
    other = tmp_path / "other"
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-42-fix", str(stale), "HEAD"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-7-unrelated", str(other), "HEAD"],
        cwd=str(repo), check=True, capture_output=True,
    )

    new_wt = str(tmp_path / "new")
    _free_branch_in_worktrees(repo, "issue-42-fix", new_wt)

    # stale worktree directory should be gone
    assert not stale.exists(), "stale worktree directory was not removed"

    # git admin should no longer see issue-42-fix as checked out
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    entries = _parse_worktree_porcelain(out)
    branches = {e.get("branch") for e in entries}
    assert "issue-42-fix" not in branches, (
        f"issue-42-fix still appears in worktree list after free: {entries}"
    )
    # other branch must be untouched
    assert "issue-7-unrelated" in branches


def test_setup_worktree_frees_stale_branch(tmp_path: Path) -> None:
    """#460 Part 1: a second assignment on the same branch succeeds even when
    the first assignment's worktree is still registered (simulating a crash
    before _cleanup_worktree ran).

    Uses a local-only repo so the test doesn't need network access.
    """
    repo = _init_local_repo(tmp_path / "repo")

    # Simulate assignment A's stale worktree: check out 'issue-99-collide'
    # at an arbitrary path (not the path the new assignment will use).
    stale_wt = tmp_path / "stale-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-99-collide", str(stale_wt), "HEAD"],
        cwd=str(repo), check=True, capture_output=True,
    )
    assert stale_wt.exists()

    # Assignment B: same branch name (same issue number + title)
    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo),
        issue_number=99, issue_title="collide", briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    # Worker makes no commits (local-only repo) → advisory (#448)
    assert final.status == ADVISORY, (
        f"assignment failed despite stale worktree eviction: {final.error}"
    )
    # The stale worktree should have been cleaned up by _free_branch_in_worktrees.
    assert not stale_wt.exists(), (
        "stale worktree directory was not removed by _free_branch_in_worktrees"
    )
    server.shutdown()


def test_git_worktree_add_retries_on_collision(tmp_path: Path) -> None:
    """#460 Part 2: _git_worktree_add retries once when the first attempt fails
    with the 'already used by worktree at' collision error.

    We create a conflicting worktree manually (so git holds the branch), then
    call _git_worktree_add directly.  Since _free_branch_in_worktrees is NOT
    called beforehand, the first worktree add fails; the retry path in
    _git_worktree_add detects the collision, removes the conflicting worktree,
    and retries successfully.
    """
    repo = _init_local_repo(tmp_path / "repo")

    # Create the conflicting worktree that holds 'issue-55-retry'.
    conflict_wt = tmp_path / "conflict-wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-55-retry", str(conflict_wt), "HEAD"],
        cwd=str(repo), check=True, capture_output=True,
    )

    # Target for the new assignment — a fresh path, not the conflicting one.
    new_wt = tmp_path / "new-wt"

    # Call _git_worktree_add directly (no proactive free).  First attempt fails;
    # the retry inside _git_worktree_add should clean up and succeed.
    log = tmp_path / "test.log"
    log.write_text("")
    _git_worktree_add(
        repo,
        ["-B", "issue-55-retry", str(new_wt), "HEAD"],
        log_path=str(log),
    )

    # The new worktree should exist at new_wt.
    assert new_wt.exists(), "retry did not create the new worktree"
    # The log should contain the collision + retry message.
    log_text = log.read_text()
    assert "collision" in log_text or "force-removing" in log_text, (
        f"expected retry log message, got: {log_text!r}"
    )
    # The conflicting worktree was removed.
    assert not conflict_wt.exists(), "conflicting worktree was not cleaned up"


def test_cleanup_worktree_prunes_when_dir_gone(tmp_path: Path) -> None:
    """#460 Part 3 — synchronous teardown: _cleanup_worktree calls git worktree
    prune even when the physical directory was already removed, so the admin
    entry doesn't block the next setup on the same branch."""
    repo = _init_local_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(repo)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )

    # Create a worktree manually and then delete its directory without
    # going through git (simulating a crash before git worktree remove).
    wt = tmp_path / "wt-to-crash"
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-77-prune", str(wt), "HEAD"],
        cwd=str(repo), check=True, capture_output=True,
    )
    import shutil as _shutil
    _shutil.rmtree(wt)
    assert not wt.exists()

    # Before cleanup: git still thinks issue-77-prune is checked out.
    out_before = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    # (admin entry may still be stale until prune runs)

    # Build a fake assignment pointing at the deleted directory.
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo),
        issue_number=77, issue_title="prune", briefing="b",
        branch="main",
    )
    fake_assignment = AgentAssignment(
        id=uuid.uuid4().hex[:12],
        spec=spec,
        status=DONE,
        worktree_path=str(wt),
    )

    # _cleanup_worktree should prune even though wt doesn't exist.
    server._cleanup_worktree(fake_assignment)

    # After cleanup, the branch should be free (admin entry pruned).
    out_after = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    entries = _parse_worktree_porcelain(out_after)
    branches = {e.get("branch") for e in entries}
    assert "issue-77-prune" not in branches, (
        f"branch still registered after _cleanup_worktree on missing dir: {entries}"
    )


def test_clean_worktrees_bypasses_cooldown_for_pending_branch(tmp_path: Path) -> None:
    """#460 Part 3: clean_worktrees ignores the 300 s cooldown when a PENDING
    assignment needs the same branch as a terminal worktree."""
    repo = _init_local_repo(tmp_path / "repo")
    state_dir = tmp_path / "state"
    server = AgentServer(
        machine_name="t", repos=["api"],
        repo_paths={"api": str(repo)},
        state_dir=state_dir,
        worker_command=lambda spec: ["/bin/true"],
    )

    # Manually plant a terminal assignment + its worktree directory.
    wt_dir = state_dir / "worktrees"
    wt_dir.mkdir(parents=True, exist_ok=True)

    term_id = uuid.uuid4().hex[:12]
    term_wt = wt_dir / term_id
    subprocess.run(
        ["git", "worktree", "add", "-b", "issue-88-overlap", str(term_wt), "HEAD"],
        cwd=str(repo), check=True, capture_output=True,
    )

    # Construct a DONE assignment that just finished (finished_at = now, within cooldown).
    term_spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo),
        issue_number=88, issue_title="overlap", briefing="b",
        branch="main",
    )
    import time as _time
    term_assignment = AgentAssignment(
        id=term_id,
        spec=term_spec,
        status=DONE,
        finished_at=_time.time(),  # just finished — normally within 300 s cooldown
        worktree_path=str(term_wt),
    )

    # Also plant a PENDING assignment needing the same branch.
    pend_id = uuid.uuid4().hex[:12]
    pend_spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo),
        issue_number=88, issue_title="overlap", briefing="b",
        branch="main",
    )
    pend_assignment = AgentAssignment(
        id=pend_id,
        spec=pend_spec,
        status=PENDING,
    )

    with server._lock:
        server._assignments[term_id] = term_assignment
        server._assignments[pend_id] = pend_assignment

    # clean_worktrees should bypass the cooldown and clean the terminal worktree.
    result = server.clean_worktrees(recent_secs=300.0)
    assert result["cleaned"] >= 1, (
        f"expected terminal worktree cleaned despite cooldown, got: {result}"
    )
    assert not term_wt.exists(), (
        "terminal worktree directory was not removed despite PENDING branch need"
    )
