"""Tests for the reap-honesty / zero-commit advisory state (#448).

A worker that exits cleanly (exit_code==0) but pushes 0 commits must be
recorded as ADVISORY rather than DONE.  This distinguishes "already
implemented — nothing to do" from "wrote code, tests pass, branch pushed".

A hard FAILED would trigger auto_reassign loops on legitimate no-op reports.
A clean DONE would feed the merge queue with an empty branch.  ADVISORY is
the safe middle ground: the operator decides whether to re-dispatch or close.
"""

from __future__ import annotations

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
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone whose `origin` is a local bare repo.

    Returns (clone, origin).  The initial commit is pushed so origin/main is
    populated — the commits-ahead check can then distinguish 0 from non-zero.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "remote", "add", "origin", str(origin))
    (clone / "README").write_text("init\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "push", "-u", "origin", "main")
    return clone, origin


@pytest.fixture
def repo_local_only(tmp_path: Path) -> Path:
    """A local-only repo with no remote — mirrors the test-fixture pattern."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("init\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "initial")
    return repo


# ── 0-commit exits → ADVISORY ─────────────────────────────────────────────────


def test_zero_commit_clean_exit_is_advisory_with_remote(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """exit_code==0 + 0 commits ahead of origin/main → ADVISORY, not DONE.

    This is the core regression guard for #448.
    """
    clone, _origin = repo_with_remote
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(clone)},
        state_dir=tmp_path / "state",
        # Worker exits cleanly but makes no git commits.
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(clone),
        issue_number=1,
        issue_title="already implemented",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == ADVISORY, (
        f"expected ADVISORY for 0-commit clean exit, got {final.status!r}"
    )
    assert final.exit_code == 0, "exit_code must still be 0 for advisory"
    assert final.zero_commit_reason is not None, "reason string must be set"
    assert "0 commits" in final.zero_commit_reason
    server.shutdown()


def test_zero_commit_clean_exit_is_advisory_local_fallback(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The fallback to <base>..HEAD (no origin) also triggers ADVISORY on 0 commits.

    Local-only repos are common in test fixtures and airgapped machines.  The
    commits-ahead check must work without a remote.
    """
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2,
        issue_title="noop work",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == ADVISORY, (
        f"expected ADVISORY via local fallback, got {final.status!r}"
    )
    assert final.zero_commit_reason is not None
    server.shutdown()


def test_advisory_reason_appears_in_log(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The advisory diagnosis is written to the assignment log so operators
    can find it in `coord log <id>` without querying the agent."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=3,
        issue_title="noop",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == ADVISORY
    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "advisory" in log_text.lower(), (
        f"expected 'advisory' in log, got:\n{log_text}"
    )
    server.shutdown()


def test_advisory_survives_persist_load(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """ADVISORY status and zero_commit_reason round-trip through the agent
    state JSON (persist → load) so a restarted agent still shows the advisory."""
    from coord.agent import AgentAssignment  # noqa: PLC0415

    a = AgentAssignment(
        id="advisory-001",
        spec=AssignmentSpec(
            repo_name="api",
            repo_path="/tmp",
            issue_number=1,
            issue_title="t",
            briefing="b",
        ),
        status=ADVISORY,
        zero_commit_reason="worker exited cleanly but pushed 0 commits",
        exit_code=0,
        finished_at=1234567890.0,
    )
    d = a.to_dict()
    assert d["status"] == ADVISORY
    assert d["zero_commit_reason"] == "worker exited cleanly but pushed 0 commits"

    # Reconstruct (mirrors _load_state logic)
    spec_data = d.pop("spec")
    spec = AssignmentSpec(**spec_data)
    a2 = AgentAssignment(spec=spec, **d)
    assert a2.status == ADVISORY
    assert a2.zero_commit_reason == "worker exited cleanly but pushed 0 commits"


# ── non-zero commits → DONE ───────────────────────────────────────────────────


def test_nonzero_commit_clean_exit_is_done_with_remote(
    tmp_path: Path, repo_with_remote: tuple[Path, Path]
) -> None:
    """A worker that exits cleanly AND pushes ≥1 commit must still be DONE.

    This is the primary regression guard: the advisory path must not affect
    real work that made actual code changes.
    """
    clone, _origin = repo_with_remote

    # Worker script: add a file, commit it, then exit.
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
        issue_number=4,
        issue_title="real work",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE, (
        f"expected DONE for non-zero commit exit, got {final.status!r}"
    )
    assert final.exit_code == 0
    assert final.zero_commit_reason is None, (
        "zero_commit_reason must be None when commits exist"
    )
    server.shutdown()


def test_nonzero_commit_clean_exit_is_done_local_fallback(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """Non-zero commits on a local-only repo → DONE via local branch fallback."""
    worker_sh = (
        "git config user.email w@w.com && "
        "git config user.name Worker && "
        "echo fix > fix.txt && "
        "git add fix.txt && "
        "git commit -m 'local fix'"
    )
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", worker_sh],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=5,
        issue_title="local fix",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=15)

    assert final.status == DONE, (
        f"expected DONE for non-zero local commit, got {final.status!r}"
    )
    assert final.zero_commit_reason is None
    server.shutdown()


# ── non-zero exit always stays FAILED regardless of commits ───────────────────


def test_nonzero_exit_is_failed_regardless_of_commits(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """A worker that exits with a non-zero code is FAILED — never ADVISORY.

    Even if it somehow pushed commits before crashing, exit_code != 0 → FAILED.
    """
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "exit 1"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=6,
        issue_title="failing worker",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.zero_commit_reason is None
    server.shutdown()


# ── ADVISORY is terminal — counted as completed, not active ──────────────────


def test_advisory_counted_as_completed_in_health(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """health() must count ADVISORY assignments as completed, not active."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/true"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=7,
        issue_title="noop",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)
    assert final.status == ADVISORY

    h = server.health()
    assert h["active"] == 0, "ADVISORY must not count as active"
    assert h["completed"] >= 1, "ADVISORY must count as completed"
    server.shutdown()
