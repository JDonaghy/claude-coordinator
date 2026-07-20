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
logic), DO NOT GUESS. Exit non-zero with a STUCK: line that starts with the \
marker `coord:conflict=semantic` and then names the conflicting files and \
line ranges, e.g.
  STUCK: coord:conflict=semantic src/foo.py:40-72 — both sides rewrote \
parse_args() differently
The coordinator reads that marker and decides what happens next.

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


# ── #1291: semantic-conflict escalation ─────────────────────────────────────
#
# A conflict-fix worker decides for itself whether a conflict is mechanical or
# semantic (see the "When NOT to guess" section of the briefing below) and
# reports the verdict on its STUCK: line.  The verdict is machine-parseable —
# a fixed marker, NOT a prose regex over "semantic"/"same function"/… which
# would rot the first time a worker phrased its give-up differently.
SEMANTIC_STUCK_MARKER = "coord:conflict=semantic"

# Prefix on the escalated worker's `issue_title`.  Coordinator-generated (so
# matching it is not prose-matching), and it is what makes the escalation
# visible: the TUI Pipeline renders the conflict-fix row's title, and
# `has_prior_semantic_escalation` uses it to enforce exactly-one-retry.
SEMANTIC_FIX_TITLE_PREFIX = "[semantic-merge]"

SEMANTIC_CONFLICT_SYSTEM_PROMPT = """\
You are a Claude Code merge worker. A first worker rebased a branch onto its \
target branch, hit a conflict it judged SEMANTIC — the two sides changed the \
same behaviour in incompatible ways — and stopped rather than guess. You are \
the second attempt: your job is to understand both intents and produce a \
resolution that honours both.

Constraints:
- The coordinator denies `gh` and `git push --force` for this worker.
- Stay on the worker's branch — never push to main / develop / the target \
branch. Push with `git push --force-with-lease`.
- The project's tests must pass before you push. A resolution that compiles \
but breaks behaviour is worse than no resolution.
- If you cannot honour both intents with confidence, stop and say so with a \
`STUCK:` line. The coordinator escalates to a human; that is a fine outcome \
and much better than a plausible guess.

Progress reporting: emit `STATUS:` lines as you go, and a `STUCK:` line if \
you stop.\
"""


def build_semantic_conflict_briefing(
    *,
    entry: QueuedMerge,
    repo_path: str,
    test_command: str | None,
    stuck_summary: str | None = None,
) -> str:
    """#1291: briefing for the escalated (semantic) second attempt.

    Deliberately goal-and-constraint shaped rather than a numbered recipe —
    the mechanical briefing's step list is the right shape for a rebase, and
    the wrong shape here: over-prescriptive prompts measurably reduce the
    stronger model's output quality on open-ended reasoning.  The first
    worker already proved the mechanical path doesn't apply.
    """
    test_cmd = test_command or "echo '(no test command configured)'"
    lines: list[str] = [
        f"# Semantic merge: {entry.repo_github} `{entry.branch}` → "
        f"`{entry.target_branch}`",
        "",
        f"Issue: #{entry.issue_number} — {entry.issue_title}",
        "",
        "A first conflict-fix worker attempted the rebase in "
        f"`{repo_path}` and stopped: it judged the conflict **semantic** — "
        "both sides changed the same behaviour in incompatible ways, so "
        "keeping both hunks is not a resolution.",
        "",
    ]
    if stuck_summary:
        lines += [f"What it reported: {stuck_summary}", ""]
    lines += [
        "## Goal",
        "",
        f"`{entry.branch}` rebased onto `{entry.target_branch}`, with a "
        "resolution that preserves what BOTH sides were trying to do, "
        "tests green, pushed to the same branch.",
        "",
        "Read enough of the history and the surrounding code to know what "
        "each side intended before you write the resolution. Intent, not "
        "textual reconciliation, is the whole job here.",
        "",
        "## Constraints",
        "",
        f"- Tests must pass: `{test_cmd}`. Do not push a red tree.",
        f"- Push only `{entry.branch}`, only with `git push --force-with-lease`.",
        "- `gh` and `git push --force` are denied by the harness. The "
        "coordinator owns PR retries, merges, and issue comments.",
        "- Every merge gate still applies after you finish (tests, CI, "
        "verify-merge, review). Nothing here is force-merged, so a "
        "resolution you are not confident in will be caught — but it will "
        "cost a human a review cycle.",
        "- If both intents cannot be honoured together, stop and explain on "
        "a `STUCK:` line. Handing this to a human is the correct outcome; "
        "guessing is not.",
        "",
        f"Last merge error: {entry.error or 'unknown conflict'}",
    ]
    return "\n".join(lines)


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
        "`STUCK:` line that begins with the exact marker",
        f"`{SEMANTIC_STUCK_MARKER}` and then names the file(s) and line",
        "ranges in conflict, e.g.",
        "",
        f"    STUCK: {SEMANTIC_STUCK_MARKER} src/foo.py:40-72 — both sides",
        "    rewrote parse_args() differently",
        "",
        "The coordinator reads that marker to decide what happens next: the",
        f"outcome is posted on issue #{entry.issue_number} and the merge",
        "entry is either escalated for one stronger attempt or marked as",
        "needing human resolution.",
        "",
        "You will NOT use `gh` or `git push --force` — both are denied by",
        "the harness. The coordinator owns PR retries and issue posting.",
    ]
    return "\n".join(lines)


# ── Semantic-verdict detection (#1291) ──────────────────────────────────────


def _decode_worker_text(raw: str) -> str:
    """Return the human-readable text of a worker log.

    Handles both plain-text logs and the stream-json format (each line a
    JSON event) the agent writes by default — in the latter case the
    assistant text blocks are concatenated.  Mirrors the detection used by
    :func:`coord.progress.parse_completion_summary_from_agent`.
    """
    stream_json = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stream_json = stripped.startswith("{")
        break

    if not stream_json:
        return raw

    from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415

    decoded: list[str] = []
    for line in raw.splitlines():
        event = parse_event(line.rstrip("\n"))
        if event is None or event.type != "assistant":
            continue
        text = _assistant_text(event)
        if text:
            decoded.append(text)
    return "\n".join(decoded)


def semantic_verdict_in_text(text: str | None) -> bool:
    """True when a worker log carries the semantic-conflict marker."""
    if not text:
        return False
    return SEMANTIC_STUCK_MARKER in _decode_worker_text(text)


def detect_semantic_conflict(
    *,
    log_path: str | None = None,
    host: str | None = None,
    assignment_id: str | None = None,
    port: int = AGENT_PORT,
    timeout: float = 15.0,
) -> bool:
    """True when the finished conflict-fix worker reported a SEMANTIC conflict.

    Tries the local log file first (coordinator-local worker), then falls
    back to the agent's ``/logs/<id>`` endpoint for remote workers — the
    same two-step every other log-parsing consumer uses (review findings,
    plans, completion summaries).  Best-effort: any read/transport failure
    returns ``False``, which means "not semantic" and preserves today's
    HUMAN_REQUIRED behaviour.
    """
    if log_path:
        try:
            from pathlib import Path  # noqa: PLC0415

            p = Path(log_path)
            if p.exists():
                raw = p.read_text(encoding="utf-8", errors="replace")
                if semantic_verdict_in_text(raw):
                    return True
        except OSError:
            pass

    if host and assignment_id:
        try:
            resp = httpx.get(
                f"http://{host}:{port}/logs/{assignment_id}", timeout=timeout
            )
            resp.raise_for_status()
            return semantic_verdict_in_text(resp.text)
        except (httpx.HTTPError, httpx.TimeoutException):
            return False

    return False


def has_prior_semantic_escalation(board: Board, merge_entry_id: str | None) -> bool:
    """True when this merge entry already had its ONE escalated attempt.

    Matches on any status — running, done, failed — so the escalation can
    never fire twice for the same entry.  A second semantic failure falls
    through to HUMAN_REQUIRED exactly as before (#1291: one retry, no loop).
    """
    if merge_entry_id is None:
        return False
    for a in list(board.active) + list(board.completed):
        if a.type != "conflict-fix":
            continue
        if a.review_of_assignment_id != merge_entry_id:
            continue
        if (a.issue_title or "").startswith(SEMANTIC_FIX_TITLE_PREFIX):
            return True
    return False


# ── Retry-cap guard ─────────────────────────────────────────────────────────


def _has_active_conflict_fix(board: Board, merge_entry_id: str | None) -> bool:
    """True when a conflict-fix for *merge_entry_id* is running or pending."""
    if merge_entry_id is None:
        return False
    return any(
        a.type == "conflict-fix"
        and a.review_of_assignment_id == merge_entry_id
        and a.status in ("running", "pending")
        for a in list(board.active) + list(board.completed)
    )


def has_prior_conflict_fix(board: Board, merge_entry_id: str | None) -> bool:
    """True when a second conflict-fix dispatch for *merge_entry_id* is blocked.

    Blocks when a conflict-fix is **active** (running/pending — don't spawn a
    duplicate) or has **genuinely failed** (failed/advisory — retry cap
    consumed, escalate to human).

    #784: a conflict-fix that completed **successfully** (``status="done"``)
    does *not* block a subsequent dispatch.  A successful rebase can be
    followed by a new conflict if other PRs merged in the meantime; that is a
    fresh situation and warrants a fresh fix attempt.  Only actual failures
    consume the one-per-entry cap.
    """
    if merge_entry_id is None:
        return False
    for a in list(board.active) + list(board.completed):
        if a.type != "conflict-fix":
            continue
        if a.review_of_assignment_id != merge_entry_id:
            continue
        # Active attempt in flight — prevent duplicate dispatch.
        if a.status in ("running", "pending"):
            return True
        # Genuine failure — retry cap consumed, surface to human.
        if a.status in ("failed", "advisory"):
            return True
        # "done" = successful rebase → cap not consumed; a re-conflict is new.
        # "cancelled" falls through here too — a cancelled attempt did no work,
        # so it is treated the same as "done": re-dispatch is allowed.
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
    semantic: bool = False,
    model: str | None = None,
    stuck_summary: str | None = None,
) -> Assignment | None:
    """Send a ``type="conflict-fix"`` assignment for *entry* to an agent.

    Returns the new ``Assignment``, or ``None`` when dispatch couldn't proceed
    (no capable machine, no ``repo_path`` configured, agent unreachable, …).
    The caller is responsible for persisting the board.

    Retry cap: blocks on two conditions — (1) an **active** conflict-fix
    (``running``/``pending``) for this entry is already in flight, preventing
    duplicate dispatch; or (2) a **failed** conflict-fix (``failed``/
    ``advisory``) already completed, consuming the one-per-entry retry cap so
    the caller marks the entry ``HUMAN_REQUIRED``.  A ``done`` (successful)
    conflict-fix does *not* block a new dispatch — a successful rebase can be
    followed by a fresh conflict if other PRs merged in the meantime, and that
    warrants a new attempt rather than an immediate human escalation (#784).

    ``semantic=True`` (#1291) dispatches the escalated second attempt: a
    different, less prescriptive briefing (see
    :func:`build_semantic_conflict_briefing`) and, with *model*, a stronger
    model.  It deliberately bypasses the *failed-prior* half of the retry
    cap — the mechanical attempt that just failed is precisely what triggers
    it — but is itself capped at one per entry by
    :func:`has_prior_semantic_escalation`, and still refuses to dispatch
    while another conflict-fix is in flight.  Because the escalated attempt
    is itself a ``conflict-fix`` row, its failure consumes the ordinary
    retry cap and the entry goes HUMAN_REQUIRED — no loop.
    """
    if semantic:
        if has_prior_semantic_escalation(board, entry.assignment_id):
            return None
        if _has_active_conflict_fix(board, entry.assignment_id):
            return None
    elif has_prior_conflict_fix(board, entry.assignment_id):
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

    if semantic:
        briefing = build_semantic_conflict_briefing(
            entry=entry,
            repo_path=repo_path,
            test_command=repo.test_command,
            stuck_summary=stuck_summary,
        )
        system_prompt = SEMANTIC_CONFLICT_SYSTEM_PROMPT
        # #1291 visibility: the title is what the TUI Pipeline row shows, so
        # the operator can see a semantic merge was attempted (and by which
        # model) rather than discovering it post-merge.
        title = f"{SEMANTIC_FIX_TITLE_PREFIX} {entry.issue_title}"
        if model:
            title = f"{SEMANTIC_FIX_TITLE_PREFIX}[{model}] {entry.issue_title}"
    else:
        briefing = build_conflict_fix_briefing(
            entry=entry,
            repo_path=repo_path,
            test_command=repo.test_command,
        )
        system_prompt = CONFLICT_FIX_SYSTEM_PROMPT
        title = f"[conflict-fix] {entry.issue_title}"

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
        "issue_title": title,
        "briefing": briefing,
        "files_allowed": [],
        "files_forbidden": [],
        "pull_repos": [],
        "deny_commands": deny_commands,
        "type": "conflict-fix",
        "system_prompt": system_prompt,
        "review_target": entry.branch,
        "branch": entry.branch,
        # #277: pin the agent to the original branch — otherwise it derives a
        # slug from the "[conflict-fix] …" issue_title and pushes the rebase
        # to an orphan branch, leaving the real PR stale.
        "target_branch": entry.branch,
    }
    if model:
        # Same wire shape the fix/review dispatchers use: the alias is
        # resolved to a pinned exact model id when `models.versions` maps it.
        payload["model"] = config.models.resolve(model)

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
        issue_title=title,
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        model=model,
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

    # #1038: operational-tier row alongside the business-tier "dispatched"
    # row `record_dispatched_assignment` already writes.  The business row
    # marks WHAT happened (a conflict-fix assignment was dispatched); this
    # marks that it was the coordinator's own automatic mechanical-conflict
    # classification/retry-cap logic that decided to do it, not a human
    # picking this entry — the same distinction the other #1038 hooks draw.
    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="merge",
        event_type=(
            "semantic_conflict_escalated" if semantic else "conflict_fix_dispatched"
        ),
        actor="daemon",
        summary=(
            f"semantic conflict escalated to {model or 'default model'}: "
            f"{entry.repo_name}#{entry.issue_number} → {machine.name}"
            if semantic
            else f"conflict-fix dispatched: {entry.repo_name}#{entry.issue_number} "
            f"→ {machine.name}"
        ),
        repo=entry.repo_name,
        issue=entry.issue_number,
        assignment_id=fix_assignment.assignment_id,
        machine=machine.name,
        details={
            "merge_entry_id": entry.assignment_id,
            "semantic": semantic,
            "model": model,
        },
    )

    return fix_assignment
