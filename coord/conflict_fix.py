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
- The coordinator denies `gh` and `git push --force` for this worker. \
Don't try to use them — the harness will reject the call.
- Stay on the worker's branch — do NOT push to main / develop / target.
- Use git push --force-with-lease (NOT --force).
- If conflicts are mechanical (non-overlapping struct fields, list entries, \
imports, separate functions), resolve them additively — keep both sides.
- If conflicts are SEMANTIC (same function modified two ways, contradictory \
logic), DO NOT GUESS. Exit non-zero with a clear STUCK: line describing the \
conflict regions. The coordinator will surface this on the issue and the \
user resolves manually.

Progress reporting:
- After each significant step (rebase started, conflicts resolved, tests \
passed, pushed), output:
  STATUS: [what you just did] → [what you're about to do] → [confidence]
- If you've tried and failed, output:
  STUCK: [what you tried] [why it failed]
  Then stop and wait for guidance.\
"""

# Denied for conflict-fix workers. The agent's deny_commands enforcement
# (coord/agent.py:build_deny_prompt + harness gate) refuses these patterns
# regardless of what the prompt asks for. Keeps CLAUDE.md's "gh is denied"
# claim honest (#243-review-2).
CONFLICT_FIX_DENY_COMMANDS = [
    "Bash(gh *)",
    "Bash(git push --force *)",
    "Bash(git push -f *)",
]


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
        "side doesn't know about — DO NOT guess. Exit non-zero with a",
        "`STUCK:` line that names the file(s) and line ranges in conflict.",
        f"The coordinator will post that on issue #{entry.issue_number} and",
        "mark the merge entry as needing human resolution.",
        "",
        "You will NOT use `gh` or `git push --force` — both are denied by",
        "the harness. The coordinator owns PR retries and issue posting.",
    ]
    return "\n".join(lines)


# ── Retry-cap guard ─────────────────────────────────────────────────────────


def has_prior_conflict_fix(board: Board, merge_entry_id: str | None) -> bool:
    """True if a conflict-fix worker has already been dispatched for *merge_entry_id*
    in this session — looking at both active and completed assignments.

    The original :func:`coord.claim.has_active_followup` only scans
    ``board.active``, which lets a second conflict-fix dispatch fire once the
    first moves to ``board.completed`` (e.g. after a successful rebase that
    nevertheless leaves a fresh conflict on the next ``coord merge``).  The
    spec caps retries at one per session, so the dispatcher and the
    ``coord merge`` caller both consult this combined predicate.
    """
    if merge_entry_id is None:
        return False
    for a in list(board.active) + list(board.completed):
        if a.type != "conflict-fix":
            continue
        if a.review_of_assignment_id == merge_entry_id:
            return True
    return False


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

    Retry cap: if a conflict-fix has *ever* been dispatched for this entry's
    ``assignment_id`` in the current session — active OR completed — the call
    is a no-op (returns None).  Per the spec, we cap at one conflict-fix
    attempt per merge entry; the caller is responsible for marking the entry
    HUMAN_REQUIRED when this guard fires, so the user takes over.
    """
    if has_prior_conflict_fix(board, entry.assignment_id):
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

    # Merge repo-level deny rules with the conflict-fix-specific ones so
    # `gh` and `git push --force` are actually denied (not just discouraged
    # by the system prompt).  Dedupe by simple set conversion — patterns
    # are exact strings on the agent side, so collisions are safe to fold.
    repo_deny = (
        list(repo.worker_permissions.deny) if repo.worker_permissions else []
    )
    deny_commands = list(dict.fromkeys(repo_deny + CONFLICT_FIX_DENY_COMMANDS))

    payload = {
        "repo_name": entry.repo_name,
        "repo_path": repo_path,
        "issue_number": entry.issue_number,
        "issue_title": f"[conflict-fix] {entry.issue_title}",
        "briefing": briefing,
        "files_allowed": [],
        "files_forbidden": [],
        "pull_repos": [],
        "deny_commands": deny_commands,
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
