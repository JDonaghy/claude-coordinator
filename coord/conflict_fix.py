"""Auto-dispatch a worker to rebase a merge-conflicted branch (#241).

When ``coord merge`` fails with a mechanical rebase conflict (classified by
:func:`coord.merge_queue.classify_conflict` as ``"rebaseable"``), the
coordinator queues a ``type="conflict-fix"`` assignment that:

1. Pulls the latest target branch.
2. Rebases the worker's branch on top of it.
3. Resolves obvious additive merges (non-overlapping struct fields, list
   entries, imports).
4. Runs the project's test command.
5. ``git push --force-with-lease`` to the same branch.

On success, the coordinator re-enqueues the original merge entry so
``coord merge`` retries.  On failure, the merge entry is marked
:data:`coord.merge_queue.HUMAN_REQUIRED` and surfaced in the TUI.

Why a separate module: same reason :mod:`coord.review` lives apart from
``coord.dispatch`` — conflict-fix is triggered by a merge_queue event, not
by a planner proposal, so it shares little with the work-dispatch shape.
"""

from __future__ import annotations

import time
import uuid

import httpx

from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.merge_queue import QueuedMerge
from coord.models import Assignment, Board, Machine


CONFLICT_FIX_SYSTEM_PROMPT = """\
You are a Claude Code conflict-fix worker. The merge of a worker's branch \
into the project's target branch failed because the branch is out of date \
or has a conflict. Your job is to rebase the branch and push it back.

Rules:
- Do NOT run gh commands. The coordinator handles the merge retry.
- Stay on the worker's branch — do NOT push to main / develop / target.
- Use git push --force-with-lease (NOT --force).
- If conflicts are mechanical (non-overlapping struct fields, list entries, \
imports, separate functions), resolve them additively — keep both sides.
- If conflicts are SEMANTIC (same function modified two ways, contradictory \
logic), DO NOT GUESS. Post a comment on the issue describing the conflict \
regions and exit non-zero. The user will resolve manually.

Progress reporting:
- After each significant step (rebase started, conflicts resolved, tests \
passed, pushed), output:
  STATUS: [what you just did] → [what you're about to do] → [confidence]
- If you've tried and failed, output:
  STUCK: [what you tried] [why it failed]
  Then stop and wait for guidance.\
"""


def build_conflict_fix_briefing(
    *,
    entry: QueuedMerge,
    repo_path: str,
    test_command: str | None,
) -> str:
    """Assemble the conflict-fix worker's briefing. Pure function — testable."""
    test_cmd = test_command or "echo '(no test command configured)'"
    lines: list[str] = [
        f"# Conflict fix: {entry.repo_github} branch `{entry.branch}`",
        "",
        f"The merge of `{entry.branch}` → `{entry.target_branch}` failed.",
        f"Reason: {entry.error or 'unknown conflict'}",
        "",
        f"Issue: #{entry.issue_number} — {entry.issue_title}",
        "",
        "## Steps",
        "",
        f"1. `cd {repo_path}`",
        "2. `git fetch origin`",
        f"3. `git checkout {entry.branch}`",
        f"4. `git pull --rebase origin {entry.target_branch}`",
        "5. Resolve any conflict markers.  Prefer additive merges; preserve",
        "   both sides when the conflict is in non-overlapping struct fields,",
        "   list entries, imports, or separate functions.",
        f"6. Run tests: `{test_cmd}`",
        f"7. `git push --force-with-lease origin {entry.branch}`",
        "8. Exit 0 if push succeeds; non-zero otherwise.",
        "",
        "## When NOT to guess",
        "",
        "If the conflict is **semantic** — the same function modified two",
        "different ways, contradictory logic, an API rename that the other",
        "side doesn't know about — DO NOT guess. Post a comment on issue",
        f"#{entry.issue_number} describing the conflict regions and exit non-zero.",
        "",
        "You will NOT use `gh`. The coordinator owns PR retries.",
    ]
    return "\n".join(lines)


# ── Machine selection ───────────────────────────────────────────────────────

def pick_conflict_fix_machine(
    repo_name: str,
    board: Board,
    config: Config,
    *,
    prefer_machine: str | None = None,
) -> Machine | None:
    """Pick a machine that has *repo_name* checked out. ``prefer_machine``
    wins if it can handle the repo (typically the original worker), so the
    rebase uses an existing local checkout.

    Returns ``None`` when no configured machine can handle the repo.
    """
    candidates = [m for m in config.machines if m.can_work_on(repo_name)]
    if not candidates:
        return None

    busy = {a.machine_name for a in board.active if a.status in ("pending", "running")}

    # 1. The preferred machine if it's idle and can handle the repo.
    if prefer_machine is not None:
        preferred = next((m for m in candidates if m.name == prefer_machine), None)
        if preferred is not None and preferred.name not in busy:
            return preferred

    # 2. Any idle machine that handles the repo.
    idle = [m for m in candidates if m.name not in busy]
    if idle:
        return idle[0]

    # 3. Anyone (including busy) — the assignment will queue on the agent.
    return candidates[0]


# ── Dispatch ────────────────────────────────────────────────────────────────


def dispatch_conflict_fix(
    entry: QueuedMerge,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    prefer_machine: str | None = None,
    now: float | None = None,
) -> Assignment | None:
    """Send a ``type="conflict-fix"`` assignment for *entry* to an agent.

    Returns the new ``Assignment``, or ``None`` when dispatch couldn't proceed
    (no capable machine, no ``repo_path`` configured, agent unreachable, …).
    The caller is responsible for persisting the board.

    Concurrency cap: if a conflict-fix is already in flight for this entry's
    ``assignment_id``, the call is a no-op (returns None).  This mirrors the
    review/smoke dedupe path — re-running ``coord merge`` shouldn't spawn a
    second fixer.
    """
    from coord.claim import has_active_followup  # noqa: PLC0415

    if has_active_followup(
        board,
        of_assignment_id=entry.assignment_id,
        assignment_type="conflict-fix",
    ):
        return None

    repo = config.repo(entry.repo_name)
    if repo is None:
        return None

    machine = pick_conflict_fix_machine(
        entry.repo_name, board, config, prefer_machine=prefer_machine,
    )
    if machine is None:
        return None

    repo_path = machine.repo_path(entry.repo_name)
    if repo_path is None:
        return None

    briefing = build_conflict_fix_briefing(
        entry=entry,
        repo_path=repo_path,
        test_command=repo.test_command,
    )

    payload = {
        "repo_name": entry.repo_name,
        "repo_path": repo_path,
        "issue_number": entry.issue_number,
        "issue_title": f"[conflict-fix] {entry.issue_title}",
        "briefing": briefing,
        "files_allowed": [],
        "files_forbidden": [],
        "pull_repos": [],
        "type": "conflict-fix",
        "system_prompt": CONFLICT_FIX_SYSTEM_PROMPT,
        "review_target": entry.branch,
        "branch": entry.branch,
    }

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    client = http_client or httpx
    try:
        resp = client.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    fix_assignment = Assignment(
        machine_name=machine.name,
        repo_name=entry.repo_name,
        issue_number=entry.issue_number,
        issue_title=f"[conflict-fix] {entry.issue_title}",
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        branch=entry.branch,
        dispatched_at=now if now is not None else time.time(),
        type="conflict-fix",
        review_target=entry.branch,
        review_of_assignment_id=entry.assignment_id,
    )
    board.active.append(fix_assignment)

    from coord.state import record_dispatched_assignment  # noqa: PLC0415

    record_dispatched_assignment(
        assignment=fix_assignment,
        repo_github=repo.github,
    )

    return fix_assignment
