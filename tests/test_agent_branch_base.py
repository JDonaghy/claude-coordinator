"""Regression tests for #255 — worker branches must start at the origin's
default-branch SHA, never at a local-only ref.

The pre-fix behavior silently fell back to `git rev-parse <branch>` (local)
when `origin/<branch>` couldn't be resolved.  Combined with the dispatch
path defaulting to a hardcoded "main" instead of `repo.default_branch`,
that meant a worker on a machine with unpushed local commits on `develop`
would create a feature branch that included those unpushed commits — the
exact symptom from quadraui#233.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coord.agent import DONE, FAILED, AgentServer, AssignmentSpec


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
    assert final.status == DONE, f"assignment failed: {final.error}"

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
    assert final.status == DONE, f"assignment failed: {final.error}"
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
    assert final.status == DONE
    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "ahead of origin/develop" in log_text, (
        f"expected unpushed-commits warning in log, got:\n{log_text}"
    )
    server.shutdown()
