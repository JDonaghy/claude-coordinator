"""Smoke-test orchestration — auto-queue validation on a capable machine.

When a worker finishes, the work often needs validation hardware the worker
didn't have. Example: a GTK key-routing fix built on a no-GTK server needs a
machine with GTK to actually verify the popup works. This module:

1. Reads the worker's diff (which files changed).
2. Looks at `smoke_tests.capability_rules` — each rule maps a file-path
   prefix to a set of required machine capabilities.
3. Picks a machine that has all required capabilities, preferring one
   different from the worker.
4. Dispatches a `type="smoke"` assignment with a briefing that tells
   `claude -p` to fetch the branch, run the smoke command, and report
   pass/fail through its exit code.

Public entry points:

- `match_rules(touched_files, rules)`  — pure: returns the union of required
  capabilities for any rule whose `files` prefix matches a touched file.
- `pick_smoke_machine(required_caps, worker_machine, board, config)` — picks
  a capable machine (different from the worker if possible).
- `dispatch_smoke(completed, board, config, ...)` — the full path; called
  from reconcile when a work assignment transitions to done.

Why a separate module from `coord/review.py`: smoke tests target machine
capabilities (GTK/terminal/CUDA), not session independence. The selection
algorithm is different — for reviews we want a *different* machine for
independence; for smoke we want a *capable* machine for hardware, and
"different" is only a tie-breaker.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import httpx

from coord import github_ops
from coord.config import Config, SmokeRule, SmokeTestsConfig
from coord.dispatch import AGENT_PORT
from coord.models import Assignment, Board, Machine


SMOKE_SYSTEM_PROMPT = """\
You are a smoke-test runner dispatched by the coordinator. \
Your only job: pull the branch, run the smoke command, report pass/fail.

Rules:
- Do NOT modify code. Do NOT push commits. You only validate.
- Do NOT run `gh` commands. The coordinator owns GitHub interactions.
- You MAY run git, build commands, and test commands.
- Exit code is what the coordinator reads — exit 0 on pass, non-zero on \
fail. Print a final line `SMOKE: pass` or `SMOKE: fail <one-line reason>` \
before exiting so logs are readable.

Steps:
1. `git fetch origin && git checkout <branch>` (the branch is in your briefing).
2. Run the smoke command from the briefing. Capture stdout/stderr.
3. If it exits 0 → print `SMOKE: pass` and exit 0.
4. If it fails → print `SMOKE: fail <short reason>` and exit non-zero.
"""


# ── Rule matching ───────────────────────────────────────────────────────────


def match_rules(touched_files: list[str], rules: list[SmokeRule]) -> list[str]:
    """Return the union of `requires` for any rule that any touched file hits.

    Matching is path-prefix: a rule with `files=["src/gtk/"]` matches
    `src/gtk/foo.c` but not `src/cli.py`. A rule with `files=["src/gtk"]`
    (no slash) catches both `src/gtk/foo.c` and `src/gtk_helpers.c` — use
    the trailing slash form to be strict.

    Returns capabilities in deterministic order (first-seen across rules).
    """
    seen: dict[str, None] = {}
    for path in touched_files:
        for rule in rules:
            if not any(path.startswith(pattern) for pattern in rule.files):
                continue
            for cap in rule.requires:
                seen.setdefault(cap, None)
    return list(seen.keys())


# ── Machine selection ───────────────────────────────────────────────────────


@dataclass
class SmokeMachineChoice:
    machine: Machine
    is_worker: bool
    rationale: str


def pick_smoke_machine(
    required_caps: list[str],
    repo_name: str,
    worker_machine_name: str,
    board: Board,
    config: Config,
) -> SmokeMachineChoice | None:
    """Pick a machine with all `required_caps` for `repo_name`.

    Preference order:
    1. Idle, capable, different from worker
    2. Busy, capable, different from worker (smoke will queue)
    3. Worker machine itself, if capable
    4. None — no machine can validate this change

    Returns None when capabilities can't be matched.
    """
    candidates = [
        m for m in config.machines
        if m.can_work_on(repo_name)
        and all(cap in m.capabilities for cap in required_caps)
    ]
    if not candidates:
        return None

    busy = {a.machine_name for a in board.active if a.status in ("pending", "running")}

    idle_different = [
        m for m in candidates
        if m.name != worker_machine_name and m.name not in busy
    ]
    if idle_different:
        return SmokeMachineChoice(
            machine=idle_different[0],
            is_worker=False,
            rationale=(
                f"chose {idle_different[0].name} — idle and has {required_caps} "
                f"(worker was {worker_machine_name})"
            ),
        )

    busy_different = [
        m for m in candidates if m.name != worker_machine_name
    ]
    if busy_different:
        return SmokeMachineChoice(
            machine=busy_different[0],
            is_worker=False,
            rationale=(
                f"chose {busy_different[0].name} — capable but busy; smoke will queue"
            ),
        )

    same = next((m for m in candidates if m.name == worker_machine_name), None)
    if same is not None:
        return SmokeMachineChoice(
            machine=same,
            is_worker=True,
            rationale=(
                f"only the worker machine ({worker_machine_name}) has {required_caps}; "
                "smoke runs on the same machine"
            ),
        )
    return None


# ── Briefing ────────────────────────────────────────────────────────────────


def build_smoke_briefing(
    *,
    repo_github: str,
    repo_name: str,
    branch: str,
    issue_number: int,
    issue_title: str,
    smoke_command: str,
    required_caps: list[str],
    timeout_seconds: int,
    is_worker: bool,
) -> str:
    lines: list[str] = []
    lines.append(f"# Smoke test: {repo_github} branch `{branch}`")
    lines.append("")
    lines.append(
        f"Validate the worker's fix for issue #{issue_number}: {issue_title}"
    )
    lines.append("")
    lines.append("## Context")
    lines.append(f"- Repo: {repo_github} (local name: {repo_name})")
    lines.append(f"- Branch: {branch}")
    if required_caps:
        lines.append(f"- Required capabilities: {', '.join(required_caps)}")
    if is_worker:
        lines.append(
            "- NOTE: only this machine has the required capabilities, so the "
            "smoke test is running on the same machine that built the change. "
            "Test the *built artifact*, not the source — the build step here is "
            "your verification that the change compiles."
        )
    lines.append(f"- Timeout: {timeout_seconds}s")
    lines.append("")
    lines.append("## What to do")
    lines.append("")
    lines.append("```bash")
    lines.append("git fetch origin")
    lines.append(f"git checkout {branch}")
    lines.append("git pull --ff-only origin " + branch)
    lines.append(smoke_command)
    lines.append("```")
    lines.append("")
    lines.append(
        "Report `SMOKE: pass` on exit 0, or "
        "`SMOKE: fail <one-line reason>` on non-zero. The coordinator reads "
        "the final exit code."
    )
    return "\n".join(lines)


# ── Diff lookup (which files did the worker change?) ────────────────────────


def _fetch_touched_files(repo_github: str, branch: str) -> list[str]:
    """Return the list of files changed on `branch` vs the base branch.

    Uses `gh pr view --json files` so the lookup works without a local
    checkout on the coordinator. Returns an empty list on lookup failure —
    the caller treats that as "no rules matched" and skips smoke.
    """
    pr = None
    try:
        pr = github_ops.find_pr_for_branch(repo_github, branch)
    except RuntimeError:
        pr = None
    if pr is None:
        return []
    try:
        raw = github_ops._gh(
            "pr", "view", str(pr["number"]),
            "--repo", repo_github,
            "--json", "files",
        )
    except RuntimeError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    files = data.get("files", []) or []
    return [f.get("path", "") for f in files if f.get("path")]


# ── Dispatch ────────────────────────────────────────────────────────────────


PRLookup = Callable[..., dict | None]
DiffLookup = Callable[[str, str], list[str]]


def dispatch_smoke(
    completed: Assignment,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    diff_lookup: DiffLookup = _fetch_touched_files,
    now: float | None = None,
) -> Assignment | None:
    """Queue a smoke test for a completed work assignment.

    Returns the new smoke `Assignment`, or None when no smoke is needed
    (no rules matched, no capable machine, smoke disabled, etc.). The
    caller is responsible for persisting the board.
    """
    smoke_cfg = getattr(config, "smoke_tests", SmokeTestsConfig())
    if not smoke_cfg.auto_queue:
        return None
    if completed.type != "work":
        return None
    if completed.status != "done":
        return None
    if not completed.branch:
        return None

    # Dedupe: don't fire a second smoke if one's already in flight.
    from coord.claim import has_active_followup

    if has_active_followup(
        board, of_assignment_id=completed.assignment_id, assignment_type="smoke"
    ):
        return None

    repo = config.repo(completed.repo_name)
    if repo is None:
        return None

    touched = diff_lookup(repo.github, completed.branch)
    required_caps = match_rules(touched, smoke_cfg.capability_rules)
    if not required_caps:
        # No rule matched — either the diff doesn't need a specialized machine
        # or rules aren't configured for this repo. Either way, skip silently.
        return None

    choice = pick_smoke_machine(
        required_caps, completed.repo_name, completed.machine_name, board, config
    )
    if choice is None:
        return None

    repo_path = choice.machine.repo_path(completed.repo_name)
    if repo_path is None:
        return None

    smoke_command = (
        smoke_cfg.default_command
        or repo.test_command
        or "echo 'no smoke command configured' && false"
    )

    briefing = build_smoke_briefing(
        repo_github=repo.github,
        repo_name=repo.name,
        branch=completed.branch,
        issue_number=completed.issue_number,
        issue_title=completed.issue_title,
        smoke_command=smoke_command,
        required_caps=required_caps,
        timeout_seconds=smoke_cfg.timeout_seconds,
        is_worker=choice.is_worker,
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
        "type": "smoke",
        "system_prompt": SMOKE_SYSTEM_PROMPT,
        "review_target": completed.branch,
    }

    url = f"http://{choice.machine.host}:{AGENT_PORT}/assign"
    client = http_client or httpx
    try:
        resp = client.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    smoke_assignment = Assignment(
        machine_name=choice.machine.name,
        repo_name=completed.repo_name,
        issue_number=completed.issue_number,
        issue_title=f"[smoke] {completed.issue_title}",
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        branch=completed.branch,
        pr_url=completed.pr_url,
        dispatched_at=now if now is not None else time.time(),
        type="smoke",
        review_target=completed.branch,
        review_of_assignment_id=completed.assignment_id,
    )
    board.active.append(smoke_assignment)
    return smoke_assignment
