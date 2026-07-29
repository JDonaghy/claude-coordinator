"""Tests for terminal-API-error classification at reap time (#1584).

Before this fix, `is_error: true` on a worker's terminal `result` event was
read in exactly one place (`format_important_event`, `coord/worker_events.py`)
and only to build a `coord watch` display string — nothing mapped it to
assignment status. A worker that died on a transient API error (a 529, a
500, a network drop) at turn 1 was recorded `status='done'`, indistinguishable
from a real success, silently ending an unattended drive.

This regression-guards the reap-time classification wired in
`AgentServer._reap` (coord/agent.py) and its `WorkerSummary.is_error` /
`format_api_error_reason` plumbing (coord/worker_events.py).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coord.agent import (
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


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# The #1563 evidence, verbatim shape.
_ERROR_RESULT_EVENT = {
    "type": "result",
    "is_error": True,
    "num_turns": 1,
    "stop_reason": "stop_sequence",
    "terminal_reason": "api_error",
    "api_error_status": 529,
    "result": (
        "API Error: 529 Overloaded. This is a server-side issue, usually "
        "temporary…"
    ),
    "total_cost_usd": 0.026247,
}

_INIT_EVENT = {
    "type": "system",
    "subtype": "init",
    "session_id": "sess-1",
    "model": "claude-haiku-4-5",
}

_OK_RESULT_EVENT = {
    "type": "result",
    "total_cost_usd": 0.05,
    "stop_reason": "end_turn",
    "num_turns": 2,
}


def _printf_lines(*events: dict) -> str:
    """Shell snippet that prints each event as one NDJSON line."""
    return "; ".join(
        f"printf '%s\\n' {_shell_quote(json.dumps(e))}" for e in events
    )


def test_terminal_api_error_recorded_failed(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The #1563/#1584 core regression: a terminal `result` event carrying
    `is_error: true` must record the assignment FAILED — with the API status
    surfaced in `api_error_reason` — even though the wrapper process itself
    exits 0."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            _printf_lines(_INIT_EVENT, _ERROR_RESULT_EVENT),
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1563,
        issue_title="review that never ran",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED, (
        f"a terminal is_error:true result must never be recorded `done` — "
        f"got {final.status!r}"
    )
    assert final.api_error_reason == "529 Overloaded"
    assert final.usage_limit_reason is None
    server.shutdown()


def test_terminal_api_error_appears_in_log(
    tmp_path: Path, repo_local_only: Path
) -> None:
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            _printf_lines(_INIT_EVENT, _ERROR_RESULT_EVENT),
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=1,
        issue_title="review that never ran",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.log_path is not None
    log_text = Path(final.log_path).read_text()
    assert "terminal API error detected" in log_text
    assert "529 Overloaded" in log_text
    server.shutdown()


def test_normal_successful_result_still_done(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """Regression: a normal successful `result` (`is_error` absent) must
    still be recorded DONE, with `api_error_reason` left None."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            _printf_lines(_INIT_EVENT, _OK_RESULT_EVENT),
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=2,
        issue_title="a clean review",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == DONE
    assert final.api_error_reason is None
    server.shutdown()


def test_transient_error_then_internal_retry_success_still_done(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The pinned regression risk: a worker that hits a transient API error,
    retries internally, and finishes successfully must NOT be marked failed.
    Its transcript has an earlier `result` line with `is_error: true`
    followed by a later, final one without it — only the LAST result event
    may decide the outcome."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            _printf_lines(_INIT_EVENT, _ERROR_RESULT_EVENT, _OK_RESULT_EVENT),
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=3,
        issue_title="retried internally then succeeded",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == DONE, (
        "a worker whose FINAL result event succeeded must not be failed "
        f"just because an earlier one carried is_error — got {final.status!r}"
    )
    assert final.api_error_reason is None
    server.shutdown()


def test_terminal_api_error_with_nonzero_exit(
    tmp_path: Path, repo_local_only: Path
) -> None:
    """The classification must not depend on the wrapper's own exit code —
    a non-zero exit alongside is_error must also land on FAILED with the
    reason recorded (not just a bare exit-code FAILED)."""
    server = AgentServer(
        machine_name="t",
        repos=["api"],
        repo_paths={"api": str(repo_local_only)},
        state_dir=tmp_path / "state",
        worker_command=lambda spec: [
            "/bin/sh", "-c",
            f"{_printf_lines(_INIT_EVENT, _ERROR_RESULT_EVENT)}; exit 1",
        ],
    )
    spec = AssignmentSpec(
        repo_name="api",
        repo_path=str(repo_local_only),
        issue_number=4,
        issue_title="killed and exited non-zero",
        briefing="b",
        branch="main",
        type="review",
    )
    a = server.assign(spec)
    final = server.wait_for(a.id, timeout=10)

    assert final.status == FAILED
    assert final.api_error_reason == "529 Overloaded"
    server.shutdown()
