"""Tests for the reap-time push-failure signal (#1797).

#1797: every ephemeral Azure worker's git credential helper had its
`$GH_TOKEN` expanded (to empty) at image-bake time instead of at
push-invocation time, so `git push` failed with a GitHub auth error on
every single worker, every single time. The failure was recorded to the
worker's log and then dropped on the floor: nothing downstream ever saw
it, so a worker with real local commits that could not reach origin was
recorded exactly like a clean DONE, and a worker with nothing to push in
the first place was recorded exactly like an auth break — both
indistinguishable from each other via `assignment.status` alone.

These tests pin down that a reap-time push failure is its own outcome:
FAILED, with `push_failure_reason` carrying the raw git error, distinct
from the unrelated `zero_commit_reason` ADVISORY path (#448) covered by
test_agent_zero_commit.py.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from coord.agent import (
    ADVISORY,
    DONE,
    FAILED,
    AgentServer,
    AssignmentSpec,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_with_rejecting_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone whose `origin` rejects every push with a git
    auth-style error, via a `pre-receive` hook — the same shape of failure
    #1797 hit in production (`remote: Invalid username or token. Password
    authentication is not supported for Git operations.`), reproduced
    deterministically with no network and no real credentials involved.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        "echo 'remote: Invalid username or token.' >&2\n"
        "echo 'remote: Password authentication is not supported for "
        "Git operations.' >&2\n"
        "exit 1\n"
    )
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "remote", "add", "origin", str(origin))
    (clone / "README").write_text("init\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")

    # Temporarily disable the hook to land the initial commit so origin/main
    # exists for the commits-ahead check, then re-enable it for the real test.
    hook.chmod(hook.stat().st_mode & ~stat.S_IEXEC)
    _git(clone, "push", "-u", "origin", "main")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)

    return clone, origin


def test_push_failure_with_real_commits_is_failed_not_done(
    tmp_path: Path, repo_with_rejecting_remote: tuple[Path, Path]
) -> None:
    """A worker that commits real work but can't push (auth broken) must be
    FAILED, not DONE — a silent DONE would mean the work is recorded as
    landed when it never left the worktree."""
    clone, _origin = repo_with_rejecting_remote

    worker_sh = (
        "git config user.email w@w.com && "
        "git config user.name Worker && "
        "echo change > change.txt && "
        "git add change.txt && "
        "git commit -m 'real work'"
    )
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", worker_sh],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=1,
        issue_title="real work, broken credential",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == FAILED, (
        f"expected FAILED when the reap-time push fails, got {final.status!r}"
    )
    assert final.exit_code == 0, "the worker itself exited cleanly"
    assert final.push_failure_reason is not None, "reason string must be set"
    assert "Invalid username or token" in final.push_failure_reason
    # Distinct signal from #448's zero-commit advisory — must stay unset.
    assert final.zero_commit_reason is None, (
        "push failure must not be conflated with the zero-commit advisory path"
    )
    server.shutdown()


def test_push_failure_with_zero_commits_is_failed_not_advisory(
    tmp_path: Path, repo_with_rejecting_remote: tuple[Path, Path]
) -> None:
    """The #1797 evidence case: a worker with 0 local commits AND a broken
    push. Before this fix, the zero-commit ADVISORY ("worker exited cleanly
    but pushed 0 commits") masked the auth break entirely. The push failure
    must win and be recorded as FAILED with a push_failure_reason — never
    silently downgraded to the generic "nothing to do" advisory."""
    clone, _origin = repo_with_rejecting_remote
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=2,
        issue_title="already implemented, broken credential",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == FAILED, (
        "push failure must take priority over the zero-commit advisory, got "
        f"{final.status!r}"
    )
    assert final.status != ADVISORY
    assert final.push_failure_reason is not None
    assert "Invalid username or token" in final.push_failure_reason
    server.shutdown()


def test_successful_push_leaves_push_failure_reason_none(
    tmp_path: Path, repo_with_rejecting_remote: tuple[Path, Path]
) -> None:
    """Sanity/regression check: a healthy push (hook disabled) must NOT set
    push_failure_reason, and status must be the ordinary DONE."""
    clone, origin = repo_with_rejecting_remote
    hook = origin / "hooks" / "pre-receive"
    hook.chmod(hook.stat().st_mode & ~stat.S_IEXEC)  # disable the rejection

    worker_sh = (
        "git config user.email w@w.com && "
        "git config user.name Worker && "
        "echo change > change.txt && "
        "git add change.txt && "
        "git commit -m 'real work'"
    )
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", worker_sh],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=3,
        issue_title="real work, healthy credential",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE
    assert final.push_failure_reason is None
    server.shutdown()
