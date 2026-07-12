"""Per-stage "doctor": diagnose a pipeline stage, best-effort recover, and —
when recovery isn't possible — offer a non-destructive reset.

Pipeline stages routinely get into bad DB states with no clean UI recovery:
phantom ``running`` rows (board says running, no live session — #366), reviews
whose findings were silently dropped (#607), stale-but-live detached sessions
days old (#494/#370/#546), merged-but-grey boxes, orphaned worktrees.  This
module is the orchestration the TUI's "Diagnose & fix stage" action and the
``coord diagnose`` command call; it *composes* existing primitives rather than
reinventing them:

* :func:`coord.interactive.finalize_interactive_exit` — record a terminal state
  for a dead/phantom session (pushes commits, releases claim, prunes worktree).
* :func:`coord.interactive._review_findings_from_transcript` — the #617
  remote-aware transcript-floor that recovers a review's verdict + findings from
  the session's own host.
* :func:`coord.reconcile.reconcile_board_merges` — flip merged-but-grey work and
  backfill missing branches.

Design decisions (locked with the operator):

* **Reset is non-destructive**: it clears the stage's board rows, releases the
  claim, removes the orphaned worktree, and stops a live session — but NEVER
  deletes the feature branch.  ``origin/issue-<N>-*`` and its commits are
  preserved, so the stage re-dispatches fresh with the work intact.  (There is
  deliberately no branch-deletion code path in this module.)
* **Cleanup is scoped to the one issue**, not a fleet-wide sweep.

The side-effecting steps are factored into small module-level helpers so the
orchestration in :func:`diagnose_stage` is unit-testable by monkeypatching them.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from coord.config import Config
    from coord.models import Assignment, Board

# Stages the doctor understands.  Each maps to the assignment ``type`` that
# carries its state; ``test`` and ``merge`` are tracked on the *work* row
# (``test_state`` / ``status='merged'`` + the merge queue) rather than a
# dedicated assignment type.
STAGE_ASSIGNMENT_TYPES: dict[str, tuple[str, ...]] = {
    "plan": ("plan",),
    "work": ("work", "plan"),
    "review": ("review",),
    "test": ("work", "plan"),
    "merge": ("work", "plan", "merge"),
}


@dataclass
class DiagnoseResult:
    """Outcome of a diagnose/recover/reset run for one stage of one issue."""

    repo_name: str
    issue_number: int
    stage: str
    findings: list[str] = field(default_factory=list)
    actions_taken: list[str] = field(default_factory=list)
    # True when the stage is healthy after this run (nothing was wrong, or the
    # problem was recovered).  False + needs_reset=True means "still wedged".
    recovered: bool = False
    # True when best-effort recovery could not clear the problem and the only
    # remaining option is a reset.
    needs_reset: bool = False
    # Always True for this module — reset keeps the branch.  Surfaced so the TUI
    # can promise "keeps branch + commits" in the confirm dialog.
    branch_preserved: bool = True
    # Whether a reset was actually performed this run.
    reset_performed: bool = False

    def to_json_dict(self) -> dict:
        """Return a JSON-serialisable dict of all DiagnoseResult fields.
        Used by ``coord diagnose --json`` and the daemon ``post_diagnose``
        handler (#935 Part C) so the TUI can parse findings/actions without
        scraping the human-readable output lines."""
        import dataclasses  # noqa: PLC0415 — lazy to avoid circular import risk
        return dataclasses.asdict(self)

    def summary_line(self) -> str:
        """The machine-readable trailer the TUI greps for (mirrors the
        ``coord:`` marker convention)."""
        return (
            f"DIAGNOSE_RESULT: stage={self.stage} "
            f"recovered={str(self.recovered).lower()} "
            f"needs_reset={str(self.needs_reset).lower()} "
            f"reset_performed={str(self.reset_performed).lower()} "
            f"actions={len(self.actions_taken)}"
        )


# ── stage / assignment resolution ───────────────────────────────────────────


def stage_assignments(
    board: "Board", repo_name: str, issue_number: int, stage: str
) -> list["Assignment"]:
    """All assignments for *issue_number* in *repo_name* matching *stage*,
    newest-dispatched first.  Mirrors the TUI's ``assignments_for_stage``."""
    types = STAGE_ASSIGNMENT_TYPES.get(stage, (stage,))
    rows = [
        a
        for a in (board.active + board.completed)
        if a.issue_number == issue_number
        and a.repo_name == repo_name
        and (a.type or "work") in types
    ]
    rows.sort(key=lambda a: (a.dispatched_at or 0.0), reverse=True)
    return rows


def _latest(assignments: list["Assignment"]) -> "Assignment | None":
    return assignments[0] if assignments else None


def current_stage(board: "Board", repo_name: str, issue_number: int) -> str:
    """The stage of the most-recently-dispatched assignment for the issue
    (what ``coord diagnose <repo> <issue>`` targets when ``--stage`` is
    omitted).  Falls back to ``work`` when the issue has no assignments.

    #1083: previously coerced any assignment ``type`` this module doesn't
    recognize (e.g. ``test-author``, ``mock-author``, ``smoke``) to
    ``"work"`` — which then had ``diagnose_stage`` recover/report on
    whatever unrelated ``work``/``plan`` row happened to exist for the issue,
    *silently* presenting it as if it were a diagnosis of the real (ignored)
    assignment. Now the actual type is returned verbatim; ``diagnose_stage``
    explicitly reports "no diagnosis available" for types outside
    :data:`STAGE_ASSIGNMENT_TYPES` instead of guessing.
    """
    rows = [
        a
        for a in (board.active + board.completed)
        if a.issue_number == issue_number and a.repo_name == repo_name
    ]
    if not rows:
        return "work"
    newest = max(rows, key=lambda a: (a.dispatched_at or 0.0))
    return newest.type or "work"


# ── monkeypatchable side-effecting wrappers ─────────────────────────────────
#
# Each wraps an existing primitive and is replaced in unit tests so the
# orchestration can be exercised without touching git/tmux/the network.


def _resolve_machine(config: "Config", machine_name: str | None):
    if not machine_name:
        return None
    return next((m for m in config.machines if m.name == machine_name), None)


def _session_state(assignment: "Assignment", config: "Config") -> str:
    """``"live"`` | ``"dead"`` | ``"unknown"`` for *assignment*'s tmux session.

    Probes the assignment's machine (local tmux, or the remote host's tmux over
    ssh — same mechanism as ``coord reattach`` / the stale-session reaper).
    ``"unknown"`` when the machine can't be resolved or the probe errors, so the
    caller never finalizes on a false negative.
    """
    import socket  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        tmux_session_alive,
        tmux_session_name,
    )

    if not assignment.assignment_id:
        return "unknown"
    machine = _resolve_machine(config, assignment.machine_name)
    ssh_target = None
    if machine is not None:
        local_hn = socket.gethostname().split(".")[0].lower()
        is_local = (
            machine.name.lower() == local_hn
            or machine.host.split(".")[0].lower() == local_hn
        )
        if not is_local:
            ssh_target = machine.host
    elif assignment.machine_name:
        # machine_name set but unknown in config — can't probe safely.
        return "unknown"
    host = TmuxHost(ssh_target=ssh_target)
    sname = tmux_session_name(assignment.assignment_id)
    try:
        return "live" if tmux_session_alive(sname, host=host) else "dead"
    except Exception:  # noqa: BLE001 — never let a probe error finalize a session
        return "unknown"


def _ssh_target_for(assignment: "Assignment", config: "Config") -> str | None:
    """The ssh host for *assignment*'s machine, or ``None`` when it's local."""
    import socket  # noqa: PLC0415

    machine = _resolve_machine(config, assignment.machine_name)
    if machine is None:
        return None
    local_hn = socket.gethostname().split(".")[0].lower()
    if machine.name.lower() == local_hn or machine.host.split(".")[0].lower() == local_hn:
        return None
    return machine.host


def _recover_review_findings(assignment: "Assignment", config: "Config") -> str | None:
    """Recover a review's verdict + findings from its session transcript and
    persist them through the durable seam (#617).  Returns the verdict on
    success, ``None`` when nothing was recoverable.  Read-only w.r.t. the
    session (safe to run even while it's live)."""
    from coord import issue_store  # noqa: PLC0415
    from coord.interactive import _review_findings_from_transcript  # noqa: PLC0415

    if not assignment.assignment_id:
        return None
    assignment_id: str = assignment.assignment_id
    ssh_target = _ssh_target_for(assignment, config)
    started_at = assignment.dispatched_at
    findings = _review_findings_from_transcript(
        assignment.issue_number,
        started_at,
        assignment_id=assignment_id,
        ssh_target=ssh_target,
    )
    if findings is None:
        return None
    repo_cfg = next((r for r in config.repos if r.name == assignment.repo_name), None)
    try:
        issue_store.post_result(
            issue_store.ResultRecord(
                assignment_id=assignment_id,
                machine_name=assignment.machine_name or "unknown",
                repo_name=assignment.repo_name,
                repo_github=(repo_cfg.github if repo_cfg else assignment.repo_name),
                issue_number=assignment.issue_number,
                status="done",
                verdict=findings.verdict,  # type: ignore[arg-type]
                summary="Findings recovered from the session transcript by coord diagnose.",
                findings_body=findings.body,
                branch=None,
            )
        )
    except RuntimeError as exc:
        # #990: the verdict was recovered from the transcript but couldn't be
        # durably persisted (retries exhausted / readback mismatch). Surface
        # this instead of letting it crash `coord diagnose` — the caller
        # treats a ``None`` return as "not recoverable" and reports
        # "re-review needed", which is the safe outcome here too since the
        # write did not actually land.
        import click  # noqa: PLC0415

        click.echo(
            f"  ⚠ recovered verdict {findings.verdict!r} from transcript for "
            f"{assignment.assignment_id} but failed to persist it: {exc}",
            err=True,
        )
        return None
    return findings.verdict


def _finalize_dead(assignment: "Assignment", config: "Config") -> str:
    """Finalize a dead/phantom session: record a terminal state, push any
    commits, release the claim, prune the worktree.  Returns a short status."""
    from coord.interactive import finalize_interactive_exit  # noqa: PLC0415
    from coord.state import COORD_DIR  # noqa: PLC0415

    machine = _resolve_machine(config, assignment.machine_name)
    repo_cfg = next((r for r in config.repos if r.name == assignment.repo_name), None)
    base = (repo_cfg.default_branch if repo_cfg else None) or "main"
    repo_github = repo_cfg.github if repo_cfg else assignment.repo_name
    repo_path = None
    if machine is not None and assignment.repo_name:
        from pathlib import Path  # noqa: PLC0415

        rp = machine.repo_path(assignment.repo_name)
        if rp:
            repo_path = str(Path(rp).expanduser())
    worktree = str(COORD_DIR / "worktrees" / (assignment.assignment_id or ""))
    fr = finalize_interactive_exit(
        assignment_id=assignment.assignment_id or "",
        repo_name=assignment.repo_name,
        repo_github=repo_github,
        issue_number=assignment.issue_number,
        machine_name=assignment.machine_name or "unknown",
        worktree_path=worktree if assignment.type in ("work", "plan") else None,
        base_branch=base,
        exit_code=0,
        started_at=assignment.dispatched_at,
        repo_path=repo_path,
        ssh_target=_ssh_target_for(assignment, config),
    )
    return fr.terminal_status or "finalized"


def _kill_session(assignment: "Assignment", config: "Config") -> bool:
    """``tmux kill-session`` for *assignment* (local or remote).  Used by reset
    to stop a live session before finalizing.  Returns True when the kill ran."""
    import subprocess  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        tmux_session_name,
    )

    if not assignment.assignment_id:
        return False
    host = TmuxHost(ssh_target=_ssh_target_for(assignment, config))
    sname = tmux_session_name(assignment.assignment_id)
    try:
        subprocess.run(
            host.cmd(["kill-session", "-t", sname]),
            capture_output=True,
            timeout=20,
        )
        return True
    except Exception:  # noqa: BLE001 — best-effort
        return False


def _reconcile_issue_merges(
    board: "Board", config: "Config", repo_name: str, issue_number: int, *, dry_run: bool
) -> list[str]:
    """Run the merge reconcile sweep scoped to one issue (branch backfill +
    out-of-band-merge detection)."""
    from coord.reconcile import reconcile_board_merges  # noqa: PLC0415

    return reconcile_board_merges(
        board, config, repo=repo_name, issue=issue_number, dry_run=dry_run
    )


def _mark_terminal(assignment: "Assignment", config: "Config") -> None:
    """Best-effort terminal write via the issue_store seam — the fallback used
    only when :func:`_finalize_dead` itself raised.  Records a failed completion
    so the phantom row leaves ``running`` and persists to the canonical DB
    WITHOUT relying on ``save_board`` (which the diagnose path deliberately does
    not call — it would clobber the seam writes with a stale snapshot)."""
    from coord import issue_store  # noqa: PLC0415

    if not assignment.assignment_id:
        return
    repo_cfg = next((r for r in config.repos if r.name == assignment.repo_name), None)
    try:
        issue_store.post_completion(
            issue_store.CompletionRecord(
                assignment_id=assignment.assignment_id,
                machine_name=assignment.machine_name or "unknown",
                repo_name=assignment.repo_name,
                repo_github=(repo_cfg.github if repo_cfg else assignment.repo_name),
                issue_number=assignment.issue_number,
                exit_code=1,  # → failed terminal state (out of 'running')
                commits_ahead=0,
                branch=assignment.branch,
            )
        )
    except Exception:  # noqa: BLE001 — fallback of a fallback; leave the phantom
        pass


# ── orchestration ───────────────────────────────────────────────────────────


def diagnose_stage(
    board: "Board",
    config: "Config",
    repo_name: str,
    issue_number: int,
    stage: str,
    *,
    reset: bool = False,
    dry_run: bool = False,
) -> DiagnoseResult:
    """Diagnose *stage* of *repo_name* #*issue_number*; best-effort recover;
    always reconcile this issue's DB; optionally reset (non-destructive).

    Returns a :class:`DiagnoseResult`.  Board mutations happen on the board
    passed in; the caller is responsible for persisting it (the CLI/daemon do
    so after this returns) — consistent with ``reconcile_board_merges``.
    """
    res = DiagnoseResult(repo_name=repo_name, issue_number=issue_number, stage=stage)

    # #1083: `stage` came either from an explicit `--stage` or from
    # `current_stage()`'s newest-assignment lookup. `current_stage()` now
    # surfaces a non-standard assignment type (e.g. "test-author",
    # "mock-author", "smoke") verbatim instead of silently mapping it to
    # "work" — so a type this module has no recovery logic for lands here as
    # `stage` rather than being guessed at. Report that plainly instead of
    # running `_recover_work_like` against it (which was never validated for
    # these types) or, worse, silently returning an unrelated `work`/`plan`
    # row's status as if it were this stage's diagnosis (the bug reported in
    # #1083: `coord diagnose` picked an unrelated, already-merged assignment
    # instead of flagging the real problem).
    if stage not in STAGE_ASSIGNMENT_TYPES:
        assignments = stage_assignments(board, repo_name, issue_number, stage)
        latest = _latest(assignments)
        known = ", ".join(sorted(STAGE_ASSIGNMENT_TYPES))
        if latest is None:
            res.findings.append(
                f"no diagnosis available for assignment type {stage!r} — "
                f"coord diagnose only understands: {known} (and no {stage!r} "
                f"assignment exists for #{issue_number} either)"
            )
        else:
            res.findings.append(
                f"no diagnosis available for assignment type {stage!r} — "
                f"coord diagnose only understands: {known}. Latest {stage!r} "
                f"assignment: {latest.assignment_id} status={latest.status} "
                f"branch={latest.branch or '(none)'} machine={latest.machine_name}"
            )
        res.recovered = False
        res.needs_reset = False
        return res

    assignments = stage_assignments(board, repo_name, issue_number, stage)
    latest = _latest(assignments)

    if latest is None:
        res.findings.append(f"no {stage} assignment on the board for #{issue_number}")
        res.recovered = True  # nothing wedged
        # Still run the issue-wide cleanup below.
        _cleanup_issue(board, config, repo_name, issue_number, res, dry_run=dry_run)
        return res

    # The stage step owns *latest*; record it so the issue-wide cleanup pass
    # doesn't re-finalize the same row (finalize writes the DB, not this
    # in-memory board row, so its status would still read "running" here).
    handled = {latest.assignment_id} if latest.assignment_id else set()

    state = _session_state(latest, config)
    res.findings.append(
        f"{stage}: latest={latest.assignment_id} status={latest.status} "
        f"session={state} machine={latest.machine_name}"
    )

    if reset:
        _do_reset(
            board, config, assignments, res, stage=stage,
            repo_name=repo_name, issue_number=issue_number, dry_run=dry_run,
        )
        _cleanup_issue(
            board, config, repo_name, issue_number, res, dry_run=dry_run, skip_ids=handled
        )
        return res

    # ── Best-effort recovery, per stage ─────────────────────────────────────
    if stage in ("review",):
        _recover_review(board, config, latest, state, res, dry_run=dry_run)
    elif stage in ("merge",):
        _recover_merge(board, config, repo_name, issue_number, latest, res, dry_run=dry_run)
    else:  # work / plan / test
        _recover_work_like(board, config, latest, state, res, dry_run=dry_run)

    _cleanup_issue(
        board, config, repo_name, issue_number, res, dry_run=dry_run, skip_ids=handled
    )
    return res


def _recover_review(
    board, config, latest, state, res: DiagnoseResult, *, dry_run: bool
) -> None:
    from coord.state import load_assignment_review_findings  # noqa: PLC0415

    has_findings = False
    if latest.assignment_id:
        cached = load_assignment_review_findings(latest.assignment_id)
        has_findings = bool(cached and (cached[1] or "").strip())

    verdict = latest.review_verdict
    if verdict == "request-changes" and not has_findings:
        res.findings.append("review verdict is request-changes but findings are EMPTY (#607 class)")
        if dry_run:
            res.findings.append("(dry-run) would recover findings from the session transcript")
            res.needs_reset = True
            return
        recovered_verdict = _recover_review_findings(latest, config)
        if recovered_verdict:
            res.actions_taken.append("recovered review findings from the session transcript → #603 store")
            res.recovered = True
        else:
            res.findings.append("findings NOT recoverable from transcript — re-review needed")
            res.needs_reset = True
    elif latest.status == "done" and verdict is None:
        # #812: review finalised as done but no verdict was ever captured.
        # The session likely failed to start (no session_id, no exit_code) or
        # exited before the reviewer ran coord report-result / the transcript-floor.
        # This is a permanent stuck state: nothing is running, TUI rendered it
        # blue/Active (now Fixed → red/Failed), Diagnose & Reset must handle it.
        res.findings.append(
            "review finalised as done but has no verdict — "
            "session likely failed to start or exited before verdict capture (#812)"
        )
        if dry_run:
            res.findings.append(
                "(dry-run) would try transcript recovery; if verdict not found, reset"
            )
            res.needs_reset = True
            return
        recovered_verdict = _recover_review_findings(latest, config)
        if recovered_verdict:
            res.actions_taken.append(
                "recovered review verdict/findings from session transcript"
            )
            res.recovered = True
        else:
            res.findings.append(
                "no verdict recoverable from transcript — "
                "reset to re-dispatch a fresh review"
            )
            res.needs_reset = True
    elif state == "dead" and latest.status == "running":
        res.findings.append("review session is dead but board still says running (phantom)")
        if not dry_run:
            # Try a transcript recovery first (captures the verdict if present),
            # then finalize to clear the phantom.
            if _recover_review_findings(latest, config):
                res.actions_taken.append("recovered review verdict/findings from transcript")
            res.actions_taken.append(f"finalized phantom review session ({_finalize_dead(latest, config)})")
            res.recovered = True
    elif state == "live" and _is_stale(latest):
        res.findings.append("review session is LIVE but stale (idle days) — capturing read-only, reset to clear")
        if not dry_run and _recover_review_findings(latest, config):
            res.actions_taken.append("captured current review findings from transcript (session left running)")
        res.needs_reset = True
    else:
        res.findings.append("review stage looks healthy")
        res.recovered = True


def _recover_merge(
    board, config, repo_name, issue_number, latest, res: DiagnoseResult, *, dry_run: bool
) -> None:
    actions = _reconcile_issue_merges(board, config, repo_name, issue_number, dry_run=dry_run)
    if actions:
        res.actions_taken.extend(actions)
        res.recovered = True
    else:
        res.findings.append("merge stage: nothing to reconcile")
        res.recovered = True


def _recover_work_like(
    board, config, latest, state, res: DiagnoseResult, *, dry_run: bool
) -> None:
    if state == "dead" and latest.status in ("running", "pending"):
        res.findings.append("session is dead but board still says running (phantom)")
        if not dry_run:
            res.actions_taken.append(f"finalized phantom session ({_finalize_dead(latest, config)})")
            res.recovered = True
    elif latest.status == "failed" and latest.failure_reason:
        # #618: assignment failed at launch (worktree-add or similar).  The
        # failure_reason tells us what happened; if it's a "branch already checked
        # out" error we can detect and prune the blocking orphaned worktree.
        res.findings.append(
            f"launch-failed: {latest.failure_reason}"
        )
        if latest.branch:
            _prune_orphan_for_failed(board, config, latest, res, dry_run=dry_run)
        # Only mark recovered when _prune_orphan_for_failed did NOT set needs_reset
        # (dirty worktrees that couldn't be pruned mean the block is still present).
        if not res.needs_reset:
            res.recovered = True  # stage row is already terminal — nothing more needed
    elif latest.status == "failed":
        # #814: remote interactive sessions finalize as "failed" without setting
        # failure_reason (the local-launch code path sets it; the remote backstop
        # in finalize_remote_interactive_exit does not).  The stage row is already
        # terminal, but there may be a blocking branch lock on the remote machine
        # that will cause the next retry to fail identically — detect and fix it.
        res.findings.append("work stage failed (no captured failure reason)")
        if latest.branch:
            _prune_orphan_for_failed(board, config, latest, res, dry_run=dry_run)
        if not res.needs_reset:
            res.recovered = True
    elif state == "live" and _is_stale(latest):
        res.findings.append("session is LIVE but stale (idle days) — reset to clear it")
        res.needs_reset = True
    elif state == "live":
        res.findings.append("session is live and recent — left running")
        res.recovered = True
    else:
        res.findings.append("stage looks healthy")
        res.recovered = True


def _prune_orphan_for_failed(
    board, config, latest: "Assignment", res: DiagnoseResult, *, dry_run: bool
) -> None:
    """#618/#814: if *latest* is a failed launch, detect and prune the orphaned
    worktree that caused the "branch already checked out" collision.

    Also checks (#814) whether the blocking holder is the repo BASE checkout
    (~/src/<repo>) on the assignment's machine.  Coord-managed worktrees are
    under ``~/.coord/worktrees/`` and can be force-removed; the base checkout
    must NEVER be removed — instead, ``git checkout <default_branch>`` frees the
    branch.  This second check is performed remotely via SSH when the assignment
    ran on a different machine.
    """
    branch = latest.branch
    if not branch:
        return
    repo_name = latest.repo_name
    repo_cfg = next((r for r in config.repos if r.name == repo_name), None)
    if repo_cfg is None:
        return

    # Find the repo path on the local machine.
    repo_path: Path | None = None
    for machine in config.machines:
        rp = machine.repo_path(repo_name)
        if rp:
            candidate = Path(rp).expanduser()
            if candidate.exists():
                repo_path = candidate
                break
    if repo_path is None:
        # #814: even without a local path, attempt the remote base-checkout check.
        _maybe_fix_base_checkout_lock(latest, config, branch, res, dry_run=dry_run)
        return

    active_ids = _active_assignment_ids_for_repo(board, repo_name)
    orphans = _find_orphaned_worktrees(repo_path, branch, active_assignment_ids=active_ids)
    if not orphans:
        # #814: no local coord worktree holding the branch — check whether the
        # BASE checkout on the assignment's machine is the blocker.
        _maybe_fix_base_checkout_lock(latest, config, branch, res, dry_run=dry_run)
        return

    res.findings.append(
        f"found {len(orphans)} orphaned worktree(s) holding branch {branch!r}: "
        + ", ".join(str(p) for p in orphans)
    )
    if dry_run:
        res.findings.append(
            f"(dry-run) would prune {len(orphans)} orphaned worktree(s) "
            "(re-run without --dry-run to remove)"
        )
        return

    removed, skipped = _prune_orphaned_worktrees(repo_path, orphans)
    if removed:
        res.actions_taken.append(
            f"pruned {len(removed)} orphaned worktree(s): "
            + ", ".join(str(p) for p in removed)
        )
    if skipped:
        res.findings.append(
            f"{len(skipped)} worktree(s) skipped (uncommitted work — inspect manually): "
            + ", ".join(str(p) for p in skipped)
        )
        res.needs_reset = True


def _maybe_fix_base_checkout_lock(
    latest: "Assignment",
    config: "Config",
    branch: str,
    res: DiagnoseResult,
    *,
    dry_run: bool,
) -> None:
    """#814: detect and optionally fix a base-checkout branch lock on the
    assignment's machine (local or remote).

    When ``~/src/<repo>`` on the target machine is checked out on *branch*,
    ``git worktree add`` refuses to create a worktree for that branch, causing
    launch failures that loop uselessly.  The fix is ``git checkout
    <default_branch>`` in the base checkout — NEVER pruning or deleting it
    (invariant #561).

    Works for both local assignments (SSH to ``localhost``) and remote ones.
    SSH failures are silently ignored — conservative: if we can't check we
    don't report a false "healthy".
    """
    machine = next(
        (m for m in config.machines if m.name == latest.machine_name), None
    )
    if machine is None:
        return
    repo_name = latest.repo_name
    repo_cfg = next((r for r in config.repos if r.name == repo_name), None)
    if repo_cfg is None:
        return

    rp_str = machine.repo_path(repo_name)
    if not rp_str:
        return
    # Build the $HOME-form path for the remote shell.
    if rp_str.startswith("~/"):
        remote_repo_sh = "$HOME/" + rp_str[2:]
    elif rp_str == "~":
        remote_repo_sh = "$HOME"
    else:
        remote_repo_sh = rp_str

    default_branch = repo_cfg.default_branch or "main"

    try:
        from coord.interactive import (  # noqa: PLC0415
            _holder_is_base_checkout,
            _remote_base_checkout_free_branch,
            find_remote_branch_holder,
        )
    except ImportError:
        return  # interactive module unavailable — skip gracefully

    holder = find_remote_branch_holder(machine.host, remote_repo_sh, branch)
    if holder is None or not _holder_is_base_checkout(holder):
        return  # not the base-checkout case

    res.findings.append(
        f"base checkout {holder!r} on {machine.host} is on branch {branch!r}"
        f" — this blocks worktree creation for {branch!r}"
    )
    if dry_run:
        res.findings.append(
            f"(dry-run) would checkout {default_branch!r} in {holder!r}"
            f" on {machine.host} to free the branch"
        )
        return

    freed = _remote_base_checkout_free_branch(
        machine.host, remote_repo_sh, default_branch,
    )
    if freed:
        res.actions_taken.append(
            f"freed base checkout {holder!r} on {machine.host}:"
            f" checked out {default_branch!r} (was on {branch!r})"
        )
    else:
        res.findings.append(
            f"could not auto-free base checkout on {machine.host} —"
            f" run manually: ssh {machine.host}"
            f" 'git -C {remote_repo_sh} checkout {default_branch}'"
        )
        res.needs_reset = True


def _do_reset(
    board, config, assignments, res: DiagnoseResult, *, stage: str,
    repo_name: str, issue_number: int, dry_run: bool,
) -> None:
    """Stage-aware, non-destructive reset (KEEP the branch + commits always).

    The shape of "reset" depends on the stage's state, not just on a live
    session: a completed REVIEW has no session to kill — its data lives in the
    board rows + #603 store — so resetting it means wiping that data so the
    stage goes back to grey/unrun and re-reviewable.
    """
    latest = _latest(assignments)
    if latest is None:
        res.findings.append(f"no {stage} stage to reset")
        res.recovered = True
        return

    if stage == "review":
        _reset_review_stage(config, repo_name, issue_number, res, dry_run=dry_run)
        return
    if stage == "test":
        _reset_test_stage(repo_name, issue_number, res, dry_run=dry_run)
        return

    # work / plan / merge — clear a live/phantom session, KEEP the branch.
    # (Merge reset deliberately does NOT un-merge; it only clears a stuck
    # session/row, so a clean re-attempt is possible without rewriting history.)
    if dry_run:
        res.findings.append("(dry-run) would reset: stop session, finalize, clear row — branch kept")
        res.needs_reset = True
        return
    if _session_state(latest, config) == "live" and _kill_session(latest, config):
        res.actions_taken.append("stopped the live session (tmux kill-session)")
    try:
        res.actions_taken.append(f"finalized session ({_finalize_dead(latest, config)})")
    except Exception as exc:  # noqa: BLE001 — fall back to a direct terminal mark
        res.findings.append(f"finalize failed ({exc}); marking row terminal directly")
        _mark_terminal(latest, config)
        res.actions_taken.append("marked stage row terminal")
    res.reset_performed = True
    res.recovered = True
    res.branch_preserved = True
    res.actions_taken.append("branch preserved — stage is re-dispatchable")


def _reset_review_stage(
    config, repo_name: str, issue_number: int, res: DiagnoseResult, *, dry_run: bool
) -> None:
    """Wipe a completed review so the stage returns to grey + re-reviewable:
    delete the ``type='review'`` rows, reset the work's ``review_state``, and
    purge the #603 ``source='review'`` context entries (the operator's
    'completely cleared out' choice).  No branch/commits touched."""
    from coord import state  # noqa: PLC0415

    if dry_run:
        res.findings.append(
            "(dry-run) would DELETE the review rows, reset work review_state → "
            "pending, and purge #603 review notes (box → grey, re-reviewable)"
        )
        res.needs_reset = True
        return
    deleted = state.delete_assignments_for_issue(repo_name, issue_number, types=("review",))
    res.actions_taken.append(f"deleted {deleted} review row(s) → stage grey")
    updated = state.reset_work_review_state(repo_name, issue_number)
    res.actions_taken.append(f"reset review_state→pending on {updated} work row(s) (re-reviewable)")
    purged = state.clear_issue_context_by_source(repo_name, issue_number, "review")
    res.actions_taken.append(f"purged {purged} #603 review note(s)")
    res.reset_performed = True
    res.recovered = True
    res.branch_preserved = True


def _reset_test_stage(
    repo_name: str, issue_number: int, res: DiagnoseResult, *, dry_run: bool
) -> None:
    """Clear the Test-gate verdict so the issue is re-testable.  No code touched."""
    from coord import state  # noqa: PLC0415

    if dry_run:
        res.findings.append("(dry-run) would clear test_state → re-testable")
        res.needs_reset = True
        return
    updated = state.reset_work_test_state(repo_name, issue_number)
    res.actions_taken.append(f"cleared Test verdict on {updated} work row(s) (re-testable)")
    res.reset_performed = True
    res.recovered = True
    res.branch_preserved = True


def _cleanup_issue(
    board,
    config,
    repo_name,
    issue_number,
    res: DiagnoseResult,
    *,
    dry_run: bool,
    skip_ids: set | None = None,
) -> None:
    """Always-on, issue-scoped DB cleanup: any OTHER phantom ``running`` rows
    for this issue whose session is dead get finalized to a terminal state."""
    skip = skip_ids or set()
    for a in (board.active + board.completed):
        if a.issue_number != issue_number or a.repo_name != repo_name:
            continue
        if a.assignment_id in skip:
            continue
        if a.status not in ("running", "pending"):
            continue
        if _session_state(a, config) != "dead":
            continue
        res.findings.append(f"cleanup: phantom {a.type} row {a.assignment_id} (session dead)")
        if not dry_run:
            try:
                _finalize_dead(a, config)
                res.actions_taken.append(f"cleanup: finalized phantom {a.type} row {a.assignment_id}")
            except Exception as exc:  # noqa: BLE001
                _mark_terminal(a, config)
                res.actions_taken.append(f"cleanup: marked phantom row {a.assignment_id} terminal ({exc})")


def _is_stale(assignment: "Assignment", *, max_age_hours: float = 12.0) -> bool:
    """A still-running session whose dispatch is older than *max_age_hours* is
    treated as stale (abandoned/idle) — recovery can't safely finalize a live
    session, so these escalate to a reset offer."""
    if not assignment.dispatched_at:
        return False
    return (time.time() - assignment.dispatched_at) > max_age_hours * 3600.0


# ── #618: orphaned worktree detection + pruning ──────────────────────────────


def _find_orphaned_worktrees(
    repo_path: Path,
    branch: str | None,
    *,
    active_assignment_ids: set[str],
    worktrees_dir: Path | None = None,
) -> list[Path]:
    """Return worktree paths under *worktrees_dir* that hold *branch* but belong
    to no active (live-tmux OR running-DB) assignment.

    A worktree is "orphaned" when ALL of:
    * Its directory is under ``~/.coord/worktrees/`` (coordinator-managed).
    * Its git checkout has *branch* checked out (or *branch* is ``None``,
      meaning any branch — used for fleet sweeps).
    * Its assignment_id (derived from the directory name) is NOT in
      *active_assignment_ids* — i.e. no live tmux session and no running DB row.

    Dirty worktrees (uncommitted changes) are listed but callers must skip
    force-remove — they'd lose uncommitted work.  Use ``_prune_orphaned_worktrees``
    to prune them with an uncommitted-work guard.
    """
    if worktrees_dir is None:
        from coord.state import COORD_DIR  # noqa: PLC0415
        worktrees_dir = COORD_DIR / "worktrees"

    orphans: list[Path] = []
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []

    # Parse the porcelain output into blocks.
    current: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                _maybe_orphan(current, branch, worktrees_dir, active_assignment_ids, orphans)
                current = {}
        elif line.startswith("worktree "):
            current["worktree"] = line[len("worktree "):]
        elif line.startswith("branch "):
            raw_branch = line[len("branch "):]
            current["branch"] = (
                raw_branch[len("refs/heads/"):] if raw_branch.startswith("refs/heads/") else raw_branch
            )
    if current:
        _maybe_orphan(current, branch, worktrees_dir, active_assignment_ids, orphans)

    return orphans


def _maybe_orphan(
    entry: dict[str, str],
    branch: str | None,
    worktrees_dir: Path,
    active_assignment_ids: set[str],
    out: list[Path],
) -> None:
    """Append to *out* if *entry* is an orphaned worktree for *branch*.

    When *branch* is ``None`` any branch matches (fleet sweep).
    """
    wt_str = entry.get("worktree", "")
    if not wt_str:
        return
    if branch is not None and entry.get("branch", "") != branch:
        return
    wt_path = Path(wt_str)
    # Only consider coordinator-managed worktrees (under ~/.coord/worktrees/).
    try:
        wt_path.relative_to(worktrees_dir)
    except ValueError:
        return
    # The assignment_id is the directory name component immediately under worktrees_dir.
    aid = wt_path.relative_to(worktrees_dir).parts[0]
    if aid in active_assignment_ids:
        return
    out.append(wt_path)


def _prune_orphaned_worktrees(
    repo_path: Path,
    orphans: list[Path],
    *,
    force: bool = False,
) -> tuple[list[Path], list[Path]]:
    """Remove *orphans* from *repo_path* via ``git worktree remove``.

    Returns ``(removed, skipped)``.  Worktrees with uncommitted changes are
    skipped when *force* is ``False`` (default) so no uncommitted work is lost.
    After removal, runs ``git worktree prune`` to clean admin entries.
    """
    removed: list[Path] = []
    skipped: list[Path] = []
    for wt in orphans:
        if not wt.exists():
            removed.append(wt)
            continue
        if not force:
            # Check for uncommitted changes — skip dirty worktrees.
            try:
                dirty = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(wt),
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                )
                if dirty.returncode == 0 and dirty.stdout.strip():
                    skipped.append(wt)
                    continue
            except (subprocess.SubprocessError, OSError):
                skipped.append(wt)
                continue
        try:
            r = subprocess.run(
                ["git", "worktree", "remove", str(wt), "--force"],
                cwd=str(repo_path),
                capture_output=True,
                timeout=15.0,
            )
            if r.returncode == 0:
                removed.append(wt)
            else:
                skipped.append(wt)
        except (subprocess.SubprocessError, OSError):
            skipped.append(wt)
    # Prune stale git admin entries regardless of what was removed.
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(repo_path),
            capture_output=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        pass
    return removed, skipped


def _active_assignment_ids_for_repo(
    board: "Board", repo_name: str
) -> set[str]:
    """Return assignment IDs for *repo_name* that are still running/pending."""
    return {
        a.assignment_id
        for a in board.active
        if a.repo_name == repo_name and a.assignment_id
    }


def find_and_prune_orphaned_worktrees(
    board: "Board",
    config: "Config",
    repo_name: str,
    branch: str,
) -> tuple[list[Path], list[Path]]:
    """Detect and prune orphaned coordinator worktrees holding *branch*.

    Public entry point used by :func:`diagnose_stage` (Gap 2 of #618) and
    by the ``coord diagnose --orphan-worktrees`` fleet sweep.

    Returns ``(removed, skipped)`` path lists.  The *skipped* list contains
    worktrees that have uncommitted changes — the operator must inspect and
    clean them manually.
    """
    repo_cfg = next((r for r in config.repos if r.name == repo_name), None)
    if repo_cfg is None:
        return [], []

    # Find the local checkout path for this repo.  We need it to run git commands.
    # On a thin client the local checkout may not exist; fall back gracefully.
    repo_path: Path | None = None
    for machine in config.machines:
        rp = machine.repo_path(repo_name)
        if rp:
            candidate = Path(rp).expanduser()
            if candidate.exists():
                repo_path = candidate
                break
    if repo_path is None:
        return [], []

    active_ids = _active_assignment_ids_for_repo(board, repo_name)
    orphans = _find_orphaned_worktrees(repo_path, branch, active_assignment_ids=active_ids)
    if not orphans:
        return [], []
    return _prune_orphaned_worktrees(repo_path, orphans)
