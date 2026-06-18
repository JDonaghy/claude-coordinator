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

import time
from dataclasses import dataclass, field
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
    "merge": ("work", "plan"),
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


# Stage ordering used to resolve the "current" stage when none is given.
_STAGE_ORDER = ["plan", "work", "review", "test", "merge"]


def current_stage(board: "Board", repo_name: str, issue_number: int) -> str:
    """The stage of the most-recently-dispatched assignment for the issue
    (what ``coord diagnose <repo> <issue>`` targets when ``--stage`` is
    omitted).  Falls back to ``work`` when the issue has no assignments."""
    rows = [
        a
        for a in (board.active + board.completed)
        if a.issue_number == issue_number and a.repo_name == repo_name
    ]
    if not rows:
        return "work"
    newest = max(rows, key=lambda a: (a.dispatched_at or 0.0))
    t = newest.type or "work"
    return t if t in _STAGE_ORDER else "work"


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
    ssh_target = _ssh_target_for(assignment, config)
    started_at = assignment.dispatched_at
    findings = _review_findings_from_transcript(
        assignment.issue_number, started_at, ssh_target=ssh_target
    )
    if findings is None:
        return None
    repo_cfg = next((r for r in config.repos if r.name == assignment.repo_name), None)
    issue_store.post_result(
        issue_store.ResultRecord(
            assignment_id=assignment.assignment_id,
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
    elif state == "live" and _is_stale(latest):
        res.findings.append("session is LIVE but stale (idle days) — reset to clear it")
        res.needs_reset = True
    elif state == "live":
        res.findings.append("session is live and recent — left running")
        res.recovered = True
    else:
        res.findings.append("stage looks healthy")
        res.recovered = True


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
