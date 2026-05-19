"""Smoke test orchestration — dispatch automated build/test validation.

When `smoke_tests.auto_dispatch` is enabled, completion of a "work"
assignment triggers a smoke test on an idle machine. The smoke test
checks out the worker's branch, runs build_command and test_command
from coordinator.yml, and reports pass/fail.

Follows the same dispatch pattern as coord/review.py.
"""

from __future__ import annotations

import time
import uuid

import httpx

from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.models import Assignment, Board, Machine


SMOKE_SYSTEM_PROMPT = """\
You are a smoke test runner dispatched by the coordinator.

Your job:
1. Check out the branch specified in your briefing
2. Run the build command
3. Run the test command
4. Report the results

Do NOT modify any code. Do NOT run gh commands. Only run the build and \
test commands specified, report whether they pass or fail, and include \
any error output if they fail.\
"""


def pick_smoke_machine(
    worker_machine: str,
    repo_name: str,
    board: Board,
    config: Config,
) -> Machine | None:
    """Choose an idle machine that can build this repo.

    Prefers a machine different from the worker. Falls back to the
    worker's machine if no other is available.
    """
    busy = {a.machine_name for a in board.active if a.status == "running"}
    candidates = [
        m for m in config.machines
        if m.can_work_on(repo_name)
        and m.repo_path(repo_name) is not None
        and m.name not in busy
    ]
    for m in candidates:
        if m.name != worker_machine:
            return m
    for m in candidates:
        if m.name == worker_machine:
            return m
    return None


def build_smoke_briefing(
    *,
    repo_name: str,
    branch: str,
    issue_number: int,
    issue_title: str,
    build_command: str | None,
    test_command: str | None,
) -> str:
    lines = [
        f"## Smoke test for #{issue_number}: {issue_title}",
        f"",
        f"Branch: `{branch}`",
        f"Repo: {repo_name}",
        f"",
        f"### Steps",
        f"1. `git fetch origin && git checkout {branch}`",
    ]
    step = 2
    if build_command:
        lines.append(f"{step}. Run build: `{build_command}`")
        step += 1
    if test_command:
        lines.append(f"{step}. Run tests: `{test_command}`")
        step += 1
    lines.append(f"{step}. Report results — did everything pass?")
    if not build_command and not test_command:
        lines.append("")
        lines.append("No build or test commands configured. Just verify the branch checks out cleanly.")
    return "\n".join(lines)


def dispatch_smoke_test(
    completed: Assignment,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    now: float | None = None,
) -> Assignment | None:
    """Dispatch a smoke test for a completed work assignment.

    Returns the new smoke test Assignment, or None if it can't be dispatched.
    """
    if not config.smoke_tests.enabled or not config.smoke_tests.auto_dispatch:
        return None
    if getattr(completed, "type", "work") != "work":
        return None
    if completed.status != "done":
        return None
    if not completed.branch:
        return None

    repo = config.repo(completed.repo_name)
    if repo is None:
        return None

    machine = pick_smoke_machine(
        completed.machine_name, completed.repo_name, board, config,
    )
    if machine is None:
        return None

    repo_path = machine.repo_path(completed.repo_name)
    if repo_path is None:
        return None

    briefing = build_smoke_briefing(
        repo_name=completed.repo_name,
        branch=completed.branch,
        issue_number=completed.issue_number,
        issue_title=completed.issue_title,
        build_command=repo.build_command,
        test_command=repo.test_command,
    )

    payload = {
        "repo_name": completed.repo_name,
        "repo_path": repo_path,
        "issue_number": completed.issue_number,
        "issue_title": f"[smoke] {completed.issue_title}",
        "briefing": briefing,
        "files_allowed": [],
        "files_forbidden": [],
        "pull_repos": [],
        "type": "smoke_test",
        "system_prompt": SMOKE_SYSTEM_PROMPT,
    }

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    client = http_client or httpx
    try:
        resp = client.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    smoke_assignment = Assignment(
        machine_name=machine.name,
        repo_name=completed.repo_name,
        issue_number=completed.issue_number,
        issue_title=f"[smoke] {completed.issue_title}",
        briefing=briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        branch=completed.branch,
        dispatched_at=now if now is not None else time.time(),
        type="smoke_test",
    )
    board.active.append(smoke_assignment)
    return smoke_assignment
