"""Tests for usage-limit-kill detection at reap time (#1461).

A worker (claude -p) that hits the account's Max/Pro *session* usage limit
mid-flight prints a terminal line like:

    "You've hit your session limit · resets 8:30pm (America/Chicago)"

and exits, with no structured event marking what happened. Before this fix,
`_reap` recorded a bare FAILED/ADVISORY with `usage_limit_reason=None` —
indistinguishable from a real defect. This regression-guards the reap-time
detection wired in `AgentServer._reap` (coord/agent.py).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coord.agent import (
    ADVISORY,
    FAILED,
    AgentServer,
    AssignmentSpec,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_local_only(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("init\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "initial")
    return repo


_KILL_MESSAGE = "You’ve hit your session limit · resets 8:30pm (America/Chicago)"


def test_usage_limit_kill_detected_on_nonzero_exit(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The #1454 shape from the issue: worker dies non-zero, transcript ends
    with the kill message, no terminating result event."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            f"printf '%s\\n' {_shell_quote(_KILL_MESSAGE)}; exit 1",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1454,
        issue_title="killed mid-flight",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.usage_limit_reason is not None
    assert final.usage_limit_reason == "usage limit — resets 8:30pm (America/Chicago)"
    server.shutdown()


def test_usage_limit_kill_detected_on_clean_exit_zero_commits(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The #1456 shape from the issue: the CLI ends the turn gracefully (exit
    0) before committing anything — lands ADVISORY, but must still carry the
    usage-limit reason rather than a bare '0 commits pushed'."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            f"printf '%s\\n' {_shell_quote(_KILL_MESSAGE)}",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1456,
        issue_title="killed mid-flight, clean exit",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == ADVISORY
    assert final.usage_limit_reason is not None
    assert final.usage_limit_reason == "usage limit — resets 8:30pm (America/Chicago)"
    server.shutdown()


def test_normal_failure_has_no_usage_limit_reason(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """A real defect (no kill message anywhere) must not get a
    usage_limit_reason — the field stays None so it's never confused with a
    genuine limit kill."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo boom >&2; exit 1"],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2,
        issue_title="a real bug",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.usage_limit_reason is None
    server.shutdown()


def test_usage_limit_reason_appears_in_log(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The detection is written to the assignment log so operators can find
    it in `coord log <id>` without querying the agent."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            f"printf '%s\\n' {_shell_quote(_KILL_MESSAGE)}; exit 1",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=3,
        issue_title="killed mid-flight",
        briefing="b",
        branch="main",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "usage-limit kill detected" in log_text
    server.shutdown()


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
