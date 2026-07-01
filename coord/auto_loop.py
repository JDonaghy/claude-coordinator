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
from coord import github_ops
from coord.models import Assignment, Board
from coord.review import (
    ReviewFindings,
    dispatch_review,
    estimate_review_counts,
    parse_review_from_agent,
    parse_review_from_log,
)
from coord.board_service import read_board, write_board
from coord.state import record_dispatched_assignment

log = logging.getLogger(__name__)


# ── Action reporting ──────────────────────────────────────────────────────────

@dataclass
class LoopAction:
    """One step taken by the auto-loop, for logging and test assertions."""

    kind: str
    """One of:
    - ``"fix_dispatched"``     — a fix worker was dispatched
    - ``"approved"``           — review approved; no further action needed
    - ``"approved_with_nits"`` — review said request-changes but flagged no
                                 blocking findings (#476); pipeline advanced and
                                 no fix dispatched
    - ``"max_iterations"``     — loop stopped; user intervention required
    - ``"no_findings"``        — log had no structured REVIEW_VERDICT block
    - ``"no_work_found"``      — could not locate the work assignment on the board
    - ``"disabled"``           — auto_loop is disabled in config
    - ``"review_dispatched"``  — a re-review was dispatched after a fix worker completed
    - ``"iteration_cap_hit"``  — fix.review_iteration >= max_review_iterations;
                                 not dispatching another review
    - ``"terminal_skip"``      — the work's issue is already closed or its PR is
                                 already merged; no fix/review dispatched (#522)
    - ``"interactive_skip"``   — the fix was an interactive (claude-pty) session;
                                 its re-review is human-attended, so no headless
                                 review was dispatched (#555)
    """
    assignment_id: str | None
    detail: str = ""


# ── Terminal-state guard (#522) ───────────────────────────────────────────────

def _work_is_terminal(
    work: Assignment,
    config: Config,
    *,
    cache: dict | None = None,
) -> bool:
    """True when *work* is already done on GitHub and must not be re-dispatched.

    Thin Assignment/Config-shaped wrapper over
    :func:`coord.github_ops.work_is_terminal` (the shared chokepoint guard,
    #522) — resolves the repo's GitHub slug and delegates.  Fail-open.
    """
    repo = config.repo(work.repo_name)
    if repo is None or not repo.github:
        return False

    from coord import github_ops  # noqa: PLC0415

    return github_ops.work_is_terminal(
        repo.github, work.issue_number, work.branch, cache=cache
    )


# ── Core logic ────────────────────────────────────────────────────────────────

def _load_review_findings(
    review: Assignment,
    log_path: str | None,
    machine_host: str | None,
    repo_github: str | None = None,
) -> ReviewFindings | None:
    """Resolve a reviewer's structured findings.

    Resolution order, cheapest first:
    1. **DB cache** — `notify` (or `report-result --body-file`) populates
       `review_findings` on the row.  Hit means zero I/O.
    2. **Local log file** — works when the review ran on this machine.
    3. **Agent HTTP `/logs/<id>`** — fetches the worker's full log
       from the remote agent (claude -p reviews on another machine).
    4. **GitHub message bus** — when `repo_github` is supplied, recover the
       findings posted to the issue under a `coord:review-findings` marker.
       This is the cross-machine path for INTERACTIVE (claude-pty) reviews,
       which have no parseable log and may not be in this machine's DB.

    Returns `None` only when ALL sources fail.
    """
    # 1. DB cache — fastest.
    if review.assignment_id:
        try:
            from coord.state import load_assignment_review_findings  # noqa: PLC0415
            cached = load_assignment_review_findings(review.assignment_id)
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "auto_loop: DB cache lookup failed for %s: %s",
                review.assignment_id, exc,
            )
            cached = None
        if cached is not None:
            verdict, body = cached
            return ReviewFindings(verdict=verdict, body=body)

    # 2. Local log file.
    findings = parse_review_from_log(log_path) if log_path else None
    if findings is not None:
        return findings

    # 3. Agent HTTP fallback.
    if machine_host:
        try:
            findings = parse_review_from_agent(machine_host, review.assignment_id or "")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "auto_loop: failed to fetch review log from agent %s for %s: %s",
                machine_host, review.assignment_id, exc,
            )
            findings = None
        if findings is not None:
            return findings

    # 4. GitHub message bus — works on ANY machine (no shared DB / local log
    #    needed).  Interactive (claude-pty) reviews post their full body to the
    #    issue under a `coord:review-findings` marker via `--body-file`; recover
    #    it here when the review ran elsewhere.  This is the cross-machine path.
    issue_number = getattr(review, "issue_number", None)
    if repo_github and issue_number and review.assignment_id:
        try:
            from coord.review import fetch_review_findings_from_github  # noqa: PLC0415
            gh_findings = fetch_review_findings_from_github(
                repo_github, int(issue_number), review.assignment_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "auto_loop: GitHub findings fetch failed for %s: %s",
                review.assignment_id, exc,
            )
            gh_findings = None
        if gh_findings is not None:
            return gh_findings

    return findings


def process_review_completion(
    review: Assignment,
    board: Board,
    config: Config,
    *,
    log_path: str | None = None,
    machine_host: str | None = None,
    http_client: httpx.Client | None = None,
    terminal_cache: dict | None = None,
) -> list[LoopAction]:
    """Process a completed review assignment through the auto-loop.

    Parses the reviewer's verdict (local log or agent HTTP fallback when
    *machine_host* is supplied), then either:

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

    _rg = None
    try:
        _rc = config.repo(review.repo_name)
        _rg = _rc.github if _rc is not None else None
    except Exception:  # noqa: BLE001
        _rg = None
    findings = _load_review_findings(review, log_path, machine_host, repo_github=_rg)
    if findings is None:
        log.debug(
            "auto_loop: no structured REVIEW_VERDICT for %s (log=%r, host=%r) — skipping",
            review.assignment_id, log_path, machine_host,
        )
        return [LoopAction(
            kind="no_findings",
            assignment_id=review.assignment_id,
            detail=(
                f"No structured review output (log={log_path!r}, host={machine_host!r})"
            ),
        )]

    # #253: persist the parsed verdict on the review assignment so the merge
    # gate can refuse to merge work whose review hasn't approved.
    review.review_verdict = findings.verdict

    if findings.verdict == "approve":
        return _advance_pipeline(
            review, board, config,
            kind="approved",
            detail="Review verdict: approve — pipeline advancing",
        )

    # verdict == "request-changes". #476 decision gate: a request-changes
    # verdict that flags NO blocking findings — only non-blocking observations
    # or nits — must NOT trigger another fix+review cycle. Doing so churns an
    # already-correct PR over cosmetic suggestions and burns the session budget
    # (the 2026-06-11 #532 incident: 3 real fix rounds, then a 4th round
    # dispatched over a single cosmetic one-liner the reviewer itself counted
    # as non-blocking). Treat advisory-only request-changes as approve-with-
    # nits: advance the pipeline, surface the nits, and do not dispatch a fix.
    blocking, nonblocking, nits = estimate_review_counts(findings.body)
    parsed_any = any(c is not None for c in (blocking, nonblocking, nits))
    has_blocking = bool(blocking)  # None or 0 → no blocking findings detected
    if parsed_any and not has_blocking:
        log.info(
            "auto_loop: request-changes with no blocking findings for review %s "
            "(blocking=%r nonblocking=%r nits=%r) — advancing as approve-with-"
            "nits, not dispatching a fix",
            review.assignment_id, blocking, nonblocking, nits,
        )
        # The merge gate keys off review_verdict; record approve so the nits
        # don't block the merge. The nits remain visible in the review comment
        # already posted to the PR, plus the advisory notice below.
        review.review_verdict = "approve"
        _post_advisory_nits_notice(review, board, config, nonblocking, nits)
        return _advance_pipeline(
            review, board, config,
            kind="approved_with_nits",
            detail=(
                "Review requested changes but flagged no blocking issues "
                f"(nonblocking={nonblocking}, nits={nits}) — advancing as "
                "approve-with-nits; no fix dispatched"
            ),
        )

    # Genuine blocking findings (or counts unparseable) → dispatch a fix worker.
    return _dispatch_fix_for_review(
        review, findings, board, config,
        http_client=http_client, terminal_cache=terminal_cache,
    )


def _advance_pipeline(
    review: Assignment,
    board: Board,
    config: Config,
    *,
    kind: str,
    detail: str,
) -> list[LoopAction]:
    """Mark the reviewed work approved and refresh its merge-queue entry.

    Shared by the plain ``approve`` path and the #476 approve-with-nits path so
    both advance the pipeline identically (the only difference is the action
    ``kind``/``detail`` reported back to the caller).
    """
    if review.review_of_assignment_id:
        work = board.find_by_id(review.review_of_assignment_id)
        if work is not None:
            work.review_state = "done"
            # #292 (Defect 2): proactively enqueue/refresh the merge queue
            # entry so the TUI shows the Merge stage as ready without requiring
            # a manual `coord merge` run first. If the entry was keyed to an
            # earlier work assignment (the original pre-bounce assignment),
            # refresh_entry_assignment updates its assignment_id so
            # has_approved_review can find this approval.
            try:
                from coord import merge_queue as mq  # noqa: PLC0415
                repo_cfg = config.repo(work.repo_name)
                if repo_cfg is not None and work.branch:
                    mq.refresh_entry_assignment(
                        work,
                        repo_github=repo_cfg.github,
                        target_branch=repo_cfg.default_branch,
                    )
            except Exception as exc:  # noqa: BLE001 — best-effort; merge gate still works
                log.warning(
                    "auto_loop: refresh_entry_assignment failed for %s: %s",
                    work.assignment_id, exc,
                )
    return [LoopAction(kind=kind, assignment_id=review.assignment_id, detail=detail)]


def _post_advisory_nits_notice(
    review: Assignment,
    board: Board,
    config: Config,
    nonblocking: int | None,
    nits: int | None,
) -> None:
    """Post a short audit-trail comment when the loop auto-advances past an
    advisory-only request-changes verdict (#476).

    Keeps the auto-advance *visible* — the user was previously burned by silent
    auto-loop behaviour. Best-effort: a gh failure must never block the
    pipeline. The full findings are already on the PR via the review comment;
    this just records the decision not to dispatch another fix round.
    """
    work = (
        board.find_by_id(review.review_of_assignment_id)
        if review.review_of_assignment_id
        else None
    )
    if work is None:
        return
    repo = config.repo(work.repo_name)
    if repo is None:
        return
    from coord import github_ops  # noqa: PLC0415

    body = (
        f"<!-- coord:event=auto_loop_advisory_advance assignment={work.assignment_id} -->\n"
        f"## ✅ Auto-advanced past advisory review (no blocking findings)\n\n"
        f"The latest review of issue **#{work.issue_number}** returned "
        f"`request-changes` but flagged **no blocking findings** "
        f"(non-blocking={nonblocking}, nits={nits}). Per the #476 decision "
        f"gate, the coordinator is **not** dispatching another fix round over "
        f"non-blocking suggestions — the PR advances to the merge gate.\n\n"
        f"The reviewer's notes remain in the review comment above. If any nit "
        f"is in fact a must-fix, dispatch a fix manually with `coord assign` "
        f"or bounce it before merging.\n"
    )
    try:
        github_ops.post_issue_comment(repo.github, work.issue_number, body)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "auto_loop: failed to post advisory-advance notice for %s: %s",
            work.assignment_id, exc,
        )


def _fix_model_for_iteration(config: Config, iteration: int) -> str | None:
    """Choose the model alias for a fix worker on a given bounce *iteration*.

    Pure function so the iteration → model mapping is unit-testable.

    Returns ``None`` when ``pipeline.escalate_fix_model`` is disabled — the
    fix dispatch then sets no model and the agent falls back to ``claude -p``'s
    default (today's behaviour).

    When escalation is enabled:
      - iteration 1 → ``config.models.default`` (first fix stays cheap/fast).
      - iteration 2+ → climb one rung up ``config.models.escalation`` per
        iteration, capped at the top of the ladder.

    Example with escalation ``[haiku, sonnet, opus]`` and default ``sonnet``:
    iter 1 → sonnet, iter 2 → opus, iter 3 → opus (capped).
    """
    if not config.pipeline.escalate_fix_model:
        return None

    model = config.models.default
    # iteration 1 stays on the base model; each later iteration escalates one
    # rung (next_model caps at the top of the ladder).
    for _ in range(max(iteration, 1) - 1):
        model = config.models.next_model(model)
    return model


def _dispatch_fix_for_review(
    review: Assignment,
    findings,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    terminal_cache: dict | None = None,
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

    # #522: never dispatch a fix for work that is already done on GitHub.
    # A merged PR / closed issue must not re-enter the review→fix loop — this
    # is the root cause of the 2026-06-09 launch flood (#349 ×4, #194).
    if _work_is_terminal(work, config, cache=terminal_cache):
        log.info(
            "auto_loop: NOT dispatching fix for %s — issue #%s is terminal "
            "(merged/closed)",
            work.assignment_id, work.issue_number,
        )
        # The review of merged work is moot; mark it resolved so the board /
        # merge gate stop treating it as needing another round.
        work.review_state = "done"
        return [LoopAction(
            kind="terminal_skip",
            assignment_id=review.assignment_id,
            detail=(
                f"issue #{work.issue_number} already merged/closed — "
                "no fix dispatched"
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

    # Build briefing and dispatch.  The fix worker escalates the model per
    # iteration (when pipeline.escalate_fix_model is enabled); compute it here
    # where the iteration is known and thread it into the dispatch.
    # #603: prepend the per-issue context digest (prior-attempt findings,
    # cross-repo deps) to the TOP of the -p fix briefing.  The interactive fix
    # path prefixes it at its own call site, so the shared _build_fix_briefing
    # stays pure (no double injection).
    from coord.state import issue_context_block  # noqa: PLC0415

    briefing = issue_context_block(work.repo_name, work.issue_number) + _build_fix_briefing(
        work, findings, next_iteration, max_iter
    )
    model = _fix_model_for_iteration(config, next_iteration)
    fix = _dispatch_fix(
        work, briefing, board, config, next_iteration,
        model=model, http_client=http_client,
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
    model: str | None = None,
    http_client: httpx.Client | None = None,
    remote_branch_checker=None,
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
    from coord.machine_pause import paused_set
    paused = paused_set()
    if (
        machine is None
        or not machine.can_work_on(work.repo_name)
        or machine.repo_path(work.repo_name) is None
        or machine.name in paused
    ):
        # Fallback: any machine capable of working on this repo, minus
        # any the user has paused via `coord pause` (routing-pause).
        candidates = [
            m for m in config.machines
            if m.can_work_on(work.repo_name)
            and m.repo_path(work.repo_name) is not None
            and m.name not in paused
        ]
        if not candidates:
            log.warning(
                "auto_loop: no machine can handle repo %r (paused=%r)",
                work.repo_name, sorted(paused)
            )
            return None
        machine = candidates[0]

    # #586: if we ended up routing to a different machine than the original
    # worker, the branch must exist on the remote so the fix worker can fetch
    # it.  If the worker never pushed, this assignment would crash in 2–3
    # seconds with no commits and no exit code — the classic branch-absent
    # silent failure.  Block early and surface a clear log message instead.
    if machine.name != work.machine_name and work.branch:
        repo_obj = config.repo(work.repo_name)
        if repo_obj is not None:
            _check_remote = remote_branch_checker or github_ops.branch_exists_on_remote
            if not _check_remote(repo_obj.github, work.branch):
                log.error(
                    "auto_loop: branch %r not on remote — cannot dispatch fix "
                    "to different machine %s; original worker %s must push "
                    "the branch to origin first",
                    work.branch, machine.name, work.machine_name,
                )
                return None

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
        # #255: fix-loop dispatches inherit the repo's configured default
        # branch so the agent branches from origin/<default> rather than
        # any local-only ref.
        "branch": repo.default_branch or "main",
        # #target_branch: tell the agent to check out the ORIGINAL work's
        # branch rather than deriving a new one from the `[fix-N] …`
        # issue title.  Without this the fix worker pushed to a
        # new orphan branch and the existing PR never received the fix
        # commits (quadraui#166 hit this hard).
        "target_branch": work.branch,
    }
    # Escalated model per bounce iteration (None when pipeline
    # .escalate_fix_model is disabled — preserves today's no-model behaviour).
    # The board record keeps the alias for legibility; the wire payload is
    # resolved through models.versions so claude -p gets an exact id when
    # one is pinned.
    if model is not None:
        payload["model"] = config.models.resolve(model)

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
        # Escalated model for this bounce iteration (None preserves the
        # legacy behaviour where the agent picks claude -p's default).
        model=model,
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
        f"- Run `coord merge --force-merge` to merge the branch as-is "
        f"(if the review findings are acceptable).\n"
        f"- Bump `pipeline.max_review_iterations` in `coordinator.yml` "
        f"(currently `{max_iter}`) to allow more automated fix rounds.\n"
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

def run_for_fix_transition(
    assignment_id: str,
    config: Config,
    *,
    terminal_cache: dict | None = None,
) -> list[LoopAction]:
    """Entry point called from ``notify.run()`` for each completed fix worker.

    When a bounce-fix worker (``type="work"``, ``review_of_assignment_id``
    IS NOT NULL, title starting with ``"[fix-"``) completes, dispatch a fresh
    review against it so the review → fix → re-review cycle closes
    automatically without manual ``coord pr`` invocations.

    Caps re-review iterations at ``config.pipeline.max_review_iterations``
    using the fix worker's ``review_iteration`` field.  When
    ``fix.review_iteration >= max_review_iterations`` the loop has already
    used all its fix rounds, so no further review is dispatched and an
    ``iteration_cap_hit`` action is returned instead.

    Parameters
    ----------
    assignment_id:
        The completed fix worker's assignment ID.
    config:
        Parsed coordinator config.

    Returns
    -------
    list[LoopAction]
        ``[LoopAction(kind="review_dispatched", ...)]`` on success,
        ``[LoopAction(kind="iteration_cap_hit", ...)]`` when the cap is hit,
        ``[LoopAction(kind="disabled", ...)]`` when auto_loop is off, or
        ``[]`` when the assignment is not found on the board or
        ``dispatch_review`` cannot find a capable machine.
    """
    if not config.pipeline.auto_loop:
        return [LoopAction(kind="disabled", assignment_id=assignment_id)]

    # #749: read_board()/write_board() route through the daemon when
    # board_service is configured — previously this always hit the local DB
    # directly regardless of thin-client status.
    board = read_board()

    fix = board.find_by_id(assignment_id)
    if fix is None:
        log.debug(
            "auto_loop: fix assignment %s not found on board — skipping",
            assignment_id,
        )
        return []

    # #555: an *interactive* fix (provider_name="claude-pty") gets its re-review
    # from the human-attended TUI flow (leg 3 #517), never a headless metered
    # `claude -p` review. Skip the automatic re-review dispatch — mirrors the
    # dispatch_pending_reviews guard for the same interactive-blindness gap.
    if fix.provider_name == "claude-pty":
        log.info(
            "auto_loop: NOT dispatching headless re-review for %s — interactive "
            "fix (provider_name=claude-pty); re-review is human-attended",
            assignment_id,
        )
        return [LoopAction(
            kind="interactive_skip",
            assignment_id=assignment_id,
            detail=(
                "interactive fix — re-review is human-attended; "
                "no headless review dispatched"
            ),
        )]

    # #522: a fix worker that finished against already-merged/closed work must
    # not trigger another review. Guards the second flood vector (re-review
    # dispatch) the same way the fix-dispatch path is guarded above.
    if _work_is_terminal(fix, config, cache=terminal_cache):
        log.info(
            "auto_loop: NOT dispatching re-review for %s — issue #%s is "
            "terminal (merged/closed)",
            assignment_id, fix.issue_number,
        )
        fix.review_state = "done"
        write_board(board)
        return [LoopAction(
            kind="terminal_skip",
            assignment_id=assignment_id,
            detail=(
                f"issue #{fix.issue_number} already merged/closed — "
                "no re-review dispatched"
            ),
        )]

    max_iter = config.pipeline.max_review_iterations
    if fix.review_iteration >= max_iter:
        log.warning(
            "auto_loop: fix %s has review_iteration=%d >= max_review_iterations=%d "
            "— not dispatching another review",
            assignment_id, fix.review_iteration, max_iter,
        )
        # Surface the cap-hit as a persisted blocker: post a GitHub comment so
        # the operator sees it outside the TUI, mark the board entry with a
        # distinct review_state so `coord status` shows an explicit blocker line,
        # and save the board so the state survives a coordinator restart.
        _post_max_iterations_notice(fix, config)
        fix.review_state = "cap_hit"
        write_board(board)
        return [LoopAction(
            kind="iteration_cap_hit",
            assignment_id=assignment_id,
            detail=(
                f"fix iteration {fix.review_iteration} >= "
                f"max_review_iterations {max_iter}; "
                "not dispatching another review"
            ),
        )]

    review = dispatch_review(fix, board, config)

    if review is None:
        log.warning(
            "auto_loop: dispatch_review returned None for fix %s "
            "(no capable machine or dedup check rejected the dispatch)",
            assignment_id,
        )
        return []

    fix.review_state = "dispatched"
    write_board(board)

    log.info(
        "auto_loop: dispatched re-review %s for fix worker %s (iteration %d/%d)",
        review.assignment_id, assignment_id, fix.review_iteration, max_iter,
    )
    return [LoopAction(
        kind="review_dispatched",
        assignment_id=assignment_id,
        detail=(
            f"re-review {review.assignment_id} dispatched to "
            f"{review.machine_name} (fix iteration {fix.review_iteration}/"
            f"{max_iter})"
        ),
    )]


def run_for_review_transition(
    assignment_id: str,
    record: dict,
    entry: dict,
    config: Config,
    *,
    terminal_cache: dict | None = None,
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

    # #749: read_board() routes through the daemon when board_service is
    # configured, instead of always hitting the local DB directly.
    board = read_board()

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
    # #fix-cli: include the agent's host so the auto-loop can fall back
    # to HTTP /logs/<id> when the local log isn't on this filesystem
    # (the gap that left quadraui#166 without a fix dispatch).
    machine_host: str | None = None
    machine_name = record.get("machine_name")
    if machine_name:
        machine = next((m for m in config.machines if m.name == machine_name), None)
        if machine is not None and machine.host:
            machine_host = machine.host
    actions = process_review_completion(
        review,
        board,
        config,
        log_path=log_path,
        machine_host=machine_host,
        terminal_cache=terminal_cache,
    )

    # Save when a fix was dispatched (new assignment), an approve was parsed
    # (so review_verdict is persisted for the merge gate, #253), an
    # advisory-only review advanced the pipeline (#476 — review_verdict flips to
    # approve + review_state="done" so the merge gate unblocks; without this the
    # gate suppresses the fix but the advance is never persisted and the PR
    # silently can't merge), or the work was found terminal (#522).
    _persist_kinds = ("fix_dispatched", "approved", "approved_with_nits", "terminal_skip")
    if any(a.kind in _persist_kinds for a in actions):
        write_board(board)

    return actions
