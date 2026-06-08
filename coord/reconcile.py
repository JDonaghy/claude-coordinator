"""Reconcile the coordinator's board with live agent server state."""

from __future__ import annotations

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
    from coord.review import dispatch_review
    from coord.claim import has_active_work_followup

    # #200: gate review auto-dispatch on the Test stage verdict. If the pipeline
    # includes a "test" gate, hold off on review until the user records a
    # passed/skipped verdict (test_state). A failed verdict blocks review until
    # the user redispatches Work to produce a new candidate.
    test_gate_active = "test" in (config.pipeline.default_gates or [])

    for completed in board.completed:
        # Treat NULL the same as "pending" — a done-work assignment whose
        # review_state was never set (e.g. because the done-transition was
        # picked up by notify rather than reconcile) is still un-reviewed
        # and should be dispatched.  Without this, work that reaches "done"
        # outside this loop's transition path stays forever un-reviewed.
        if completed.review_state not in (None, "pending"):
            continue
        # Only work assignments get reviewed.
        if completed.type != "work":
            continue
        if test_gate_active and completed.test_state not in ("passed", "skipped"):
            # Either no verdict yet, or verdict was "failed" — either way the
            # work is not ready for review. The next reconcile pass will re-check.
            continue
        # #459: skip review dispatch when a work or conflict-fix is actively
        # rewriting this issue's branch (e.g. a coord-bounce fix iteration).
        # Reviewing stale code now produces a verdict on code that's about to
        # change. Leave review_state as "pending" so the next reconcile pass
        # retries once the active fix finishes.
        if has_active_work_followup(
            board,
            repo_name=completed.repo_name,
            issue_number=completed.issue_number,
        ):
            continue
        review = dispatch_review(completed, board, config)
        if review is not None:
            completed.review_state = "dispatched"
            if review.assignment_id is not None:
                changed.append(review.assignment_id)
        # If review is None (auto_dispatch off, machine unreachable, no branch,
        # etc.) leave review_state as "pending" so the next reconcile retries.

    # Auto-queue smoke tests for any work assignments that just finished.
    # Independent of review dispatch — both can fire for the same completion.
    smoke_cfg = getattr(config, "smoke_tests", None)
    if smoke_cfg is not None and smoke_cfg.auto_queue:
        from coord.smoke import dispatch_smoke

        for completed in newly_done_work:
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
