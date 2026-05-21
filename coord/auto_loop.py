"""Auto-loop: drive the review → fix → re-review cycle until clean.

When a **review** assignment completes with a verdict of ``request-changes``,
this module dispatches a fix worker on the same branch with the reviewer's
findings as the briefing.  When the fix worker finishes, the normal review
dispatch machinery (reconcile / notify) fires another review automatically —
creating a closed loop.

The loop terminates when:
  - A review approves the changes (verdict = ``approve``)
  - The iteration count hits ``pipeline.max_review_iterations``
  - A fix worker fails to dispatch (agent unreachable, no capable machine, etc.)

Config (coordinator.yml)::

    pipeline:
      auto_loop: true            # default true
      max_review_iterations: 3   # default 3

Integration:
  Called from ``coord.notify.run()`` after review completion transitions are
  posted.  ``run_for_review_transition`` loads the board, processes the review,
  saves if a fix was dispatched, and returns a list of :class:`LoopAction`
  for logging.

Data model:
  ``Assignment.review_iteration`` tracks the fix-round number.  The original
  work assignment has ``review_iteration=0``.  Each fix worker gets the
  previous worker's iteration + 1.  When the auto-loop sees a review that
  requests changes and the reviewed work's ``review_iteration >=
  max_review_iterations``, it posts a notice to GitHub and stops.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

import httpx

from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.models import Assignment, Board
from coord.review import parse_review_from_log
from coord.state import load_board, record_dispatched_assignment, save_board

log = logging.getLogger(__name__)


# ── Action reporting ──────────────────────────────────────────────────────────

@dataclass
class LoopAction:
    """One step taken by the auto-loop, for logging and test assertions."""

    kind: str
    """One of:
    - ``"fix_dispatched"``   — a fix worker was dispatched
    - ``"approved"``         — review approved; no further action needed
    - ``"max_iterations"``   — loop stopped; user intervention required
    - ``"no_findings"``      — log had no structured REVIEW_VERDICT block
    - ``"no_work_found"``    — could not locate the work assignment on the board
    - ``"disabled"``         — auto_loop is disabled in config
    """
    assignment_id: str | None
    detail: str = ""


# ── Core logic ────────────────────────────────────────────────────────────────

def process_review_completion(
    review: Assignment,
    board: Board,
    config: Config,
    *,
    log_path: str | None = None,
    http_client: httpx.Client | None = None,
) -> list[LoopAction]:
    """Process a completed review assignment through the auto-loop.

    Parses the reviewer's verdict from *log_path*, then either:

    - Returns an ``approved`` action (no side effects) if verdict is
      ``approve``.
    - Dispatches a fix worker (mutates *board*) if verdict is
      ``request-changes`` and the iteration limit has not been reached.
    - Posts a GitHub notice and returns a ``max_iterations`` action when the
      loop has run too many times.

    The caller is responsible for persisting the board after this returns.
    """
    if not config.pipeline.auto_loop:
        return [LoopAction(kind="disabled", assignment_id=review.assignment_id)]

    findings = parse_review_from_log(log_path) if log_path else None
    if findings is None:
        log.debug(
            "auto_loop: no structured REVIEW_VERDICT in %s — skipping", log_path
        )
        return [LoopAction(
            kind="no_findings",
            assignment_id=review.assignment_id,
            detail=f"No structured review output in {log_path!r}",
        )]

    if findings.verdict == "approve":
        # Mark the work assignment's review as done for board consistency.
        if review.review_of_assignment_id:
            work = board.find_by_id(review.review_of_assignment_id)
            if work is not None:
                work.review_state = "done"
        return [LoopAction(
            kind="approved",
            assignment_id=review.assignment_id,
            detail="Review verdict: approve — pipeline advancing",
        )]

    # verdict == "request-changes" → try to dispatch a fix worker.
    return _dispatch_fix_for_review(
        review, findings, board, config, http_client=http_client
    )


def _dispatch_fix_for_review(
    review: Assignment,
    findings,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
) -> list[LoopAction]:
    """Find the reviewed work assignment and dispatch a fix worker for it."""
    # Locate the work assignment that was reviewed.
    work: Assignment | None = None
    if review.review_of_assignment_id:
        work = board.find_by_id(review.review_of_assignment_id)

    if work is None:
        log.warning(
            "auto_loop: cannot find work assignment for review %s "
            "(review_of_assignment_id=%r)",
            review.assignment_id,
            review.review_of_assignment_id,
        )
        return [LoopAction(
            kind="no_work_found",
            assignment_id=review.assignment_id,
            detail=(
                f"work assignment {review.review_of_assignment_id!r} not on board"
            ),
        )]

    # Compute the next iteration number and check the limit.
    next_iteration = (work.review_iteration or 0) + 1
    max_iter = config.pipeline.max_review_iterations

    if next_iteration > max_iter:
        log.warning(
            "auto_loop: max_review_iterations (%d) reached for assignment %s "
            "— stopping loop and notifying user",
            max_iter, work.assignment_id,
        )
        _post_max_iterations_notice(work, config)
        return [LoopAction(
            kind="max_iterations",
            assignment_id=review.assignment_id,
            detail=(
                f"max_review_iterations={max_iter} reached for "
                f"work assignment {work.assignment_id}"
            ),
        )]

    # Build briefing and dispatch.
    briefing = _build_fix_briefing(work, findings, next_iteration, max_iter)
    fix = _dispatch_fix(
        work, briefing, board, config, next_iteration, http_client=http_client
    )

    if fix is None:
        return [LoopAction(
            kind="no_work_found",
            assignment_id=review.assignment_id,
            detail="fix worker dispatch failed (agent unreachable or no capable machine)",
        )]

    log.info(
        "auto_loop: dispatched fix worker %s for review %s (iteration %d/%d)",
        fix.assignment_id, review.assignment_id, next_iteration, max_iter,
    )
    return [LoopAction(
        kind="fix_dispatched",
        assignment_id=review.assignment_id,
        detail=(
            f"fix worker {fix.assignment_id} dispatched to {fix.machine_name} "
            f"(iteration {next_iteration}/{max_iter})"
        ),
    )]


def _build_fix_briefing(
    work: Assignment,
    findings,
    iteration: int,
    max_iter: int,
) -> str:
    """Assemble the briefing for the fix worker.  Pure function — easy to test."""
    lines: list[str] = [
        f"# Fix assignment (iteration {iteration}/{max_iter}): {work.issue_title}",
        "",
        f"You are fixing review findings for issue #{work.issue_number}.",
        (
            f"Work on branch `{work.branch or '(check your git branches)'}` — "
            "**do not change the branch name**."
        ),
        "",
        "## Reviewer findings to address",
        "",
        findings.body.strip(),
        "",
        "## Instructions",
        "",
        "1. Read the review findings above carefully.",
        "2. Fix **every** issue identified by the reviewer.",
        "3. Stay on the **same branch** — push your fixes to the existing branch.",
        "4. Run the project test suite and ensure all tests pass before pushing.",
        (
            f"5. This is fix iteration {iteration} of {max_iter} allowed. "
            "Address all findings completely so the next review can approve."
        ),
        "",
        "STATUS: reading review findings → implementing fixes → confidence: high",
        "",
    ]
    if work.briefing and work.briefing.strip():
        lines += [
            "## Original work briefing",
            "",
            work.briefing.strip(),
            "",
        ]
    return "\n".join(lines)


def _dispatch_fix(
    work: Assignment,
    briefing: str,
    board: Board,
    config: Config,
    iteration: int,
    *,
    http_client: httpx.Client | None = None,
) -> Assignment | None:
    """POST a fix assignment to the agent server.

    Prefers the same machine as the original worker (the branch is already
    checked out there).  Falls back to any capable machine.

    Returns the new Assignment (already added to ``board.active``), or None
    on failure.
    """
    # Pick machine: prefer the original worker's machine first.
    machine = next(
        (m for m in config.machines if m.name == work.machine_name), None
    )
    if machine is None or not machine.can_work_on(work.repo_name) or machine.repo_path(work.repo_name) is None:
        # Fallback: any machine capable of working on this repo.
        candidates = [
            m for m in config.machines
            if m.can_work_on(work.repo_name) and m.repo_path(work.repo_name) is not None
        ]
        if not candidates:
            log.warning(
                "auto_loop: no machine can handle repo %r", work.repo_name
            )
            return None
        machine = candidates[0]

    repo_path = machine.repo_path(work.repo_name)
    if repo_path is None:
        return None

    repo = config.repo(work.repo_name)
    if repo is None:
        return None

    payload = {
        "repo_name": work.repo_name,
        "repo_path": repo_path,
        "issue_number": work.issue_number,
        "issue_title": f"[fix-{iteration}] {work.issue_title}",
        "briefing": briefing,
        "files_allowed": work.files_allowed,
        "files_forbidden": work.files_forbidden,
        "pull_repos": [],
        "type": "work",
    }

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    client = http_client or httpx
    try:
        resp = client.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        log.warning("auto_loop: agent request failed for fix dispatch: %s", exc)
        return None

    fix_assignment = Assignment(
        machine_name=machine.name,
        repo_name=work.repo_name,
        issue_number=work.issue_number,
        issue_title=f"[fix-{iteration}] {work.issue_title}",
        files_allowed=list(work.files_allowed),
        files_forbidden=list(work.files_forbidden),
        briefing=briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        branch=work.branch,
        pr_url=work.pr_url,
        dispatched_at=time.time(),
        type="work",
        # Link back so the next review can find the work chain.
        review_of_assignment_id=work.assignment_id,
        # Iteration counter so the loop knows when to stop.
        review_iteration=iteration,
    )
    board.active.append(fix_assignment)

    record_dispatched_assignment(
        assignment=fix_assignment,
        repo_github=repo.github,
    )
    return fix_assignment


def _post_max_iterations_notice(work: Assignment, config: Config) -> None:
    """Post a GitHub issue comment when the loop hits the iteration limit."""
    from coord import github_ops  # noqa: PLC0415

    repo = config.repo(work.repo_name)
    if repo is None:
        return

    completed_rounds = work.review_iteration  # rounds completed so far
    max_iter = config.pipeline.max_review_iterations
    body = (
        f"<!-- coord:event=auto_loop_stopped assignment={work.assignment_id} -->\n"
        f"## ⚠️ Auto-loop stopped — max review iterations reached\n\n"
        f"The review → fix cycle for issue **#{work.issue_number}** has "
        f"completed **{completed_rounds}** fix round(s) without receiving an "
        f"approval, which equals the configured maximum of "
        f"**{max_iter}** `pipeline.max_review_iterations`.\n\n"
        f"**Manual intervention required.** Options:\n"
        f"- Review the diff and the reviewer's latest findings, then dispatch "
        f"a fix manually with `coord assign`.\n"
        f"- Adjust the issue scope and open a fresh issue.\n\n"
        f"Details:\n"
        f"- Assignment: `{work.assignment_id}`\n"
        f"- Branch: `{work.branch or '(unknown)'}`\n"
        f"- Completed fix rounds: {completed_rounds}/{max_iter}\n"
    )
    try:
        github_ops.post_issue_comment(repo.github, work.issue_number, body)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "auto_loop: failed to post max-iterations notice for %s: %s",
            work.assignment_id, exc,
        )


# ── notify.py integration ─────────────────────────────────────────────────────

def run_for_review_transition(
    assignment_id: str,
    record: dict,
    entry: dict,
    config: Config,
) -> list[LoopAction]:
    """Entry point called from ``notify.run()`` for each completed review.

    Loads the board from the database, processes the review completion, saves
    the board if a fix worker was dispatched, and returns the list of actions
    taken.

    Parameters
    ----------
    assignment_id:
        The completed review's assignment ID.
    record:
        The dispatched-assignment record dict (from ``load_dispatched()``).
    entry:
        The agent /status entry for this assignment (contains ``log_path``).
    config:
        Parsed coordinator config.
    """
    if not config.pipeline.auto_loop:
        return [LoopAction(kind="disabled", assignment_id=assignment_id)]

    if record.get("type") != "review":
        return []

    board = load_board()
    if board is None:
        log.debug("auto_loop: no board — skipping review %s", assignment_id)
        return []

    review = board.find_by_id(assignment_id)
    if review is None:
        # Review not on board yet — it was recorded by notify but the board
        # might not be persisted.  Try looking it up by review_of_assignment_id
        # from the record dict and create a minimal proxy.
        log.debug(
            "auto_loop: review %s not found on board — cannot process", assignment_id
        )
        return []

    log_path: str | None = entry.get("log_path")
    actions = process_review_completion(review, board, config, log_path=log_path)

    if any(a.kind == "fix_dispatched" for a in actions):
        save_board(board)

    return actions
