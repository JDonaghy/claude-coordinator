"""Reconcile the coordinator's board with live agent server state."""

from __future__ import annotations

import re
import time
import uuid

import httpx

from typing import TYPE_CHECKING

from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.models import Assignment, Board

if TYPE_CHECKING:
    from coord.merge_queue import QueuedMerge


def _query_agent(host: str, port: int = AGENT_PORT, timeout: float = 5.0) -> dict | None:
    try:
        resp = httpx.get(f"http://{host}:{port}/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None


# Terminal statuses an agent reports in its /status `completed` history,
# mapped to the board terminal status we persist. (#625)
_AGENT_TERMINAL_STATUS = {
    "done": "done",
    "advisory": "advisory",
    "failed": "failed",
    "cancelled": "failed",
}


def reconcile_completed_assignments(
    config: Config,
    *,
    board: Board | None = None,
    agent_status_fn=_query_agent,
    update_state_fn=None,
    capture_plan: bool = True,
) -> list[dict]:
    """Dispatch-free passive completion reconcile (#625).

    Poll the agent of every RUNNING board assignment; for any the agent
    reports terminal in its ``/status`` ``completed`` history, write the
    terminal status + ``finished_at`` to the board via the issue_store seam
    and (best-effort) capture a plan's structured output.  This reflects a
    headless worker's already-finished state so the board — and the TUI box
    colour — stops lying when the auto-loop (the only other thing that polled
    agents) is turned off.

    Deliberately minimal — it is the WHOLE point of #625 that reflecting a
    termination is *passive* state, decoupled from auto-dispatch so it can
    never re-introduce the dispatch flood:

    * NEVER dispatches work/review.
    * NEVER posts a GitHub comment (the single completion/plan comment is left
      to an explicit ``coord notify``; this only writes board state).
    * Only acts on ``status == "running"`` rows, so it is idempotent — once a
      row is flipped terminal a later tick skips it.

    Interactive sessions are tmux launches, not agent subprocesses, so they
    never appear in the agent's ``completed`` list — a live attended session
    can't be reaped by this path.

    Returns one dict per reconciled assignment (empty when nothing changed).
    """
    if update_state_fn is None:
        from coord.issue_store import _update_local_state  # noqa: PLC0415

        update_state_fn = _update_local_state

    if board is None:
        from coord.state import build_board  # noqa: PLC0415

        board = build_board()

    running = [a for a in board.active if a.status == "running"]
    if not running:
        return []

    hosts = {m.name: m.host for m in config.machines}
    status_by_host: dict[str, dict | None] = {}  # poll each agent at most once
    reconciled: list[dict] = []

    for a in running:
        aid = a.assignment_id
        if not aid:
            continue
        host = hosts.get(a.machine_name)
        if not host:
            continue
        if host not in status_by_host:
            status_by_host[host] = agent_status_fn(host)
        status = status_by_host[host]
        if not status:
            continue  # agent unreachable → leave the row, retry next tick
        entry = next(
            (e for e in status.get("completed", []) if e.get("id") == aid),
            None,
        )
        if entry is None:
            continue  # still active on the agent (or rolled off history) → leave it
        terminal = _AGENT_TERMINAL_STATUS.get((entry.get("status") or "").lower())
        if terminal is None:
            continue

        update_state_fn(
            assignment_id=aid,
            terminal_status=terminal,
            branch=a.branch,
            review_state=None,
        )

        # #666 Gap A: best-effort cost/token capture from the agent completed
        # entry.  Must never raise — a tick crash breaks the daemon.
        _capture_cost_from_entry_best_effort(aid, entry)

        plan_captured = (
            _capture_plan_best_effort(host, aid)
            if capture_plan and a.type == "plan"
            else False
        )

        # #667: capture token counts from the /status entry (the agent now
        # includes them there after parsing its own log).  Best-effort — any
        # failure is swallowed so it can't break the reconcile.
        _capture_tokens_best_effort(aid, entry)

        reconciled.append(
            {
                "assignment_id": aid,
                "issue_number": a.issue_number,
                "repo": a.repo_name,
                "type": a.type,
                "to_status": terminal,
                "plan_captured": plan_captured,
            }
        )

    return reconciled


def _capture_plan_best_effort(host: str, assignment_id: str) -> bool:
    """Fetch + persist a plan's structured output from the agent log so the
    TUI's plan detail panel isn't empty after a passive reconcile.  Best
    effort: any failure is swallowed — the terminal-status write already
    landed and is what fixes the stuck box."""
    try:
        from coord.plan_parser import parse_plan_from_agent  # noqa: PLC0415
        from coord.state import save_plan  # noqa: PLC0415

        plan = parse_plan_from_agent(host, assignment_id)
        if plan is None or plan.is_empty():
            return False
        save_plan(assignment_id, plan.to_dict())
        return True
    except Exception:  # noqa: BLE001 — never let plan capture break the reconcile
        return False


def _capture_cost_from_entry_best_effort(assignment_id: str, entry: dict) -> None:
    """#666 Gap A: capture cost from an agent ``completed`` entry when flipping
    a row terminal.

    Best-effort and silent — any exception is swallowed so a cost-capture
    failure never crashes the daemon's reconcile tick.

    Cost source: ``total_cost_usd`` (full-log parse, available when the agent
    serves terminal entries) with ``cost_so_far`` as a fallback.  Either is
    used only when present and > 0 so an un-measured session isn't written as 0.

    Token counts are captured separately by ``_capture_tokens_best_effort``
    (#667 Gap B), which is called at the same call site.
    """
    try:
        from coord.state import update_assignment_cost  # noqa: PLC0415

        raw_cost = entry.get("total_cost_usd") or entry.get("cost_so_far")
        if raw_cost is not None:
            try:
                cost = float(raw_cost)
            except (TypeError, ValueError):
                cost = None
            else:
                if cost > 0:
                    update_assignment_cost(assignment_id, cost)
    except Exception:  # noqa: BLE001 — never let cost capture break the reconcile
        pass


def _capture_tokens_best_effort(assignment_id: str, entry: dict) -> None:
    """#667: persist token counts from a /status completed entry.

    The agent now parses its own log and includes
    ``input_tokens`` / ``output_tokens`` / ``cache_creation_tokens`` /
    ``cache_read_tokens`` in the completed entry.  We write them to the DB
    here so a passive reconcile also captures tokens (not just cost).
    Best-effort — any failure is swallowed.
    """
    try:
        input_tokens = int(entry.get("input_tokens") or 0)
        output_tokens = int(entry.get("output_tokens") or 0)
        cache_creation_tokens = int(entry.get("cache_creation_tokens") or 0)
        cache_read_tokens = int(entry.get("cache_read_tokens") or 0)
        if input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens == 0:
            return
        from coord.state import update_assignment_tokens  # noqa: PLC0415

        update_assignment_tokens(
            assignment_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
        )
    except Exception:  # noqa: BLE001 — never let token capture break the reconcile
        pass


def _reassign(
    failed: Assignment, board: Board, config: Config,
    *,
    model: str | None = None,
) -> Assignment | None:
    """Re-dispatch a failed assignment to an idle different machine.

    *model* overrides the model tier on the retry. When None, the
    original assignment's model is reused (escalation happens at the call
    site).
    """
    from coord.machine_pause import paused_set
    paused = paused_set()
    busy = {a.machine_name for a in board.active if a.status == "running"}
    candidates = [
        m for m in config.machines
        if m.can_work_on(failed.repo_name)
        and m.repo_path(failed.repo_name) is not None
        and m.name not in busy
        and m.name != failed.machine_name
        and m.name not in paused
    ]
    if not candidates:
        # Fall back to including the same machine that failed last time —
        # paused machines stay excluded even from the fallback.
        candidates = [
            m for m in config.machines
            if m.can_work_on(failed.repo_name)
            and m.repo_path(failed.repo_name) is not None
            and m.name not in busy
            and m.name not in paused
        ]
    if not candidates:
        return None

    machine = candidates[0]
    repo_path = machine.repo_path(failed.repo_name)

    # #437: STRUCTURAL TOS-COMPLIANCE GATE — auto-reassign is an
    # unattended dispatch path; refuse to retry through a provider that
    # opts out of unattended use.  Resolve precedence with per-repo
    # override and the global default (the failed assignment doesn't
    # carry a spec-level provider into this path).  On refusal: skip the
    # reassignment — the failed assignment stays failed for human
    # attention rather than getting silently re-tried on the wrong
    # provider.
    from coord.providers import guard_unattended_dispatch  # noqa: PLC0415
    repo_for_provider = config.repo(failed.repo_name)
    try:
        guard_unattended_dispatch(
            spec_provider=None,
            repo_provider=(
                repo_for_provider.provider
                if repo_for_provider is not None
                else None
            ),
            providers_cfg=config.providers,
            models_cfg=config.models,
            where="auto-reassign (reconcile)",
        )
    except ValueError:
        return None

    retry_model = model if model is not None else failed.model
    # The Assignment keeps the alias for legibility; the wire payload is
    # resolved through models.versions when an exact id is pinned.
    retry_model_wire = config.models.resolve(retry_model)

    repo_cfg = config.repo(failed.repo_name)
    payload = {
        "repo_name": failed.repo_name,
        "repo_path": repo_path,
        "issue_number": failed.issue_number,
        "issue_title": f"[retry] {failed.issue_title}",
        "briefing": failed.briefing,
        "files_allowed": failed.files_allowed,
        "files_forbidden": failed.files_forbidden,
        "pull_repos": [],
        "type": "work",
        "model": retry_model_wire,
        # #255: retry inherits the repo's configured default branch as the
        # worker's integration base.
        "branch": (repo_cfg.default_branch if repo_cfg is not None else None) or "main",
    }

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    try:
        resp = httpx.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    retry_assignment = Assignment(
        machine_name=machine.name,
        repo_name=failed.repo_name,
        issue_number=failed.issue_number,
        issue_title=f"[retry] {failed.issue_title}",
        files_allowed=failed.files_allowed,
        files_forbidden=failed.files_forbidden,
        briefing=failed.briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        dispatched_at=time.time(),
        type="work",
        model=retry_model,
    )
    board.active.append(retry_assignment)

    from coord.state import record_dispatched_assignment
    repo = config.repo(failed.repo_name)
    if repo is not None:
        record_dispatched_assignment(
            assignment=retry_assignment,
            repo_github=repo.github,
        )

    return retry_assignment


def reconcile(board: Board, config: Config) -> list[str]:
    """Poll agent servers and update board assignments that have finished.

    Returns assignment_ids whose status changed or were backfilled.
    """
    machines_by_name = {m.name: m for m in config.machines}

    # Collect all machines we need to query: those with active assignments
    # OR completed assignments missing branch info.
    machines_to_query: set[str] = set()
    for a in board.active:
        machines_to_query.add(a.machine_name)
    for a in board.completed:
        if a.branch is None and a.assignment_id is not None:
            machines_to_query.add(a.machine_name)

    # Query each machine once and cache the result.
    agent_completed: dict[str, dict] = {}
    reachable_machines: set[str] = set()
    for machine_name in machines_to_query:
        machine = machines_by_name.get(machine_name)
        if machine is None:
            continue
        status = _query_agent(machine.host)
        if status is None:
            continue
        reachable_machines.add(machine_name)
        for e in status.get("completed", []):
            agent_completed[e["id"]] = e

    changed: list[str] = []
    newly_done_work: list = []  # assignments that just transitioned work → done
    newly_failed: list = []  # assignments that just transitioned to failed

    # Sweep for dead interactive (--interactive / claude-pty) sessions before
    # processing agent-based assignments.  A killed tmux session leaves a
    # stale "running" board row + orphaned worktree that blocks relaunch.
    # Reaping here ensures ``coord resume`` / ``coord notify`` clean up
    # without requiring the user to first run ``coord reattach``.
    from coord.interactive import (  # noqa: PLC0415
        reap_stale_interactive_sessions,
        reap_stale_remote_interactive_sessions,
    )

    reaped = reap_stale_interactive_sessions(board, config)
    changed.extend(reaped)

    # #588: probe remote claude-pty sessions older than the configured timeout
    # threshold.  The local reaper above skips these; this sweep SSHes to the
    # remote host and finalizes sessions whose tmux has exited.
    remote_reaped = reap_stale_remote_interactive_sessions(board, config)
    changed.extend(remote_reaped)

    # Pass 1: transition active assignments that have finished.
    for a in board.active[:]:
        if a.assignment_id is None:
            continue

        # Track unreachable agents for stale detection
        if a.machine_name in machines_to_query and a.machine_name not in reachable_machines:
            a.unreachable_count = getattr(a, "unreachable_count", 0) + 1
            stale_threshold = getattr(config.concurrency, "stale_threshold", 3)
            if a.unreachable_count >= stale_threshold:
                board.mark_failed_by_id(a.assignment_id)
                newly_failed.append(a)
                changed.append(a.assignment_id)
            continue
        elif a.machine_name in reachable_machines:
            a.unreachable_count = 0

        entry = agent_completed.get(a.assignment_id)
        if entry is None:
            continue
        branch = entry.get("branch")
        if entry.get("status") == "done":
            done = board.mark_done_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
                branch=branch,
            )
            if done is not None:
                if done.type == "work":
                    # Always mark work completions as pending review so the
                    # dispatch loop below (and future reconcile passes) can
                    # pick them up reliably.
                    done.review_state = "pending"
                    newly_done_work.append(done)
                elif done.type == "review":
                    # A review finished — update the original work assignment.
                    orig_id = done.review_of_assignment_id
                    if orig_id:
                        orig = board.find_by_id(orig_id)
                        if orig is not None:
                            orig.review_state = "done"
                elif done.type == "conflict-fix":
                    # #241: re-enqueue the parent merge entry for retry.
                    _on_conflict_fix_done(done, succeeded=True)
        elif entry.get("status") == "advisory":
            # #448: worker exited cleanly but pushed 0 commits. Move to
            # completed with status "advisory" — NOT "failed" — so that
            # auto_reassign does not loop on it. Review is also skipped
            # because there is no code to review on the branch.
            done = board.mark_done_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
                branch=branch,
            )
            if done is not None:
                # mark_done_by_id sets status="done"; correct it to "advisory".
                done.status = "advisory"
                if done.type == "work":
                    # No code pushed → nothing to review. Set review_state to
                    # "advisory" so the review-dispatch loop skips this entry.
                    done.review_state = "advisory"
                elif done.type == "review":
                    # Defensive (should not occur after Bug 2 fix): review
                    # workers that somehow hit advisory still advance the
                    # original work assignment's review_state.
                    orig_id = done.review_of_assignment_id
                    if orig_id:
                        orig = board.find_by_id(orig_id)
                        if orig is not None:
                            orig.review_state = "done"
                elif done.type == "conflict-fix":
                    # A conflict-fix with 0 commits didn't resolve anything.
                    _on_conflict_fix_done(done, succeeded=False)
            # NOTE: do NOT add to newly_failed — prevents auto_reassign loop.
        else:
            # Defensive: don't downgrade a DB-done assignment to failed when
            # the agent reports cancelled (e.g. after POST /cancel cleanup
            # of a hung reap). The work succeeded; cancellation here is
            # bookkeeping noise.
            if (entry.get("status") == "cancelled"
                    and (a.status or "").lower() == "done"):
                continue
            failed = board.mark_failed_by_id(
                a.assignment_id,
                finished_at=entry.get("finished_at"),
            )
            if failed is not None:
                newly_failed.append(failed)
                if failed.type == "conflict-fix":
                    # #241: the auto-fix didn't work — escalate to the user.
                    _on_conflict_fix_done(failed, succeeded=False)
        changed.append(a.assignment_id)

    # Dispatch pending reviews for all completed work assignments.
    # We iterate board.completed (not just newly-done) so that a failed
    # dispatch on a previous reconcile pass is retried here automatically.
    #
    # #465: review fires immediately on work completion — no manual smoke
    # prerequisite (the interactive smoke gate now lives on merge).
    # dispatch_pending_reviews() bounds this with a per-pass cap + surge gate
    # (flood guard, incident 2026-06-08) and applies the #459 active-fix
    # dedupe, so a backlog unmasking can't flood metered reviews.
    from coord.review import dispatch_pending_reviews

    for review in dispatch_pending_reviews(board, config):
        if review.assignment_id is not None:
            changed.append(review.assignment_id)

    # Auto-queue smoke tests for any work assignments that just finished.
    # Independent of review dispatch — both can fire for the same completion.
    #
    # #685: per-issue test-mode policy gates auto-smoke dispatch.
    #   test-mode:auto  → headless smoke (auto-dispatch here, current behaviour).
    #   test-mode:smoke → skip; the TUI will offer the interactive smoke agent.
    #   no label        → no policy set (pre-#685 dispatch) → respect auto_queue
    #                     as before (backward-compatible).
    smoke_cfg = getattr(config, "smoke_tests", None)
    if smoke_cfg is not None and smoke_cfg.auto_queue:
        from coord.smoke import dispatch_smoke
        from coord.state import get_issue_test_mode

        for completed in newly_done_work:
            test_mode = get_issue_test_mode(completed.repo_name, completed.issue_number)
            if test_mode == "smoke":
                # Interactive-smoke mode: the TUI raises the --smoke-of offer;
                # don't auto-dispatch here.
                continue
            smoke = dispatch_smoke(completed, board, config)
            if smoke is not None and smoke.assignment_id is not None:
                changed.append(smoke.assignment_id)

    # Auto-reassign failed work assignments to a different machine.
    if newly_failed and getattr(config.concurrency, "auto_reassign", False):
        for failed_a in newly_failed:
            if getattr(failed_a, "type", "work") != "work":
                continue
            reassigned = _reassign(failed_a, board, config)
            if reassigned is not None and reassigned.assignment_id is not None:
                changed.append(reassigned.assignment_id)

    # Pass 2: backfill branch on completed assignments that are missing it.
    for a in board.completed:
        if a.branch is not None or a.assignment_id is None:
            continue
        entry = agent_completed.get(a.assignment_id)
        if entry is None:
            continue
        branch = entry.get("branch")
        if branch:
            a.branch = branch
            changed.append(a.assignment_id)

    return changed


def _post_human_required_comment_raw(
    entry: QueuedMerge,
    fix_assignment_id: str,
    machine_name: str,
) -> None:
    """Notify the user on GitHub that a conflict-fix worker gave up."""
    from coord import github_ops  # noqa: PLC0415

    body = (
        "## Conflict-fix worker could not auto-resolve\n\n"
        f"Worker `{fix_assignment_id}` on "
        f"`{machine_name}` attempted to rebase "
        f"`{entry.branch}` onto `{entry.target_branch}` and exited "
        "non-zero. The merge queue entry is now `HUMAN_REQUIRED`.\n\n"
        f"**Last error:** `{entry.error or 'unknown'}`\n\n"
        "Manual resolution required: rebase the branch locally and "
        "`git push --force-with-lease`, then re-run `coord merge`. The "
        "coordinator will not re-dispatch a conflict-fix for this entry "
        "in the current session."
    )
    try:
        github_ops.post_issue_comment(entry.repo_github, entry.issue_number, body)
    except Exception as exc:  # noqa: BLE001 — best-effort notification
        import logging  # noqa: PLC0415
        logging.warning(
            "could not post HUMAN_REQUIRED comment on %s#%d: %s",
            entry.repo_github, entry.issue_number, exc,
        )


def on_conflict_fix_done(
    *,
    parent_assignment_id: str,
    fix_assignment_id: str,
    machine_name: str,
    succeeded: bool,
) -> None:
    """Update the parent merge entry after a conflict-fix worker finishes.

    On *succeeded*: the merge entry is reset to PENDING so the next
    ``coord merge`` retries.  On failure: marked HUMAN_REQUIRED so the TUI
    can surface "manual resolution required", and a comment is posted on
    the underlying issue so the user is notified outside the TUI too.

    Called from both ``reconcile()`` (via mark_done/failed) and
    ``coord notify`` (via post_transition) — both paths must trigger this
    so the re-enqueue fires regardless of which polling command runs first.
    """
    from coord import merge_queue as mq  # noqa: PLC0415

    items = mq.load_queue()
    changed = False
    failed_entry: mq.QueuedMerge | None = None
    for entry in items:
        if entry.assignment_id != parent_assignment_id:
            continue
        if succeeded:
            entry.state = mq.PENDING
            entry.error = None
            entry.last_attempt = None
        else:
            entry.state = mq.HUMAN_REQUIRED
            existing_error = entry.error or "conflict-fix failed"
            entry.error = (
                f"{existing_error}; conflict-fix worker did not resolve. "
                "Manual rebase required."
            )
            failed_entry = entry
        changed = True
    if changed:
        mq.save_queue(items)

    if failed_entry is not None:
        _post_human_required_comment_raw(
            entry=failed_entry,
            fix_assignment_id=fix_assignment_id,
            machine_name=machine_name,
        )


def _on_conflict_fix_done(fix_assignment: Assignment, *, succeeded: bool) -> None:
    """Thin wrapper used by the reconcile() loop."""
    parent_id = fix_assignment.review_of_assignment_id
    if not parent_id:
        return
    on_conflict_fix_done(
        parent_assignment_id=parent_id,
        fix_assignment_id=fix_assignment.assignment_id or "",
        machine_name=fix_assignment.machine_name or "",
        succeeded=succeeded,
    )


def _extract_issue_number(branch: str) -> int | None:
    """Extract N from ``issue-{N}-*`` branch names; returns None if no match."""
    m = re.match(r"issue-(\d+)-", branch)
    return int(m.group(1)) if m else None


def close_stale_prs(
    config: Config,
    *,
    repo: str | None = None,
    issue: int | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Close open PRs whose work is already on main or whose issue is closed.

    Sweeps every coord-tracked repo (filtered by *repo* / *issue* when given)
    for OPEN PRs with ``issue-{N}-*`` head branches.  Each PR is classified as
    stale when either condition holds:

      1. The linked issue N is CLOSED on GitHub.
      2. The branch has 0 commits ahead of the repo's default branch (catches
         fast-forward merges; squash/rebase cases are caught by condition 1
         because coord closes the issue when squash-merging).

    Stale PRs are closed with an explanatory comment.  Non-stale PRs are left
    untouched.  *dry_run* lists what would change without writing.  Idempotent.
    """
    from coord import github_ops  # noqa: PLC0415

    actions: list[str] = []

    for repo_cfg in config.repos:
        if repo is not None and repo_cfg.name != repo:
            continue

        try:
            open_prs = github_ops.list_open_prs(repo_cfg.github)
        except Exception as exc:  # noqa: BLE001
            actions.append(
                f"skip stale-PR sweep for {repo_cfg.name}: could not list PRs ({exc})"
            )
            continue

        default_branch = repo_cfg.default_branch or "main"

        for pr in open_prs:
            branch = pr.get("headRefName") or ""
            pr_number = pr.get("number")
            if not branch or pr_number is None:
                continue

            issue_number = _extract_issue_number(branch)
            if issue_number is None:
                continue  # not a coord-managed branch — skip
            if issue is not None and issue_number != issue:
                continue

            # Fail-safe classification: when uncertain, leave the PR open.
            stale_reason: str | None = None

            if github_ops.issue_is_closed(repo_cfg.github, issue_number):
                stale_reason = f"issue #{issue_number} is closed"
            elif github_ops.branch_is_fully_merged(
                repo_cfg.github, branch, default_branch
            ):
                stale_reason = f"all commits already on {default_branch}"

            if stale_reason is None:
                continue  # live PR — leave it alone

            actions.append(
                f"close PR #{pr_number} "
                f"({repo_cfg.name} #{issue_number}, {branch}): {stale_reason}"
                + (" [dry-run]" if dry_run else "")
            )

            if not dry_run:
                comment = (
                    f"Closing stale PR — {stale_reason}. "
                    f"The work for issue #{issue_number} has already landed.\n\n"
                    f"<!-- coord:stale-close issue={issue_number} -->"
                )
                try:
                    github_ops.close_pr(repo_cfg.github, pr_number, comment=comment)
                except Exception as exc:  # noqa: BLE001
                    actions.append(f"  ↳ error closing PR #{pr_number}: {exc}")

    return actions


def reconcile_board_merges(
    board: Board,
    config: Config,
    *,
    repo: str | None = None,
    issue: int | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Reconcile done work assignments against git/GitHub reality.

    Two conservative sweeps over ``type='work'`` ``status='done'`` assignments,
    returning a list of human-readable action (and skip) strings:

    (a) #611 branch backfill — a remote interactive work session can finish
        ``status=done`` with ``branch=None`` even though it pushed
        ``issue-{N}-*`` to origin, which greys the TUI Start review/test/merge
        buttons (they require a done work assignment WITH a branch).  When
        exactly one remote branch matches ``issue-{N}-*`` for the issue, the
        branch is backfilled via :func:`state.update_assignment_branch`.  More
        than one candidate (or none) is left untouched and logged.

    (b) #609 record out-of-band merges — work merged directly on GitHub, or a
        ``merge_queue`` row that drained without flipping the board, is never
        recorded as ``status='merged'`` so the TUI shows a grey merge box
        forever.  When :func:`github_ops.work_is_terminal` reports the branch
        merged (PR merged OR issue closed, fail-open), the row is flipped via
        :func:`state.mark_assignment_merged`.

    Both sweeps are **conservative**: they never act when uncertain and append a
    skip reason instead.  *repo* filters to a single local repo name.  When
    *dry_run* is True no writes happen (no ``state.update_*`` calls) — the
    actions list still describes what *would* change.  The board objects are
    mutated in place on a real run so a subsequent ``save_board`` agrees with
    the targeted DB writes.
    """
    from coord import github_ops, state  # noqa: PLC0415

    actions: list[str] = []
    terminal_cache: dict = {}
    # One remote-branch listing per repo, fetched lazily and reused.
    branches_by_repo: dict[str, set[str]] = {}

    candidates = [
        a
        for a in board.active + board.completed
        if a.type == "work"
        and a.status == "done"
        and (repo is None or a.repo_name == repo)
        and (issue is None or a.issue_number == issue)
    ]

    for a in candidates:
        repo_cfg = config.repo(a.repo_name)
        if repo_cfg is None:
            actions.append(
                f"skip {a.assignment_id} ({a.repo_name} #{a.issue_number}): "
                "repo not in config"
            )
            continue

        # (a) #611 — backfill a missing branch from origin.
        if not a.branch:
            if repo_cfg.github not in branches_by_repo:
                branches_by_repo[repo_cfg.github] = (
                    github_ops.list_remote_branch_names(repo_cfg.github)
                )
            prefix = f"issue-{a.issue_number}-"
            matches = sorted(
                name
                for name in branches_by_repo[repo_cfg.github]
                if name.startswith(prefix)
            )
            if len(matches) == 1:
                branch = matches[0]
                actions.append(
                    f"backfill branch {a.assignment_id} "
                    f"({a.repo_name} #{a.issue_number}) -> {branch}"
                    + (" [dry-run]" if dry_run else "")
                )
                if not dry_run:
                    a.branch = branch
                    state.update_assignment_branch(a.assignment_id or "", branch)
            elif len(matches) > 1:
                actions.append(
                    f"skip backfill {a.assignment_id} "
                    f"({a.repo_name} #{a.issue_number}): "
                    f"{len(matches)} ambiguous branch candidates {matches}"
                )
                continue
            else:
                actions.append(
                    f"skip backfill {a.assignment_id} "
                    f"({a.repo_name} #{a.issue_number}): "
                    f"no remote branch matching {prefix}*"
                )
                continue

        # (b) #609 — flip done work whose branch is merged on GitHub.
        if not a.branch:
            # Still no branch even after the backfill attempt — can't determine
            # merge state without one, so leave it for the next sweep.
            continue
        if github_ops.work_is_terminal(
            repo_cfg.github, a.issue_number, a.branch, cache=terminal_cache
        ):
            actions.append(
                f"mark merged {a.assignment_id} "
                f"({a.repo_name} #{a.issue_number}, {a.branch})"
                + (" [dry-run]" if dry_run else "")
            )
            if not dry_run:
                a.status = "merged"
                state.mark_assignment_merged(a.assignment_id or "")

    # (c) #721 — close open PRs whose work has already landed.
    actions.extend(close_stale_prs(config, repo=repo, issue=issue, dry_run=dry_run))

    # (d) #732 — prune stale merge_queue entries for closed issues / merged PRs.
    # Runs after the board sweeps so a just-marked-merged assignment doesn't
    # also appear as a pruned queue entry in the same reconcile run.
    # repo/issue filters don't apply here — we always scan the full queue, since
    # a stale entry affects every `coord merge` run regardless of --repo.
    from coord import merge_queue as mq  # noqa: PLC0415

    pruned = mq.prune_stale_queue_entries(dry_run=dry_run)
    for entry in pruned:
        actions.append(
            f"prune queue entry {entry.assignment_id} "
            f"({entry.repo_name} #{entry.issue_number}, state={entry.state})"
            + (" [dry-run]" if dry_run else "")
        )

    # (e) #894 — settle sibling ghost rows for terminal issues.
    #
    # The #609 sweep (b) only processes type='work' status='done' rows, so it
    # misses two classes of lingering ghost rows for already-merged/closed issues:
    #
    #   * type=review/smoke/conflict-fix rows whose status='done' but
    #     review_state='pending' — the interactive-completion path
    #     (issue_store._update_local_state) sets review_state='pending' on ALL
    #     completed assignments so reconcile picks them up like claude -p workers.
    #     When the parent issue closes before that handoff fires, these rows
    #     surface as "awaiting review" in coord status / the TUI forever.
    #
    #   * status='advisory' rows (any type) — the #609 candidates filter requires
    #     status='done', so advisory rows are never reached.  They linger in the
    #     TUI's advisory view after the issue is terminal.
    #
    # This sweep is conservative and fail-open:
    #   - Only acts when work_is_terminal(...) is confirmed true.
    #   - Uses the terminal_cache populated by sweep (b) to avoid extra GH calls;
    #     falls back to a fresh check (still fail-open) for ghost rows whose issue
    #     wasn't processed in sweep (b) (e.g. work already merged in a prior run).
    #   - Respects the repo/issue filter so --repo/--issue scopes apply.
    #   - Terminality is keyed on issue_is_closed OR pr_is_merged — NOT branch
    #     ancestry, so rebase/squash merges with new SHAs are correctly handled.

    # Build a (repo_name, issue_number) → branch lookup from all work rows so
    # that sibling rows lacking a branch can still pass a branch to work_is_terminal
    # (enabling the pr_is_merged fast-path in addition to issue_is_closed).
    work_branch_for: dict[tuple[str, int], str | None] = {}
    for _a in board.active + board.completed:
        if _a.type == "work" and _a.issue_number is not None:
            key = (_a.repo_name, _a.issue_number)
            # Prefer a non-None branch; first seen wins (done rows come before
            # merged rows in board.completed, but any non-None branch is fine).
            if key not in work_branch_for or work_branch_for[key] is None:
                work_branch_for[key] = _a.branch

    # Identify ghost sibling rows subject to this sweep.
    ghost_candidates = [
        a
        for a in board.active + board.completed
        if (
            (
                a.type in ("review", "smoke", "conflict-fix")
                and a.status == "done"
                and a.review_state == "pending"
            )
            or a.status == "advisory"
        )
        and (repo is None or a.repo_name == repo)
        and (issue is None or a.issue_number == issue)
    ]

    for a in ghost_candidates:
        repo_cfg = config.repo(a.repo_name)
        if repo_cfg is None:
            actions.append(
                f"skip settle {a.assignment_id} "
                f"({a.repo_name} #{a.issue_number}): repo not in config"
            )
            continue

        # Resolve the best available branch for the terminality probe.  The
        # sibling row itself may carry a branch; fall back to the work row's
        # branch so the pr_is_merged check fires even when the sibling has none.
        branch = a.branch or work_branch_for.get((a.repo_name, a.issue_number))

        if not github_ops.work_is_terminal(
            repo_cfg.github, a.issue_number, branch, cache=terminal_cache
        ):
            continue  # Issue still live — leave this row alone.

        if a.status == "advisory":
            actions.append(
                f"settle advisory {a.assignment_id} "
                f"({a.repo_name} #{a.issue_number})"
                + (" [dry-run]" if dry_run else "")
            )
            if not dry_run:
                a.status = "merged"
                state.mark_advisory_settled(a.assignment_id or "")
        else:
            # type=review/smoke/conflict-fix, status=done, review_state=pending
            actions.append(
                f"settle sibling {a.assignment_id} "
                f"({a.repo_name} #{a.issue_number}, type={a.type})"
                + (" [dry-run]" if dry_run else "")
            )
            if not dry_run:
                a.review_state = "done"
                state.mark_sibling_review_done(a.assignment_id or "")

    return actions
