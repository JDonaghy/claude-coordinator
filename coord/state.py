"""Persistence for coordinator state (proposals, board, dispatched assignments,
notifications).

All I/O goes through SQLite via :mod:`coord.db`.  The JSON file constants are
kept as module attributes so that legacy ``monkeypatch.setattr`` calls in tests
don't raise ``AttributeError``, but none of the functions use them for I/O any
more.  Use the ``coord_db`` pytest fixture (defined in tests/conftest.py) to
isolate tests with an in-memory database.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sqlite3
import time
import warnings
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

from coord._board_mapping import (
    assemble_board as _assemble_board,
    decode_smoke_tests as _decode_smoke_tests,
    infer_review_state as _infer_review_state_core,
    json_loads as _json_loads,
    row_to_assignment as _row_to_assignment,
)
from coord.board_service import resolve as _board_service_resolve
from coord.board_service import route_write as _route_write
from coord.db import get_connection
from coord.models import Assignment, Board, Proposal, SplitChunk, SplitProposal

# Re-exported for backward compatibility (these moved to coord._board_mapping in
# #584 so the daemon/client can share the one mapping):
#   _json_loads, _decode_smoke_tests, _row_to_assignment
__all__ = ["_json_loads", "_decode_smoke_tests", "_row_to_assignment"]

# ── Directory for logs and other non-DB state ─────────────────────────────────
COORD_DIR = Path.home() / ".coord"

# Legacy file-path constants — kept so that existing monkeypatch.setattr calls
# don't blow up with AttributeError.  None of the functions read/write these.
PROPOSALS_FILE = COORD_DIR / "pending_proposals.json"
SPLITS_FILE = COORD_DIR / "pending_splits.json"
DISPATCHED_FILE = COORD_DIR / "dispatched.json"
NOTIFIED_FILE = COORD_DIR / "notified.json"
BOARD_FILE = COORD_DIR / "board.json"
SESSION_FILE = COORD_DIR / "session.json"
PLANS_FILE = COORD_DIR / "plans.json"


# ── Helpers ───────────────────────────────────────────────────────────────────
# _json_loads, _decode_smoke_tests and _row_to_assignment now live in
# coord._board_mapping (#584) so the daemon/client share the one mapping; they
# are imported above under their original private names.


def _assignment_upsert_params(a: Assignment) -> tuple:
    """Return the tuple of values for an assignment upsert SQL statement."""
    return (
        a.assignment_id or "",
        a.machine_name,
        a.repo_name,
        a.issue_number,
        a.issue_title,
        a.status,
        a.type,
        a.branch,
        a.pr_url,
        a.briefing or "",
        json.dumps(a.files_allowed),
        json.dumps(a.files_forbidden),
        a.model,
        a.dispatched_at,
        a.finished_at,
        a.smoke_test,
        a.smoke_test_reason,
        a.review_state,
        a.review_of_assignment_id,
        a.review_target,
        json.dumps(a.required_gates),
        json.dumps(a.plan) if a.plan is not None else None,
        a.unreachable_count,
        a.review_iteration,
        a.review_posted_at,
        a.test_state,
        a.test_reason,
        a.review_verdict,
        # #821: commit-bound SHA for review assignments.
        a.review_head_sha,
        a.cost_usd,
        # #252: encode list as JSON; None → NULL.
        (json.dumps(a.smoke_tests) if a.smoke_tests is not None else None),
        # #324: resolved provider name; None → NULL.
        a.provider_name,
    )


_UPSERT_SQL = """
    INSERT INTO assignments (
        assignment_id, machine_name, repo_name, issue_number, issue_title,
        status, type, branch, pr_url, briefing,
        files_allowed, files_forbidden, model, dispatched_at, finished_at,
        smoke_test, smoke_test_reason, review_state, review_of_assignment_id,
        review_target, required_gates, plan, unreachable_count, review_iteration,
        review_posted_at, test_state, test_reason, review_verdict, review_head_sha,
        cost_usd, smoke_tests, provider_name
    ) VALUES (
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?
    )
    ON CONFLICT(assignment_id) DO UPDATE SET
        status             = excluded.status,
        branch             = excluded.branch,
        pr_url             = excluded.pr_url,
        finished_at        = excluded.finished_at,
        smoke_test         = excluded.smoke_test,
        smoke_test_reason  = excluded.smoke_test_reason,
        review_state       = excluded.review_state,
        review_of_assignment_id = excluded.review_of_assignment_id,
        review_target      = excluded.review_target,
        unreachable_count  = excluded.unreachable_count,
        plan               = excluded.plan,
        model              = excluded.model,
        briefing           = excluded.briefing,
        files_allowed      = excluded.files_allowed,
        files_forbidden    = excluded.files_forbidden,
        required_gates     = excluded.required_gates,
        review_iteration   = excluded.review_iteration,
        review_posted_at   = COALESCE(excluded.review_posted_at, review_posted_at),
        test_state         = excluded.test_state,
        test_reason        = excluded.test_reason,
        review_verdict     = COALESCE(excluded.review_verdict, review_verdict),
        -- #821: once a review_head_sha is recorded, preserve it; a later
        -- upsert without the SHA (e.g. from an older code path) must not
        -- erase a captured value.
        review_head_sha    = COALESCE(excluded.review_head_sha, review_head_sha),
        -- #208: cost_usd is set once at completion.  COALESCE so a re-load
        -- of the same row from an agent that doesn't know the cost
        -- doesn't blow away a previously-captured value.
        cost_usd           = COALESCE(excluded.cost_usd, cost_usd),
        -- #252: same pattern — once a worker has emitted a smoke-test
        -- list, a later upsert without one (e.g. agent reload) can't
        -- erase it.
        smoke_tests        = COALESCE(excluded.smoke_tests, smoke_tests),
        -- #324: once a provider_name is recorded at dispatch, a later
        -- upsert without one (e.g. agent reload) must not clear it.
        provider_name      = COALESCE(excluded.provider_name, provider_name)
"""


# ── Session ───────────────────────────────────────────────────────────────────

def write_session_start() -> None:
    """Record session start with clean_shutdown=False."""
    conn = get_connection()
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn.execute(
        """INSERT INTO sessions (started_at, clean_shutdown)
           VALUES (?, 0)""",
        (started_at,),
    )
    conn.commit()


def write_session_end(
    *,
    completed_ids: list[str],
    issues_closed: list[int],
    total_cost_usd: float,
) -> None:
    """Record session end with clean_shutdown=True and summary stats."""
    conn = get_connection()
    ended_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # Find the latest session row; update it or insert if none
    row = conn.execute(
        "SELECT id, started_at FROM sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE sessions SET
               ended_at = ?, clean_shutdown = 1,
               completed_this_session = ?, issues_closed = ?,
               total_cost_usd = ?
               WHERE id = ?""",
            (
                ended_at,
                json.dumps(completed_ids),
                json.dumps(issues_closed),
                total_cost_usd,
                row["id"],
            ),
        )
    else:
        conn.execute(
            """INSERT INTO sessions
               (ended_at, clean_shutdown, completed_this_session,
                issues_closed, total_cost_usd)
               VALUES (?, 1, ?, ?, ?)""",
            (ended_at, json.dumps(completed_ids), json.dumps(issues_closed),
             total_cost_usd),
        )
    conn.commit()


def load_session() -> dict | None:
    """Load the latest session record.  Returns None if no session exists."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    result: dict = {
        "started_at": d.get("started_at"),
        "clean_shutdown": bool(d.get("clean_shutdown")),
    }
    if d.get("ended_at"):
        result["ended_at"] = d["ended_at"]
    if d.get("completed_this_session") is not None:
        result["completed_this_session"] = json.loads(d["completed_this_session"])
    if d.get("issues_closed") is not None:
        result["issues_closed"] = json.loads(d["issues_closed"])
    if d.get("total_cost_usd") is not None:
        result["total_cost_usd"] = d["total_cost_usd"]
    return result


# ── Proposals ─────────────────────────────────────────────────────────────────

def save_proposals(proposals: list[Proposal]) -> Path:
    """Persist the current proposal list (replaces previous list)."""
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM proposals")
        for p in proposals:
            conn.execute(
                """INSERT INTO proposals
                   (id, machine_name, repo_name, issue_number, issue_title,
                    rationale, files_likely, briefing, model, type, required_gates)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p.id, p.machine_name, p.repo_name, p.issue_number,
                    p.issue_title, p.rationale,
                    json.dumps(list(p.files_likely)),
                    p.briefing, p.model, p.type,
                    json.dumps(list(p.required_gates)),
                ),
            )
    return PROPOSALS_FILE  # Return legacy path for callers that check it


def load_proposals() -> list[Proposal]:
    """Return all pending proposals."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
    return [
        Proposal(
            id=row["id"],
            machine_name=row["machine_name"],
            repo_name=row["repo_name"],
            issue_number=row["issue_number"],
            issue_title=row["issue_title"],
            rationale=row["rationale"] or "",
            files_likely=_json_loads(row["files_likely"]) or [],
            briefing=row["briefing"] or "",
            model=row["model"],
            type=row["type"] or "work",
            required_gates=_json_loads(row["required_gates"]) or [],
        )
        for row in rows
    ]


def clear_proposals() -> None:
    """Delete all pending proposals."""
    conn = get_connection()
    conn.execute("DELETE FROM proposals")
    conn.commit()


# ── Split proposals ───────────────────────────────────────────────────────────

def save_split_proposals(splits: list[SplitProposal]) -> Path:
    """Persist the current split-proposal list (replaces previous list)."""
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM split_chunks")
        conn.execute("DELETE FROM split_proposals")
        for s in splits:
            conn.execute(
                """INSERT INTO split_proposals
                   (id, repo_name, issue_number, issue_title, rationale)
                   VALUES (?, ?, ?, ?, ?)""",
                (s.id, s.repo_name, s.issue_number, s.issue_title, s.rationale),
            )
            for chunk in s.chunks:
                conn.execute(
                    """INSERT INTO split_chunks
                       (split_proposal_id, title, scope, files_likely)
                       VALUES (?, ?, ?, ?)""",
                    (s.id, chunk.title, chunk.scope, json.dumps(list(chunk.files_likely))),
                )
    return SPLITS_FILE


def load_split_proposals() -> list[SplitProposal]:
    """Return all pending split proposals."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM split_proposals ORDER BY id").fetchall()
    result = []
    for row in rows:
        chunks = conn.execute(
            "SELECT * FROM split_chunks WHERE split_proposal_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        result.append(
            SplitProposal(
                id=row["id"],
                repo_name=row["repo_name"],
                issue_number=row["issue_number"],
                issue_title=row["issue_title"],
                rationale=row["rationale"] or "",
                chunks=[
                    SplitChunk(
                        title=c["title"],
                        scope=c["scope"],
                        files_likely=_json_loads(c["files_likely"]) or [],
                    )
                    for c in chunks
                ],
            )
        )
    return result


def clear_split_proposals() -> None:
    """Delete all split proposals and their chunks."""
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM split_chunks")
        conn.execute("DELETE FROM split_proposals")


# ── Dispatched-assignment ledger ──────────────────────────────────────────────

def load_dispatched() -> list[dict]:
    """Return dispatched assignments as dicts matching the old JSON ledger format.

    Only returns rows that were explicitly dispatched (``dispatched_at IS NOT
    NULL``).  Assignments inserted solely via :func:`save_board` (e.g. created
    directly in tests without going through the dispatch path) are excluded.

    **Thin-client note (#906):** reads the local DB directly.  On a thin client
    this will be empty/stale.  Callers that need the canonical list on a thin
    client should use ``board_service.read_board()`` and build the dispatch-dict
    format from the board's active assignments instead (see
    ``coord.commands.plan_followup._dispatch_followup`` for the board-based
    migration).  The guard fires on thin clients so the offending caller is
    identifiable.
    """
    _thin_client_local_board_guard("load_dispatched")
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM assignments WHERE dispatched_at IS NOT NULL ORDER BY dispatched_at"
    ).fetchall()
    return [_row_to_dispatched_dict(row) for row in rows]


def _row_to_dispatched_dict(row: object) -> dict:
    d = dict(row)
    return {
        "assignment_id": d.get("assignment_id"),
        "machine_name": d.get("machine_name", ""),
        "repo_name": d.get("repo_name", ""),
        "repo_github": d.get("repo_github"),
        "issue_number": d.get("issue_number", 0),
        "issue_title": d.get("issue_title", ""),
        "files_likely": _json_loads(d.get("files_allowed")) or [],
        "briefing": d.get("briefing") or "",
        "model": d.get("model"),
        "type": d.get("type", "work"),
        "required_gates": _json_loads(d.get("required_gates")) or [],
        "dispatched_at": d.get("dispatched_at"),
        "review_of_assignment_id": d.get("review_of_assignment_id"),
        "review_target": d.get("review_target"),
        "status": d.get("status"),
    }


# ── Daemon routing (#590 Phase 2) ────────────────────────────────────────────
#
# When ``board_service`` is set (a thin client over Tailscale), an assignment
# dispatched from this box must land on the daemon's shared DB, not the client's
# local ``coord.db`` — otherwise the new row never reaches the board everyone
# else sees and the launch is invisible.  ``record_dispatched`` /
# ``record_dispatched_assignment`` / ``record_test_verdict`` become thin routing
# wrappers over ``_*_local``; the daemon endpoints call the ``_local`` form
# directly so a daemon can never recurse back out over HTTP.  ``board_service``
# unset → the ``_local`` path runs unchanged (no regression).


def _board_service():  # -> ServiceConfig | None
    # #749: delegates to coord.board_service.resolve() rather than importing
    # coord.client directly — coord.state's outward coupling now goes through
    # the one board_service facade.
    return _board_service_resolve()


def _thin_client_local_board_guard(fn_name: str) -> None:
    """Warn (or raise in strict mode) when a thin client touches the local board.

    Fires only when ``_board_service()`` is set (thin-client mode).  A no-op
    on the daemon host where the local DB is canonical.

    **Default behaviour (non-breaking):** emits a ``UserWarning`` via
    :func:`warnings.warn` *and* :func:`logging.warning`, both carrying
    the ``#615`` tag and a caller-identifying frame so the ``coord.cli``
    command that still reads/writes the local board can be pinpointed.

    **Strict mode (``COORD_STRICT_LOCAL_BOARD=1``):** raises
    :class:`RuntimeError` so CI / a deliberate audit run surfaces every
    remaining offender as a hard failure.

    This is "option B" debt instrumentation for #615: run the coordinator
    on a thin client, watch what lights up, then migrate each offending
    ``save_board`` / ``load_board`` / ``build_board`` call to a
    daemon-routed path incrementally.
    """
    if _board_service() is None:
        return  # daemon host — local DB IS canonical; guard is a no-op

    # Walk the call stack to find the most informative caller frame.
    # Prefer frames from coord.cli so the message names the subcommand.
    caller_info = "<unknown>"
    try:
        state_module = __name__  # "coord.state"
        best: inspect.FrameInfo | None = None
        for fi in inspect.stack()[2:]:  # skip this fn + the board fn that called us
            mod = fi.frame.f_globals.get("__name__", "")
            if mod == state_module:
                continue  # still inside coord.state — keep looking
            if best is None:
                best = fi  # first frame outside coord.state
            if "cli" in mod:
                best = fi  # prefer coord.cli frames; keep going in case of deeper
                break
        if best is not None:
            caller_info = (
                f"{best.frame.f_globals.get('__name__', '?')}.{best.function}"
                f" ({Path(best.filename).name}:{best.lineno})"
            )
    except Exception:  # noqa: BLE001 — introspection must never break a command
        pass

    action = "wrote" if "save" in fn_name else "read"
    msg = (
        f"#615: {fn_name}() {action} the local board on a thin client — "
        f"this command is not yet daemon-routed; its effect will NOT reach "
        f"the daemon. Caller: {caller_info}."
    )

    if os.environ.get("COORD_STRICT_LOCAL_BOARD", "").strip() == "1":
        raise RuntimeError(msg)

    # Warn via both channels: warnings (capturable in tests / -W flags) and
    # logging (shows up in log files and structured output).
    # stacklevel=3: attributes the warning to the caller of save/load/build_board.
    warnings.warn(msg, UserWarning, stacklevel=3)
    _log.warning(msg)


def record_dispatched(
    *,
    assignment_id: str,
    proposal: Proposal,
    repo_github: str,
    provider_name: str | None = None,
) -> None:
    """Record a newly dispatched assignment — routes to the daemon when set."""
    svc = _board_service()
    resp = _route_write(
        svc,
        "/dispatched-work",
        {
            "assignment_id": assignment_id,
            "proposal": asdict(proposal),
            "repo_github": repo_github,
            "provider_name": provider_name,
        },
    )
    if resp is not None:
        return
    _record_dispatched_local(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_github,
        provider_name=provider_name,
    )


def _record_dispatched_local(
    *,
    assignment_id: str,
    proposal: Proposal,
    repo_github: str,
    provider_name: str | None = None,
) -> None:
    """Record a newly dispatched assignment in the assignments table.

    Args:
        assignment_id: The agent-assigned ID from the dispatch response.
        proposal: The proposal that was dispatched.
        repo_github: The ``owner/repo`` GitHub identifier.
        provider_name: The *resolved* provider name (after the spec > repo >
            default precedence chain).  ``None`` for callers that predate
            #324 — the TUI shows the implicit default ("claude") when NULL.
    """
    # #706: compute the deterministic branch name at dispatch time so the row
    # is never branch=NULL.  Mirrors agent.py:1021 exactly:
    #   branch_name = existing_branch or f"issue-{issue_number}-{_slugify(issue_title)}"
    # where proposal.target_branch maps to existing_branch.
    from coord.agent import _slugify  # noqa: PLC0415

    branch = proposal.target_branch or (
        f"issue-{proposal.issue_number}-{_slugify(proposal.issue_title)}"
    )

    conn = get_connection()
    conn.execute(
        """INSERT INTO assignments (
            assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, type, briefing,
            files_allowed, model, dispatched_at, required_gates,
            provider_name, branch
        ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(assignment_id) DO NOTHING""",
        (
            assignment_id,
            proposal.machine_name,
            proposal.repo_name,
            repo_github,
            proposal.issue_number,
            proposal.issue_title,
            proposal.type,
            proposal.briefing,
            json.dumps(list(proposal.files_likely)),
            proposal.model,
            time.time(),
            json.dumps(list(proposal.required_gates)),
            provider_name,
            branch,
        ),
    )
    conn.commit()


def record_dispatched_assignment(
    *,
    assignment: Assignment,
    repo_github: str,
) -> None:
    """Record a dispatched assignment — routes to the daemon when set."""
    svc = _board_service()
    resp = _route_write(
        svc, "/dispatched", {"assignment": asdict(assignment), "repo_github": repo_github}
    )
    if resp is not None:
        return
    _record_dispatched_assignment_local(assignment=assignment, repo_github=repo_github)


def _record_dispatched_assignment_local(
    *,
    assignment: Assignment,
    repo_github: str,
) -> None:
    """Record a dispatched assignment (review, smoke, retry) from an Assignment object."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO assignments (
            assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, type, briefing,
            files_allowed, model, dispatched_at, review_of_assignment_id,
            review_target, required_gates, review_iteration,
            provider_name, branch
        ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(assignment_id) DO UPDATE SET
            status = 'running',
            machine_name = excluded.machine_name,
            repo_github = excluded.repo_github,
            type = excluded.type,
            briefing = excluded.briefing,
            model = excluded.model,
            dispatched_at = excluded.dispatched_at,
            review_of_assignment_id = excluded.review_of_assignment_id,
            review_target = excluded.review_target,
            required_gates = excluded.required_gates,
            review_iteration = excluded.review_iteration,
            -- #324: COALESCE so a retry/re-dispatch doesn't clear a
            -- previously-recorded provider_name from the original dispatch.
            provider_name = COALESCE(excluded.provider_name, provider_name),
            -- #557: COALESCE so a re-dispatch doesn't clear a branch that
            -- finalize already wrote (mark_notified sets branch on completion).
            branch = COALESCE(excluded.branch, branch)""",
        (
            assignment.assignment_id or "",
            assignment.machine_name,
            assignment.repo_name,
            repo_github,
            assignment.issue_number,
            assignment.issue_title,
            assignment.type,
            assignment.briefing,
            json.dumps(list(assignment.files_allowed)),
            assignment.model,
            assignment.dispatched_at or time.time(),
            assignment.review_of_assignment_id,
            assignment.review_target,
            json.dumps(list(assignment.required_gates)),
            assignment.review_iteration,
            assignment.provider_name,
            assignment.branch,
        ),
    )
    conn.commit()


def record_acceptance_verdict(
    *,
    assignment_id: str,
    acceptance_state: str,
    acceptance_reason: str | None = None,
    acceptance_sha: str | None = None,
    acceptance_total: int | None = None,
    acceptance_passed: int | None = None,
) -> None:
    """Record an Acceptance-gate verdict on one assignment (#944, the oracle
    loop's external trust gate) — routes to the daemon when set.

    The single-row analogue of ``record_test_verdict``, called by ``coord
    acceptance record --issue N --sha <sha>`` after re-running the sealed
    suite externally against the pushed SHA. ``acceptance_total`` /
    ``acceptance_passed`` (#932) are the per-test counts backing the
    Acceptance box's partial-green display (e.g. "3/7").
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/acceptance-verdict",
        {
            "assignment_id": assignment_id,
            "acceptance_state": acceptance_state,
            "acceptance_reason": acceptance_reason,
            "acceptance_sha": acceptance_sha,
            "acceptance_total": acceptance_total,
            "acceptance_passed": acceptance_passed,
        },
    )
    if resp is not None:
        return
    _record_acceptance_verdict_local(
        assignment_id=assignment_id,
        acceptance_state=acceptance_state,
        acceptance_reason=acceptance_reason,
        acceptance_sha=acceptance_sha,
        acceptance_total=acceptance_total,
        acceptance_passed=acceptance_passed,
    )


def _record_acceptance_verdict_local(
    *,
    assignment_id: str,
    acceptance_state: str,
    acceptance_reason: str | None = None,
    acceptance_sha: str | None = None,
    acceptance_total: int | None = None,
    acceptance_passed: int | None = None,
) -> None:
    """UPDATE the assignment's acceptance_state/acceptance_reason/acceptance_sha
    (+ #932's acceptance_total/acceptance_passed counts)."""
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET acceptance_state=?, acceptance_reason=?, "
        "acceptance_sha=?, acceptance_total=?, acceptance_passed=? WHERE assignment_id=?",
        (
            acceptance_state,
            acceptance_reason,
            acceptance_sha,
            acceptance_total,
            acceptance_passed,
            assignment_id,
        ),
    )
    conn.commit()

    # #603: a failed external acceptance re-run is durable context for EVERY
    # future agent on the issue — mirrors the test-failure note below. Local
    # writer (we're already daemon-side on a thin client), so use the
    # _local variant to avoid re-routing.
    if acceptance_state == "failed" and (acceptance_reason or "").strip():
        row = conn.execute(
            "SELECT repo_name, issue_number FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        if row is not None:
            _add_issue_context_entry_local(
                row["repo_name"],
                row["issue_number"],
                f"Acceptance FAILED @ {acceptance_sha or '?'}: {acceptance_reason.strip()}",
                source="test",
            )


def record_test_verdict(
    *,
    assignment_id: str,
    test_state: str,
    test_reason: str | None = None,
    smoke_test: str | None = None,
    smoke_test_reason: str | None = None,
) -> None:
    """Record a Test-gate verdict on one assignment — routes to the daemon when set.

    The single-row analogue of the ``coord test`` ``save_board`` write, used so a
    thin client (and the TUI's verdict key) can record a verdict to the shared DB
    without rewriting the whole board.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/test-verdict",
        {
            "assignment_id": assignment_id,
            "test_state": test_state,
            "test_reason": test_reason,
            "smoke_test": smoke_test,
            "smoke_test_reason": smoke_test_reason,
        },
    )
    if resp is not None:
        return
    _record_test_verdict_local(
        assignment_id=assignment_id,
        test_state=test_state,
        test_reason=test_reason,
        smoke_test=smoke_test,
        smoke_test_reason=smoke_test_reason,
    )


def _record_test_verdict_local(
    *,
    assignment_id: str,
    test_state: str,
    test_reason: str | None = None,
    smoke_test: str | None = None,
    smoke_test_reason: str | None = None,
) -> None:
    """UPDATE the assignment's test_state/test_reason (+ smoke_test mirror)."""
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET test_state=?, test_reason=? WHERE assignment_id=?",
        (test_state, test_reason, assignment_id),
    )
    # Mirror to legacy smoke_test only when a value was supplied (pass/fail),
    # matching coord test / the TUI's record_test_verdict_conn.
    if smoke_test is not None:
        conn.execute(
            "UPDATE assignments SET smoke_test=?, smoke_test_reason=? "
            "WHERE assignment_id=?",
            (smoke_test, smoke_test_reason, assignment_id),
        )
    conn.commit()
    # #603: a test failure is durable context for EVERY future agent on the
    # issue (not just the immediate fix worker) — record it in the per-issue
    # digest.  Local writer (we're already daemon-side on a thin client), so
    # use the _local variant to avoid re-routing.
    if test_state == "failed" and (test_reason or "").strip():
        row = conn.execute(
            "SELECT repo_name, issue_number FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        if row is not None:
            _add_issue_context_entry_local(
                row["repo_name"],
                row["issue_number"],
                f"Test FAILED: {test_reason.strip()}",
                source="test",
            )


# ── Notification ledger ────────────────────────────────────────────────────────

def load_notified() -> dict[str, dict]:
    """Return {assignment_id: {event, posted_at, branch?}} for all notified assignments."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM notifications").fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        entry: dict = {
            "event": row["event"],
            "posted_at": row["posted_at"],
        }
        if row["branch"]:
            entry["branch"] = row["branch"]
        result[row["assignment_id"]] = entry
    return result


def mark_notified(
    assignment_id: str,
    event: str,
    *,
    branch: str | None = None,
) -> None:
    """Record that a GitHub comment was posted for this assignment.

    Also updates the assignments table so that build_board() reflects the new
    status without needing a separate save_board() call.

    **Thin-client note (#906):** this function writes the local DB directly.
    On a thin client the canonical writes happen on the daemon via the
    ``COORD_NOTIFY_ON_DAEMON`` whole-command reroute (see
    ``coord.commands.lifecycle.notify``).  The guard fires when a thin-client
    caller bypasses the reroute — surfacing the issue without breaking the
    call.
    """
    _thin_client_local_board_guard("mark_notified")
    from coord.comments import EVENT_COMPLETION, EVENT_PLAN

    conn = get_connection()
    now = time.time()
    conn.execute(
        """INSERT OR REPLACE INTO notifications (assignment_id, event, branch, posted_at)
           VALUES (?, ?, ?, ?)""",
        (assignment_id, event, branch, now),
    )
    # Keep assignments table in sync so build_board() is always accurate.
    if event in (EVENT_COMPLETION, EVENT_PLAN):
        if branch is not None:
            conn.execute(
                "UPDATE assignments SET status=?, finished_at=?, branch=? WHERE assignment_id=?",
                ("done", now, branch, assignment_id),
            )
        else:
            conn.execute(
                "UPDATE assignments SET status=?, finished_at=? WHERE assignment_id=?",
                ("done", now, assignment_id),
            )
    else:
        conn.execute(
            "UPDATE assignments SET status='failed', finished_at=? WHERE assignment_id=?",
            (now, assignment_id),
        )
    conn.commit()


# ── Review-findings tracking ──────────────────────────────────────────────────

def update_assignment_review_findings(
    assignment_id: str,
    *,
    verdict: str,
    body: str,
) -> None:
    """#bounce / #905: persist a parsed `ReviewFindings` on the assignment row.

    Stored as JSON ({"verdict": ..., "body": ...}) so the future read
    path can recover both fields with one column.  Callers that only
    know the verdict (and not the body) should use
    `update_assignment_review_verdict` instead; this helper is for the
    place where notify already parsed the full findings.

    Idempotent: silently no-ops when the row doesn't exist (matches the
    other `update_assignment_*` helpers).

    **Daemon-aware (#905):** routes to ``POST /review-findings`` when a
    ``board_service`` is configured so the verdict lands on the shared DB
    rather than the thin client's empty local one.
    """
    if not assignment_id:
        return
    svc = _board_service()
    resp = _route_write(
        svc,
        "/review-findings",
        {"assignment_id": assignment_id, "verdict": verdict, "body": body},
    )
    if resp is not None:
        return
    _update_assignment_review_findings_local(assignment_id, verdict=verdict, body=body)


def _update_assignment_review_findings_local(
    assignment_id: str,
    *,
    verdict: str,
    body: str,
) -> None:
    """Local-DB write for :func:`update_assignment_review_findings`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    payload = json.dumps({"verdict": verdict, "body": body})
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET review_findings=?, review_verdict=? "
        "WHERE assignment_id=?",
        (payload, verdict, assignment_id),
    )
    conn.commit()


def delete_assignments_for_issue(
    repo_name: str, issue_number: int, *, types: tuple[str, ...]
) -> int:
    """Delete assignment rows of the given *types* for an issue.

    Used by the per-stage reset (``coord diagnose --reset``): wiping the
    ``type='review'`` rows makes the Review stage show no verdict (grey /
    Pending in the TUI) and removes the request-changes the merge gate keys on.
    Returns the number of rows deleted.  Runs against the canonical DB (the
    daemon executes diagnose), so no save_board is involved."""
    if not types:
        return 0
    conn = get_connection()
    placeholders = ",".join("?" for _ in types)
    cur = conn.execute(
        f"DELETE FROM assignments WHERE repo_name=? AND issue_number=? "  # noqa: S608 — placeholders are literal '?'
        f"AND type IN ({placeholders})",
        (repo_name, issue_number, *types),
    )
    conn.commit()
    return cur.rowcount


def reset_work_review_state(repo_name: str, issue_number: int) -> int:
    """Make an issue's work re-reviewable: reset the work/plan rows'
    ``review_state`` → 'pending' and clear ``review_verdict`` /
    ``review_posted_at``.  Returns rows updated."""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE assignments SET review_state='pending', review_verdict=NULL, "
        "review_posted_at=NULL "
        "WHERE repo_name=? AND issue_number=? AND type IN ('work','plan')",
        (repo_name, issue_number),
    )
    conn.commit()
    return cur.rowcount


def reset_work_test_state(repo_name: str, issue_number: int) -> int:
    """Clear the work/plan rows' Test-gate verdict (``test_state`` /
    ``test_reason``) so the issue is re-testable.  Returns rows updated."""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE assignments SET test_state=NULL, test_reason=NULL "
        "WHERE repo_name=? AND issue_number=? AND type IN ('work','plan')",
        (repo_name, issue_number),
    )
    conn.commit()
    return cur.rowcount


def clear_issue_context_by_source(
    repo_name: str, issue_number: int, source: str
) -> int:
    """Delete #603 context entries with a given *source* (e.g. 'review') for an
    issue — the targeted peer of :func:`clear_issue_context`.  Returns rows
    deleted."""
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM issue_context WHERE repo_name=? AND issue_number=? AND source=?",
        (repo_name, issue_number, source),
    )
    conn.commit()
    return cur.rowcount


def _parse_review_findings_blob(raw: object) -> tuple[str, str] | None:
    """Parse a stored ``review_findings`` blob into ``(verdict, body)``.

    Shared by the daemon and local reads so both hand callers the same shape.
    Accepts a JSON string (local DB column) or an already-decoded dict (daemon
    ``/board`` payload).  Returns ``None`` when empty, unparseable, or missing a
    string ``verdict``/``body``.
    """
    if not raw:
        return None
    if isinstance(raw, (str, bytes)):
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    else:
        payload = raw
    verdict = payload.get("verdict") if isinstance(payload, dict) else None
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(verdict, str) or not isinstance(body, str):
        return None
    return (verdict, body)


def load_assignment_review_findings(
    assignment_id: str,
) -> tuple[str, str] | None:
    """#bounce / #877: read back a cached `(verdict, body)` for an assignment.

    **Daemon-aware (#877):** when a ``board_service`` is configured (thin-client
    mode) the canonical ``review_findings`` live on the daemon, NOT in this
    host's local DB — which is empty/stale there.  A local-only read therefore
    silently misses daemon-captured findings, the exact #547 failure that made
    the verdict-relay backstop open a blank editor despite the body already
    being on the board.  So prefer the daemon board (``GET /board`` filtered by
    ``assignment_id``) and fall back to the local DB only when no
    ``board_service`` is set (daemon host, where the local DB IS canonical) or
    the fetch fails.

    Returns `None` when the row doesn't exist or the column is NULL
    (notify hasn't parsed this review yet) — callers fall back to
    parsing the log via local file or agent HTTP.
    """
    if not assignment_id:
        return None
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_board_payload  # noqa: PLC0415

            payload = fetch_board_payload(svc)
            for a in payload.get("assignments", []):
                if a.get("assignment_id") == assignment_id:
                    return _parse_review_findings_blob(a.get("review_findings"))
            # Daemon is canonical: assignment absent ⇒ genuinely no findings yet.
            return None
        except Exception:  # noqa: BLE001 — daemon unreachable → local fallback
            pass
    return _load_assignment_review_findings_local(assignment_id)


def _load_assignment_review_findings_local(
    assignment_id: str,
) -> tuple[str, str] | None:
    """Local-DB read for :func:`load_assignment_review_findings` — used on the
    daemon host (local DB is canonical) or as the offline fallback."""
    conn = get_connection()
    row = conn.execute(
        "SELECT review_findings FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        return None
    raw = row["review_findings"] if hasattr(row, "keys") else row[0]
    return _parse_review_findings_blob(raw)


def update_assignment_smoke_tests(
    assignment_id: str, smoke_tests: list[str],
) -> None:
    """#252: persist the worker's parsed SMOKE_TESTS list on the row — routes
    to the daemon when ``board_service`` is set (#749), else writes locally.

    Previously unrouted: on a thin client this silently wrote to a local DB
    that isn't the canonical one, so `coord notify` / `coord approve-plan`
    never actually recorded the SMOKE_TESTS block and the TUI never saw it.
    """
    if not assignment_id:
        return
    svc = _board_service()
    resp = _route_write(
        svc, "/assignment-usage", {"assignment_id": assignment_id, "smoke_tests": smoke_tests}
    )
    if resp is not None:
        return
    _update_assignment_smoke_tests_local(assignment_id, smoke_tests)


def _update_assignment_smoke_tests_local(
    assignment_id: str, smoke_tests: list[str],
) -> None:
    """#252: persist the worker's parsed SMOKE_TESTS list on the row.

    ``smoke_tests=[]`` (the explicit "no tests — change is internal"
    form) is stored as the JSON literal ``"[]"`` so the TUI can
    distinguish it from "no block emitted" (NULL).  Silently no-ops
    when the row doesn't exist — callers don't have to coordinate.
    """
    if not assignment_id:
        return
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET smoke_tests=? WHERE assignment_id=?",
        (json.dumps(smoke_tests), assignment_id),
    )
    conn.commit()


def update_assignment_claude_session_id(
    assignment_id: str, claude_session_id: str
) -> None:
    """#315: persist the worker's claude session ID on the assignment row.

    Called by ``coord notify`` once the agent reports the worker's completed
    session ID from its ``system.init`` event.  Best-effort: silently does
    nothing when the row doesn't exist or the ID is empty.  COALESCE-based
    UPDATE so the first writer wins (two concurrent notifies can't clobber
    a valid value with NULL).

    **Daemon-aware (#906):** routes to ``POST /assignment-session-id`` when a
    ``board_service`` is configured so the session ID lands on the shared DB.
    Fails-OPEN on HTTP error — a missed session-ID just means the next
    ``chat-continue`` will fall back to fetching it from the agent's
    ``/status`` endpoint (the #315 fallback already handles this).
    """
    if not assignment_id or not claude_session_id:
        return
    svc = _board_service()
    try:
        resp = _route_write(
            svc,
            "/assignment-session-id",
            {"assignment_id": assignment_id, "claude_session_id": claude_session_id},
        )
    except Exception as _e:  # noqa: BLE001
        import httpx as _httpx  # noqa: PLC0415
        if isinstance(_e, _httpx.HTTPError):
            _log.warning(
                "#906: update_assignment_claude_session_id: daemon write failed "
                "(deploy-lag?), falling back to local: %s", _e
            )
            resp = None
        else:
            raise
    if resp is not None:
        return
    _update_assignment_claude_session_id_local(assignment_id, claude_session_id)


def _update_assignment_claude_session_id_local(
    assignment_id: str, claude_session_id: str
) -> None:
    """Local-DB write for :func:`update_assignment_claude_session_id`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET claude_session_id=? WHERE assignment_id=? "
        "AND claude_session_id IS NULL",
        (claude_session_id, assignment_id),
    )
    conn.commit()


def update_assignment_cost(assignment_id: str, cost_usd: float) -> None:
    """#208/#665: record the worker's final cost — routes to the daemon when set.

    Idempotent: UPDATE fires only when cost_usd is NULL or the stored value
    is lower (first-writer-wins / monotone).  Silently does nothing when the
    row doesn't exist — callers shouldn't have to coordinate.
    """
    if not assignment_id:
        return
    svc = _board_service()
    resp = _route_write(
        svc, "/assignment-usage", {"assignment_id": assignment_id, "cost_usd": cost_usd}
    )
    if resp is not None:
        return
    _update_assignment_cost_local(assignment_id, cost_usd)


def _update_assignment_cost_local(assignment_id: str, cost_usd: float) -> None:
    """Write cost_usd directly to the local DB.  Called by the daemon endpoint."""
    if not assignment_id:
        return
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET cost_usd=? WHERE assignment_id=? "
        "AND (cost_usd IS NULL OR cost_usd < ?)",
        (cost_usd, assignment_id, cost_usd),
    )
    conn.commit()


def update_assignment_branch(assignment_id: str, branch: str) -> None:
    """#611: backfill the branch on an assignment row that is missing it.

    A remote interactive work session can finish ``status=done`` with
    ``branch=None`` even though it pushed ``issue-{N}-*`` to origin — the TUI
    then greys Start review/test/merge because the gate requires a done work
    assignment WITH a non-empty branch.  Idempotent: only sets ``branch`` when
    it is currently NULL or empty, so a reconcile sweep can run repeatedly and
    never clobber a real value.  Silently no-ops when the row doesn't exist —
    matches the other ``update_assignment_*`` helpers.
    """
    if not assignment_id or not branch:
        return
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET branch=? WHERE assignment_id=? "
        "AND (branch IS NULL OR branch = '')",
        (branch, assignment_id),
    )
    conn.commit()


def mark_assignment_merged(assignment_id: str) -> None:
    """#609: flip a done work assignment to ``status='merged'``.

    Work merged out-of-band (a direct GitHub merge, or a merge_queue row that
    drained without flipping the board) is otherwise never recorded as merged,
    so the TUI shows a grey merge box forever.  Idempotent: only transitions a
    row whose status is currently ``'done'`` (so a second call, or a row that
    failed/was re-dispatched, is left alone).  Silently no-ops when the row
    doesn't exist.
    """
    if not assignment_id:
        return
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET status='merged' WHERE assignment_id=? "
        "AND status='done'",
        (assignment_id,),
    )
    conn.commit()


def mark_work_review_settled(assignment_id: str) -> None:
    """#951: clear a lingering ``review_state='pending'`` ghost on a work row
    whose issue is already terminal (closed or PR merged).

    ``mark_assignment_merged`` (#609) flips a done work row's ``status`` to
    ``'merged'`` but never touches ``review_state``.  Every finished work
    assignment defaults to ``review_state='pending'`` (set unconditionally so
    the review-dispatch loop can pick it up), so that ghost survives the
    status flip and the row keeps surfacing as "[awaiting review]" in
    ``coord status`` / the TUI forever — the display tag is keyed on
    ``review_state`` independent of ``status``. Sweep (e) (#894) already
    settles this ghost for sibling ``review``/``smoke``/``conflict-fix`` rows
    via :func:`mark_sibling_review_done`; this is the ``type='work'`` mirror,
    which fell between both sweeps (#951).

    Idempotent: only transitions rows that still carry
    ``review_state='pending'``.  Silently no-ops when the row doesn't exist.
    """
    if not assignment_id:
        return
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET review_state='done' WHERE assignment_id=? "
        "AND type='work' AND review_state='pending'",
        (assignment_id,),
    )
    conn.commit()


def mark_sibling_review_done(assignment_id: str) -> None:
    """#894: clear the review_state='pending' ghost on a done sibling row.

    When a merged+closed issue has a lingering review/smoke/conflict-fix
    assignment that completed (status='done') but whose review_state was left
    at 'pending' by the interactive-completion path (issue_store sets
    review_state='pending' so reconcile picks it up like a claude -p worker),
    flip review_state → 'done' so it no longer surfaces as "awaiting review".

    Idempotent: only transitions rows that still carry review_state='pending'
    and status='done'.  Silently no-ops for other states.
    """
    if not assignment_id:
        return
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET review_state='done' WHERE assignment_id=? "
        "AND type IN ('review','smoke','conflict-fix') "
        "AND status='done' AND review_state='pending'",
        (assignment_id,),
    )
    conn.commit()


def mark_advisory_settled(assignment_id: str) -> None:
    """#894: flip an advisory row to 'merged' when its issue is terminal.

    Advisory assignments (status='advisory') for a merged+closed issue are
    never touched by the existing #609 sweep (which only looks at
    status='done' work rows), so they linger in the board's advisory view
    forever.  This settles them by flipping status → 'merged', consistent with
    how a done work row is settled by mark_assignment_merged.

    Idempotent: only transitions rows still carrying status='advisory'.
    Silently no-ops when the row doesn't exist or is already settled.
    """
    if not assignment_id:
        return
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET status='merged' WHERE assignment_id=? "
        "AND status='advisory'",
        (assignment_id,),
    )
    conn.commit()


def update_assignment_tokens(
    assignment_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> None:
    """#546/#665: record token counts — routes to the daemon when set.

    Only writes when at least one token count is non-zero (interactive/Max
    sessions produce no per-token data and should not overwrite 0 with 0).
    Idempotent: the UPDATE only fires when the row's ``input_tokens`` is still
    0 (first writer wins).  Silently swallows ``OperationalError`` so
    pre-migration databases (tests, older installs that haven't restarted the
    coordinator yet) never crash the notify path.
    """
    if not assignment_id:
        return
    total = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    if total <= 0:
        return
    svc = _board_service()
    resp = _route_write(
        svc,
        "/assignment-usage",
        {
            "assignment_id": assignment_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
        },
    )
    if resp is not None:
        return
    _update_assignment_tokens_local(
        assignment_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
    )


def _update_assignment_tokens_local(
    assignment_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> None:
    """Write token counts directly to the local DB.  Called by the daemon endpoint."""
    if not assignment_id:
        return
    total = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    if total <= 0:
        return
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE assignments SET "
            "input_tokens=?, output_tokens=?, "
            "cache_creation_tokens=?, cache_read_tokens=? "
            "WHERE assignment_id=? "
            "AND (input_tokens IS NULL OR input_tokens = 0)",
            (
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens,
                assignment_id,
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # Column may not exist yet (pre-migration DB or test fixtures).
        pass


def mark_assignment_interactive(assignment_id: str) -> None:
    """#546/#665: flag the row as interactive — routes to the daemon when set.

    Called from :func:`coord.interactive.finalize_interactive_exit` so the
    TUI can reliably show "Max (subscription)" without misidentifying old
    automated rows that also lack ``cost_usd`` / token data.  Silently
    no-ops when the row doesn't exist or the column is missing (pre-migration
    DB).
    """
    if not assignment_id:
        return
    svc = _board_service()
    resp = _route_write(
        svc, "/assignment-usage", {"assignment_id": assignment_id, "is_interactive": True}
    )
    if resp is not None:
        return
    _mark_assignment_interactive_local(assignment_id)


def _mark_assignment_interactive_local(assignment_id: str) -> None:
    """Write is_interactive=1 directly to the local DB.  Called by the daemon endpoint."""
    if not assignment_id:
        return
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE assignments SET is_interactive=1 WHERE assignment_id=?",
            (assignment_id,),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # Column may not exist on a pre-migration DB.
        pass


def set_test_plan(
    assignment_id: str,
    plan: dict,
    *,
    branch_head: str | None = None,
) -> None:
    """#342/#349: persist a generated smoke-test plan on the assignment row.

    ``plan`` must be a valid plan dict (keys ``steps`` and ``blockers``).
    Stored as JSON-encoded TEXT in the ``test_plan`` column.  Silently
    no-ops when the row doesn't exist — matches the pattern used by the
    other ``update_assignment_*`` helpers.

    ``branch_head`` is the git HEAD SHA of the worker's branch at the time
    the plan was generated.  The TUI compares this against the current local
    branch HEAD to detect staleness and re-generate when needed.  When
    ``branch_head`` is ``None`` the column is explicitly reset to NULL so no
    stale SHA from a previous generation persists.

    Idempotent: calling again with a new plan overwrites the previous value.
    """
    if not assignment_id:
        return
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET test_plan=?, test_plan_branch_head=? "
        "WHERE assignment_id=?",
        (json.dumps(plan), branch_head, assignment_id),
    )
    conn.commit()


def get_test_plan(assignment_id: str) -> dict | None:
    """#342 Phase A: read back the cached smoke-test plan for an assignment.

    Returns ``None`` when the row doesn't exist, the column is NULL
    (plan not yet generated), or the stored JSON is malformed.

    **Daemon-aware (#906):** routes to ``GET /assignment-test-plan`` when a
    ``board_service`` is configured so a thin client (e.g. running
    ``--smoke-of`` for a local checkout but with the canonical DB on the
    daemon) reads the real cached plan rather than returning None from an
    empty local DB.  Fails-OPEN on error (returns None and lets the smoke
    briefing fall back to "no plan found").
    """
    if not assignment_id:
        return None
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import post_record  # noqa: PLC0415

            resp = post_record(
                svc, "/assignment-test-plan", {"assignment_id": assignment_id}
            )
            raw = resp.get("test_plan")
            if not raw:
                return None
            try:
                value = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                return None
            return value if isinstance(value, dict) else None
        except Exception:  # noqa: BLE001 — fail-open; smoke briefing handles None
            _log.warning(
                "#906: get_test_plan: daemon read failed for %s, using local",
                assignment_id,
            )
    return _get_test_plan_local(assignment_id)


def _get_test_plan_local(assignment_id: str) -> dict | None:
    """Local-DB read for :func:`get_test_plan`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT test_plan FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        return None
    raw = row["test_plan"] if hasattr(row, "keys") else row[0]
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def set_assignment_failure_reason(assignment_id: str, reason: str) -> None:
    """#618: persist a short launch-failure reason on the assignment row.

    Called immediately when an interactive session fails to start (e.g.
    ``git worktree add`` raises "branch already checked out at <path>") so
    the TUI can explain the red box even without a log file.

    Also marks the row terminal (``status='failed'``, ``finished_at=now``) so
    the stale-session reaper does not have to pick it up later — the operator
    sees the failure immediately without waiting for the next reconcile sweep.

    Silently no-ops when the column is missing (pre-migration DB) or when
    the row doesn't exist.

    **Daemon-aware (#906):** routes to ``POST /assignment-failure-reason`` when
    a ``board_service`` is configured so the terminal mark lands on the shared
    DB rather than the thin client's empty local one.  Fails-OPEN on HTTP error
    — the row was already written by ``record_dispatched_assignment`` via its
    own daemon route, so a missed failure-reason is recoverable (the assignment
    stays ``running`` until the next reconcile sweep).
    """
    if not assignment_id:
        return
    svc = _board_service()
    try:
        resp = _route_write(
            svc,
            "/assignment-failure-reason",
            {"assignment_id": assignment_id, "reason": reason},
        )
    except Exception as _e:  # noqa: BLE001
        import httpx as _httpx  # noqa: PLC0415
        if isinstance(_e, _httpx.HTTPError):
            _log.warning(
                "#906: set_assignment_failure_reason: daemon write failed "
                "(deploy-lag?), falling back to local: %s", _e
            )
            resp = None
        else:
            raise
    if resp is not None:
        return
    _set_assignment_failure_reason_local(assignment_id, reason)


def _set_assignment_failure_reason_local(assignment_id: str, reason: str) -> None:
    """Local-DB write for :func:`set_assignment_failure_reason`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    conn = get_connection()
    now = time.time()
    try:
        conn.execute(
            "UPDATE assignments SET failure_reason=?, status='failed', finished_at=? "
            "WHERE assignment_id=?",
            (reason[:512], now, assignment_id),  # cap at 512 chars — one-liner
        )
        conn.commit()
    except sqlite3.OperationalError:
        # Column may not exist on a pre-migration DB — best-effort.
        pass


def mark_review_posted(assignment_id: str) -> None:
    """Record that this review assignment's findings have been successfully posted.

    Sets ``review_posted_at`` on the assignment row.  Idempotent — calling
    it again after it's already set is harmless (the timestamp won't change
    because the UPDATE only fires when the row exists).

    **Daemon-aware (#905):** routes to ``POST /review-posted`` when a
    ``board_service`` is configured so the timestamp lands on the shared DB
    rather than the thin client's empty local one.
    """
    svc = _board_service()
    resp = _route_write(svc, "/review-posted", {"assignment_id": assignment_id})
    if resp is not None:
        return
    _mark_review_posted_local(assignment_id)


def _mark_review_posted_local(assignment_id: str) -> None:
    """Local-DB write for :func:`mark_review_posted`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET review_posted_at=? WHERE assignment_id=?",
        (time.time(), assignment_id),
    )
    conn.commit()


def load_done_reviews_needing_post(repo_name: str | None = None) -> list[dict]:
    """Return done review assignments whose findings have not yet been posted.

    A review assignment needs posting when:
    - ``type = 'review'``
    - ``status = 'done'``
    - ``review_posted_at IS NULL``

    Optionally filtered to a single repo by *repo_name*.

    Returns dicts in the same format as :func:`load_dispatched` (keyed by
    ``assignment_id``, ``machine_name``, ``repo_github``, ``issue_number``,
    ``review_target``, etc.).

    **Daemon-aware (#905):** when a ``board_service`` is configured the local
    SQLite is empty/stale — the canonical assignments live on the daemon.
    Reads them from the ``GET /board`` payload so a thin client running
    ``coord notify`` or ``coord post-pending-reviews`` finds the real
    candidates instead of an empty list and therefore captures the verdict.
    Falls back to local on daemon-host (no board_service) or fetch failure.
    """
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_board_payload  # noqa: PLC0415

            payload = fetch_board_payload(svc)
            results: list[dict] = []
            for a in payload.get("assignments", []):
                if (
                    a.get("type") == "review"
                    and a.get("status") == "done"
                    and not a.get("review_posted_at")
                    and (repo_name is None or a.get("repo_name") == repo_name)
                ):
                    results.append({
                        "assignment_id": a.get("assignment_id"),
                        "machine_name": a.get("machine_name", ""),
                        "repo_name": a.get("repo_name", ""),
                        "repo_github": a.get("repo_github"),
                        "issue_number": a.get("issue_number", 0),
                        "issue_title": a.get("issue_title", ""),
                        "files_likely": a.get("files_allowed") or [],
                        "briefing": a.get("briefing") or "",
                        "model": a.get("model"),
                        "type": a.get("type", "review"),
                        "required_gates": a.get("required_gates") or [],
                        "dispatched_at": a.get("dispatched_at"),
                        "review_of_assignment_id": a.get("review_of_assignment_id"),
                        "review_target": a.get("review_target"),
                        "status": a.get("status"),
                    })
            return results
        except Exception:  # noqa: BLE001 — daemon unreachable → local fallback
            pass
    return _load_done_reviews_needing_post_local(repo_name=repo_name)


def _load_done_reviews_needing_post_local(repo_name: str | None = None) -> list[dict]:
    """Local-DB read for :func:`load_done_reviews_needing_post`.

    Used on the daemon host (local DB is canonical) or as the offline fallback.
    """
    conn = get_connection()
    if repo_name:
        rows = conn.execute(
            "SELECT * FROM assignments "
            "WHERE type='review' AND status='done' AND review_posted_at IS NULL "
            "AND repo_name=? ORDER BY finished_at",
            (repo_name,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM assignments "
            "WHERE type='review' AND status='done' AND review_posted_at IS NULL "
            "ORDER BY finished_at",
        ).fetchall()
    return [_row_to_dispatched_dict(row) for row in rows]


# ── Plan persistence ────────────────────────────────────────────────────────────

def save_plan(assignment_id: str, plan_dict: dict) -> None:
    """Persist a parsed WorkerPlan for *assignment_id*.

    **Thin-client note (#906):** this function writes the local ``plans``
    table directly.  It is called from two paths:
    - ``coord.notify.post_transition`` — covered by the ``COORD_NOTIFY_ON_DAEMON``
      whole-command reroute; on a thin client ``coord notify`` runs the whole
      function on the daemon, so this local write is correct.
    - ``coord.reconcile._capture_plan_best_effort`` — only reached from the
      daemon's passive tick loop (``serve_app._passive_tick``); always local.

    The guard fires if a caller bypasses both reroutes.
    """
    _thin_client_local_board_guard("save_plan")
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO plans (assignment_id, plan_data) VALUES (?, ?)",
        (assignment_id, json.dumps(plan_dict)),
    )
    conn.commit()


def load_plans() -> dict[str, dict]:
    """Return all saved plans as ``{assignment_id: plan_dict}``."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM plans").fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        try:
            result[row["assignment_id"]] = json.loads(row["plan_data"])
        except (json.JSONDecodeError, TypeError):
            pass
    return result


# ── Board persistence ──────────────────────────────────────────────────────────

def save_board(board: Board) -> Path:
    """Persist the board to the database.

    Note: this function mutates assignments that lack an ``assignment_id``,
    generating a deterministic fallback ID and writing it back to the
    assignment object in-place.
    """
    _thin_client_local_board_guard("save_board")
    conn = get_connection()
    with conn:
        for a in board.active + board.completed:
            if not a.assignment_id:
                # Generate a deterministic fallback ID for assignments that were
                # created without one (e.g. directly in tests).
                a.assignment_id = (
                    f"anon-{a.machine_name}-{a.repo_name}-{a.issue_number}"
                )
            conn.execute(_UPSERT_SQL, _assignment_upsert_params(a))
        # NOTE: we intentionally never DELETE here.  The assignments table is
        # append-only ground truth.  A partial board snapshot (e.g. from
        # coord status loading only recent assignments) must not wipe rows that
        # simply weren't included in the snapshot.  Explicit archival/pruning
        # should be a separate operation if ever needed.
        # Save round_number and mark that the board has been initialised
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('round_number', ?)",
            (str(board.round_number),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES ('board_initialized', '1')"
        )
    return BOARD_FILE  # Legacy return value


def load_board() -> Board | None:
    """Load the board from the database.

    Returns ``None`` if no board has been saved yet (``board_initialized``
    meta key absent), preserving the old "no board.json" → None semantics.
    """
    _thin_client_local_board_guard("load_board")
    conn = get_connection()
    row = conn.execute(
        "SELECT value FROM board_meta WHERE key = 'board_initialized'"
    ).fetchone()
    if row is None:
        return None
    return _query_board(conn)


def _query_board(conn: sqlite3.Connection) -> Board:
    """Build a Board from the current assignments table (no review_state inference)."""
    # Load all plans keyed by assignment_id
    plan_rows = conn.execute("SELECT assignment_id, plan_data FROM plans").fetchall()
    plans_by_id: dict[str, dict] = {
        r["assignment_id"]: json.loads(r["plan_data"]) for r in plan_rows
    }

    rows = conn.execute("SELECT * FROM assignments").fetchall()
    round_number_row = conn.execute(
        "SELECT value FROM board_meta WHERE key = 'round_number'"
    ).fetchone()
    round_number = int(round_number_row["value"]) if round_number_row else 0
    # #749: shared row→Board assembly core — see coord._board_mapping.assemble_board.
    return _assemble_board(rows, plans_by_id, round_number)


def build_board() -> Board:
    """Reconstruct a Board from the database.

    In the SQLite world this is equivalent to :func:`load_board` but always
    returns a Board (never None).  Also infers ``review_state`` for completed
    work assignments by joining against review-type assignments.
    """
    _thin_client_local_board_guard("build_board")
    conn = get_connection()
    board = _query_board(conn)
    _infer_review_state(board, conn)
    return board


def register_milestone_drain(*, repo_name: str, tracking_issue: int) -> None:
    """Register a milestone for daemon auto-drain — routes to the daemon when set.

    Called once, by a non-dry-run bulk ``coord milestone dispatch`` (#769
    Phase 1) — the single explicit approval that lets the daemon's tick loop
    (``coord.serve_app._milestone_drain_tick``, opt-in via
    ``coordinator.yml`` ``milestone.auto_dispatch``) keep recomputing and
    dispatching this milestone's ready frontier as declared-order
    dependencies complete, with no further per-issue approval. Idempotent —
    registering an already-registered ``(repo_name, tracking_issue)`` pair is
    a no-op.
    """
    svc = _board_service()
    resp = _route_write(
        svc, "/milestone-drain",
        {"repo_name": repo_name, "tracking_issue": tracking_issue},
    )
    if resp is not None:
        return
    _register_milestone_drain_local(repo_name=repo_name, tracking_issue=tracking_issue)


def _register_milestone_drain_local(*, repo_name: str, tracking_issue: int) -> None:
    conn = get_connection()
    with conn:
        drains = _load_milestone_drains_raw(conn)
        key = (repo_name, tracking_issue)
        if not any(
            (d.get("repo_name"), d.get("tracking_issue")) == key for d in drains
        ):
            drains.append({"repo_name": repo_name, "tracking_issue": tracking_issue})
            conn.execute(
                "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
                "('milestone_drains', ?)",
                (json.dumps(drains),),
            )


def list_milestone_drains() -> list[dict]:
    """List milestones currently registered for daemon auto-drain.

    Local-DB only (no thin-client routing) — the only caller is the daemon's
    own tick loop, which always runs against the canonical DB directly.
    """
    conn = get_connection()
    return _load_milestone_drains_raw(conn)


def _load_milestone_drains_raw(conn: sqlite3.Connection) -> list[dict]:
    row = conn.execute(
        "SELECT value FROM board_meta WHERE key = 'milestone_drains'"
    ).fetchone()
    if row is None:
        return []
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def deregister_milestone_drain(*, repo_name: str, tracking_issue: int) -> None:
    """Remove a milestone from the active-drain registry.

    Local-DB only — called by the daemon's tick loop once a milestone's
    whole work order reaches a terminal state (:func:`coord.
    milestone_dispatch.is_milestone_complete`).
    """
    conn = get_connection()
    with conn:
        drains = _load_milestone_drains_raw(conn)
        key = (repo_name, tracking_issue)
        remaining = [
            d for d in drains
            if (d.get("repo_name"), d.get("tracking_issue")) != key
        ]
        conn.execute(
            "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
            "('milestone_drains', ?)",
            (json.dumps(remaining),),
        )


def _infer_review_state(board: Board, conn: sqlite3.Connection) -> None:
    """Set review_state on completed work assignments from their linked reviews.

    Thin SQLite wrapper: fetch the review rows + notified ids, then delegate to
    the storage-neutral core (``coord._board_mapping.infer_review_state``) so the
    daemon/client path applies the identical logic (#584).
    """
    review_rows = conn.execute(
        "SELECT assignment_id, review_of_assignment_id, status FROM assignments "
        "WHERE type = 'review' AND review_of_assignment_id IS NOT NULL"
    ).fetchall()
    notified_rows = conn.execute("SELECT assignment_id FROM notifications").fetchall()
    notified_ids = {r["assignment_id"] for r in notified_rows}
    _infer_review_state_core(board, review_rows, notified_ids)


def update_issue_labels(repo_name: str, issue_number: int, labels: list[str]) -> bool:
    """Update the issues cache's labels after a GitHub label change — routes to
    the daemon when ``board_service`` is set (#601), else writes the local DB.

    On a thin client the local DB is retired, so `coord ready`/`backlog`/`refine`/
    `track` changing a label would otherwise never reach the daemon's issues
    table and the TUI Pipeline (which reads it) wouldn't reflect the move.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-labels",
        {"repo_name": repo_name, "issue_number": issue_number, "labels": labels},
    )
    if resp is not None:
        return bool(resp.get("updated"))
    return _update_issue_labels_local(repo_name, issue_number, labels)


def _update_issue_labels_local(
    repo_name: str, issue_number: int, labels: list[str]
) -> bool:
    """Update the local ``issues`` row's labels column after a successful
    GitHub label change.

    Returns ``True`` when a row was updated, ``False`` when no row matched (the
    issue isn't in the local cache yet — it'll be inserted on the next sync; not
    an error here).  Does not touch ``state`` or ``synced_at`` — only ``labels``.
    """
    conn = get_connection()
    cursor = conn.execute(
        "UPDATE issues SET labels = ? WHERE repo_name = ? AND number = ?",
        (json.dumps(sorted(set(labels))), repo_name, issue_number),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_cached_issue_labels(repo_name: str, issue_number: int) -> list[str] | None:
    """Return the local cache's label list for an issue, or ``None`` if the
    issue isn't cached (or its ``labels`` column can't be parsed).

    Read-only lookup against the local ``issues`` table — never calls GitHub.
    Used to compute an accurate before/after delta for CLI echo messages
    (e.g. ``coord issue label``'s "labels updated: +{...} -{...}" summary),
    since ``apply_issue_labels`` only returns the post-change label set.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT labels FROM issues WHERE repo_name = ? AND number = ?",
        (repo_name, issue_number),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["labels"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return None


def apply_issue_labels(
    repo_name: str,
    issue_number: int,
    *,
    add: set[str],
    remove: set[str],
    repo_github: str | None = None,
) -> tuple[list[str], bool]:
    """Add and/or remove arbitrary labels on an issue through the seam (#802).

    Routes to the daemon (``POST /issue-label``) when ``board_service`` is
    set, else writes locally. Returns ``(new_labels, changed)`` where
    ``changed`` is ``True`` when at least one label was added or removed.

    Tolerates already-present ``add`` labels and already-absent ``remove``
    labels (idempotent — no error raised). Updates the local ``issues``
    cache so the TUI reflects the change without waiting for ``coord sync``.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-label",
        {
            "repo_name": repo_name,
            "issue_number": issue_number,
            "add": sorted(add),
            "remove": sorted(remove),
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return resp.get("labels", []), bool(resp.get("changed"))
    return _apply_issue_labels_local(
        repo_name, issue_number,
        add=add, remove=remove,
        repo_github=repo_github,
    )


def _apply_issue_labels_local(
    repo_name: str,
    issue_number: int,
    *,
    add: set[str],
    remove: set[str],
    repo_github: str | None = None,
) -> tuple[list[str], bool]:
    """Backend adapter: write the label change to GitHub then mirror the new
    label set into the local ``issues`` cache.

    Returns ``(new_labels, changed)``; callers that need no-op detection use
    ``changed``. This is the seam endpoint the daemon calls directly — it
    never recurses back out over HTTP.
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    new_labels, changed = github_ops.change_issue_labels(
        slug, issue_number, add=add, remove=remove
    )
    _update_issue_labels_local(repo_name, issue_number, new_labels)
    return new_labels, changed


def create_issue(
    repo_name: str,
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
    repo_github: str | None = None,
) -> dict:
    """Create a new GitHub issue through the issue-tracker seam (#802).

    Routes to the daemon (``POST /issue-create``) when ``board_service`` is
    set, else creates locally. Returns a dict with ``number`` and ``url``.
    Also inserts the new issue into the local ``issues`` cache so the TUI
    reflects it on the next refresh without waiting for ``coord sync``.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-create",
        {
            "repo_name": repo_name,
            "title": title,
            "body": body,
            "labels": labels or [],
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return resp
    return _create_issue_local(
        repo_name, title, body, labels=labels, repo_github=repo_github
    )


def _create_issue_local(
    repo_name: str,
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
    repo_github: str | None = None,
) -> dict:
    """Backend adapter: create the issue on GitHub then insert it into the
    local ``issues`` cache so the TUI sees it immediately.

    Returns ``{"number": N, "url": "..."}``. This is the seam endpoint the
    daemon calls directly — it never recurses back out over HTTP.
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    result = github_ops.create_issue(slug, title, body, labels=labels or [])

    # Mirror the new issue into the local cache (best-effort in intent — the
    # GitHub write above is authoritative and a missing row just gets filled
    # on the next sync — but left unguarded, matching the sibling
    # _edit_issue_content_local's cache write: a real typo/schema-drift bug
    # in this hand-written SQL should surface, not vanish behind a bare except).
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO issues
            (repo_name, number, title, body, state, labels, synced_at,
             milestone_number, milestone_title)
        VALUES (?, ?, ?, ?, 'open', ?, ?, NULL, NULL)
        ON CONFLICT (repo_name, number) DO UPDATE SET
            title     = excluded.title,
            body      = excluded.body,
            state     = 'open',
            labels    = excluded.labels,
            synced_at = excluded.synced_at
        """,
        (
            repo_name,
            result["number"],
            title,
            body,
            json.dumps(sorted(labels or [])),
            time.time(),
        ),
    )
    conn.commit()
    return result


def get_issue_test_mode(repo_name: str, issue_number: int) -> str | None:
    """Return the test-mode policy for an issue from the issues cache.

    Reads the ``test-mode:smoke`` / ``test-mode:auto`` label from the ``issues``
    table row.  Returns ``"smoke"``, ``"auto"``, or ``None`` (no label set — the
    caller should treat ``None`` as *old behaviour*, i.e. respect
    ``smoke_tests.auto_queue`` from the config).

    Does not call GitHub directly.  The cache is kept current by
    ``github_ops.set_test_mode_label``, so the value is fresh whenever the TUI
    has dispatched a headless session after #685.

    **Daemon-aware (#906):** routes to ``POST /issue-test-mode`` when a
    ``board_service`` is configured.  This function's caller,
    ``coord.reconcile.reconcile()`` (not the similarly-named, genuinely
    daemon-tick-only ``reconcile_completed_assignments()`` — an earlier
    version of this docstring conflated the two), is reached unconditionally
    from the thin-client-reachable ``coord resume`` command
    (``coord/commands/lifecycle.py``). Without daemon routing, a thin client's
    empty local ``issues`` table would return ``None`` here and silently
    auto-dispatch a headless smoke test for an issue explicitly labeled
    ``test-mode:smoke``. Fails-OPEN on error (returns ``None``, same as "no
    label set" — matches pre-#906 local-DB-miss behaviour).
    """
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import post_record  # noqa: PLC0415

            resp = post_record(
                svc,
                "/issue-test-mode",
                {"repo_name": repo_name, "issue_number": issue_number},
            )
            value = resp.get("test_mode")
            return value if value in ("auto", "smoke") else None
        except Exception:  # noqa: BLE001 — fail-open; caller respects auto_queue
            _log.warning(
                "#906: get_issue_test_mode: daemon read failed for %s#%s, using local",
                repo_name, issue_number,
            )
    return _get_issue_test_mode_local(repo_name, issue_number)


def _get_issue_test_mode_local(repo_name: str, issue_number: int) -> str | None:
    """Local-DB read for :func:`get_issue_test_mode`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT labels FROM issues WHERE repo_name = ? AND number = ?",
        (repo_name, issue_number),
    ).fetchone()
    if row is None:
        return None
    try:
        labels: list[str] = json.loads(row["labels"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if "test-mode:auto" in labels:
        return "auto"
    if "test-mode:smoke" in labels:
        return "smoke"
    return None


def edit_issue_content(
    repo_name: str,
    issue_number: int,
    *,
    title: str | None = None,
    body: str | None = None,
    repo_github: str | None = None,
) -> bool:
    """Edit an issue's title and/or body through the issue-tracker seam.

    Routes to the daemon (`POST /issue-edit`) when ``board_service`` is set,
    else writes locally. The actual TRACKER write (GitHub via `gh` today;
    GitLab / bare-DB-as-tracker later) lives in the ``_local`` impl, so the
    backend stays behind one seam — the same boundary the chat-about-issue
    session edits through, never raw `gh`.

    Returns True when something was written, False on a no-op (no fields given).
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-edit",
        {
            "repo_name": repo_name,
            "issue_number": issue_number,
            "title": title,
            "body": body,
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return bool(resp.get("updated"))
    return _edit_issue_content_local(
        repo_name, issue_number, title=title, body=body, repo_github=repo_github
    )


def write_milestone(
    repo_name: str,
    *,
    number: int | None = None,
    title: str | None = None,
    description: str | None = None,
    due_on: str | None = None,
    repo_github: str | None = None,
) -> dict:
    """Create or edit a GitHub milestone through the milestone-tracker seam
    (#645, mirrors ``edit_issue_content``).

    Routes to the daemon (``POST /milestone-edit``) when ``board_service`` is
    set, else writes locally. ``number=None`` **creates** a new milestone;
    ``number=<int>`` **edits** an existing one — the same shape as
    ``coord milestone create``/``coord milestone edit``. Returns the
    milestone's JSON dict (``number``, ``title``, ``description``,
    ``due_on``, ...) from the tracker backend.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/milestone-edit",
        {
            "repo_name": repo_name,
            "number": number,
            "title": title,
            "description": description,
            "due_on": due_on,
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return resp
    return _write_milestone_local(
        repo_name,
        number=number,
        title=title,
        description=description,
        due_on=due_on,
        repo_github=repo_github,
    )


def _write_milestone_local(
    repo_name: str,
    *,
    number: int | None = None,
    title: str | None = None,
    description: str | None = None,
    due_on: str | None = None,
    repo_github: str | None = None,
) -> dict:
    """Backend adapter (GitHub today): create or edit a milestone via
    ``github_ops``.

    Unlike ``_edit_issue_content_local`` there is no local cache row to
    mirror — per #645's store decision, milestones stay GitHub-native and
    the DB remains a read-cache of ``issues.milestone_number/title`` only
    (no new write tables). Raises ``ValueError`` when creating without a
    title (mirrors the CLI's own required-field validation, so a daemon
    thin-client call that skips the CLI still fails loudly instead of
    silently calling ``gh api`` with a blank title).
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    if number is None:
        if not (title or "").strip():
            raise ValueError("creating a milestone requires a title")
        return github_ops.create_milestone(
            slug, title, description=description, due_on=due_on
        )
    return github_ops.edit_milestone(
        slug, number, title=title, description=description, due_on=due_on
    )


def _edit_issue_content_local(
    repo_name: str,
    issue_number: int,
    *,
    title: str | None = None,
    body: str | None = None,
    repo_github: str | None = None,
) -> bool:
    """Backend adapter (GitHub today): write the issue's title/body to the
    tracker, then mirror it into the local ``issues`` cache so the TUI reflects
    the edit on its next refresh without waiting for a full `coord sync`."""
    if title is None and body is None:
        return False
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    github_ops.edit_issue(slug, issue_number, title=title, body=body)

    # Mirror into the cache (best-effort: the tracker write above is
    # authoritative; a missing cache row just gets filled on the next sync).
    conn = get_connection()
    sets: list[str] = []
    params: list[object] = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if body is not None:
        sets.append("body = ?")
        params.append(body)
    params.extend([repo_name, issue_number])
    conn.execute(
        f"UPDATE issues SET {', '.join(sets)} WHERE repo_name = ? AND number = ?",
        tuple(params),
    )
    conn.commit()
    return True


def upsert_open_issues(repo_name: str, issues: list[dict]) -> None:
    """Persist open issues for a repo into the issues table — routes to the
    daemon when ``board_service`` is set (#601), else writes the local DB.

    On a thin client `coord sync` (and the TUI's `r` refresh) fetches from
    GitHub fine but must forward the upsert to the daemon, or the canonical
    issue cache the TUI reads never updates.
    """
    svc = _board_service()
    resp = _route_write(svc, "/issues-sync", {"repo_name": repo_name, "issues": issues})
    if resp is not None:
        return
    _upsert_open_issues_local(repo_name, issues)


def _upsert_open_issues_local(repo_name: str, issues: list[dict]) -> None:
    """Persist open issues for a repo into the local issues table.

    ``issues`` is the list of dicts returned by ``github_ops.get_open_issues``:
    each dict has at minimum ``number``, ``title``, ``body``, and ``labels``
    (a list of label dicts with a ``name`` key).

    All rows for this repo are first marked closed; then the supplied open
    issues are upserted with ``state='open'``.  This means issues closed on
    GitHub since the last sync will disappear from the Pending group on the
    next ``coord plan``.

    #771 review: the close-marking UPDATE below also stamps ``synced_at`` for
    rows that are transitioning ``open -> closed`` on *this* sync. Without
    that, a row's ``synced_at`` stayed frozen at whenever it was last synced
    while still open (the upsert below only refreshes ``synced_at`` for
    issues present in the current fetch, i.e. still-open ones) — so the
    7-day prune below effectively measured "days since last confirmed open,"
    not "days since closed," silently shrinking (sometimes to ~zero) the
    grace period consumers (e.g. the TUI's milestone DAG view) rely on to
    still find a just-closed issue in this cache. Already-closed rows are
    excluded from this stamp (``WHERE state = 'open'`` — the pre-flip state)
    so their clock keeps counting from when *they* closed, and the prune
    below still reclaims them on schedule.
    """
    conn = get_connection()
    now = time.time()
    # Mark all current open issues for this repo as closed (stamping
    # synced_at = now for exactly the rows flipping state right now); the
    # upsert below will reopen those still present in the fetched list.
    conn.execute(
        "UPDATE issues SET state = 'closed', synced_at = ? WHERE repo_name = ? AND state = 'open'",
        (now, repo_name),
    )
    # Prune closed issues synced more than 7 days ago to keep the DB lean.
    conn.execute(
        "DELETE FROM issues WHERE repo_name = ? AND state = 'closed' AND synced_at < ?",
        (repo_name, now - 7 * 86400),
    )
    for issue in issues:
        labels = json.dumps(
            [lbl["name"] for lbl in issue.get("labels", []) if isinstance(lbl, dict)]
        )
        # #406: milestone is either {number, title} or None.
        milestone = issue.get("milestone") or {}
        milestone_number = milestone.get("number") if milestone else None
        milestone_title = milestone.get("title") if milestone else None
        conn.execute(
            """
            INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at,
                                milestone_number, milestone_title)
            VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
            ON CONFLICT (repo_name, number) DO UPDATE SET
                title            = excluded.title,
                body             = excluded.body,
                state            = 'open',
                labels           = excluded.labels,
                synced_at        = excluded.synced_at,
                milestone_number = excluded.milestone_number,
                milestone_title  = excluded.milestone_title
            """,
            (
                repo_name,
                issue["number"],
                issue.get("title", ""),
                issue.get("body", "") or "",
                labels,
                now,
                milestone_number,
                milestone_title,
            ),
        )
    # #603: the per-issue context digest is short-lived — drop it for any issue
    # of this repo no longer open (closed, or already pruned from `issues`).
    # Keyed off the open set (not state='closed') so it's robust regardless of
    # the 7-day prune above.  Forgotten on close.
    conn.execute(
        "DELETE FROM issue_context WHERE repo_name = ? AND issue_number NOT IN "
        "(SELECT number FROM issues WHERE repo_name = ? AND state = 'open')",
        (repo_name, repo_name),
    )
    conn.commit()


# ── Per-issue rolling context digest (#603) ─────────────────────────────────────

# Deterministic curation budget for the rendered digest (Phase 1/4).  Pins are
# always kept; non-pinned notes fill the remaining slots newest-first and the
# whole block is char-capped.  Kept small on purpose — this rides the TOP of
# every agent briefing, so it must stay short.
ISSUE_CONTEXT_MAX_ENTRIES = 12
ISSUE_CONTEXT_MAX_CHARS = 2500


def add_issue_context_entry(
    repo_name: str,
    issue_number: int,
    body: str,
    *,
    pinned: bool = False,
    source: str | None = None,
) -> int | None:
    """Append a per-issue context entry — routes to the daemon when
    ``board_service`` is set (#603), else writes the local DB.

    Returns the new entry id on the local path; ``None`` when routed (the
    daemon owns the autoincrement) or when *body* is blank.
    """
    body = (body or "").strip()
    if not body:
        return None
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-context",
        {
            "action": "add",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "body": body,
            "pinned": pinned,
            "source": source,
        },
    )
    if resp is not None:
        return resp.get("entry_id")
    return _add_issue_context_entry_local(
        repo_name, issue_number, body, pinned=pinned, source=source
    )


def _add_issue_context_entry_local(
    repo_name: str,
    issue_number: int,
    body: str,
    *,
    pinned: bool = False,
    source: str | None = None,
) -> int:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO issue_context "
        "(repo_name, issue_number, pinned, source, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (repo_name, issue_number, 1 if pinned else 0, source, body.strip(), time.time()),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def set_issue_context_pin(
    repo_name: str, issue_number: int, entry_id: int, pinned: bool
) -> bool:
    """Pin/unpin one entry — routes to the daemon when set.  Returns whether a
    row was updated."""
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-context",
        {
            "action": "pin",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "entry_id": entry_id,
            "pinned": pinned,
        },
    )
    if resp is not None:
        return bool(resp.get("updated"))
    return _set_issue_context_pin_local(repo_name, issue_number, entry_id, pinned)


def _set_issue_context_pin_local(
    repo_name: str, issue_number: int, entry_id: int, pinned: bool
) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "UPDATE issue_context SET pinned = ? "
        "WHERE id = ? AND repo_name = ? AND issue_number = ?",
        (1 if pinned else 0, entry_id, repo_name, issue_number),
    )
    conn.commit()
    return cur.rowcount > 0


def clear_issue_context(repo_name: str, issue_number: int) -> int:
    """Delete all context entries for an issue — routes to the daemon when set.
    Returns the number of rows removed (0 when routed)."""
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-context",
        {
            "action": "clear",
            "repo_name": repo_name,
            "issue_number": issue_number,
        },
    )
    if resp is not None:
        return int(resp.get("deleted") or 0)
    return _clear_issue_context_local(repo_name, issue_number)


def _clear_issue_context_local(repo_name: str, issue_number: int) -> int:
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM issue_context WHERE repo_name = ? AND issue_number = ?",
        (repo_name, issue_number),
    )
    conn.commit()
    return cur.rowcount


def replace_issue_context(
    repo_name: str, issue_number: int, entries: list[dict]
) -> None:
    """Atomically replace ALL context entries for an issue (used by `coord
    context curate`) — routes to the daemon when set.  *entries* is an ordered
    list of ``{body, pinned?, source?}`` dicts."""
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-context",
        {
            "action": "replace",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "entries": entries,
        },
    )
    if resp is not None:
        return
    _replace_issue_context_local(repo_name, issue_number, entries)


def _replace_issue_context_local(
    repo_name: str, issue_number: int, entries: list[dict]
) -> None:
    conn = get_connection()
    conn.execute(
        "DELETE FROM issue_context WHERE repo_name = ? AND issue_number = ?",
        (repo_name, issue_number),
    )
    now = time.time()
    for i, e in enumerate(entries):
        body = (e.get("body") or "").strip()
        if not body:
            continue
        conn.execute(
            "INSERT INTO issue_context "
            "(repo_name, issue_number, pinned, source, body, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            # +i·µs keeps the supplied order stable under the created_at sort.
            (repo_name, issue_number, 1 if e.get("pinned") else 0,
             e.get("source"), body, now + i * 1e-6),
        )
    conn.commit()


def list_issue_context(repo_name: str, issue_number: int) -> list[dict]:
    """Return an issue's raw context entries (oldest-first) — routes to the
    daemon when set, else reads the local DB.  Each entry:
    ``{id, pinned, source, body, created_at}``."""
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_issue_context  # noqa: PLC0415

        return fetch_issue_context(svc, repo_name, issue_number)
    return _list_issue_context_local(repo_name, issue_number)


def _list_issue_context_local(repo_name: str, issue_number: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, pinned, source, body, created_at FROM issue_context "
        "WHERE repo_name = ? AND issue_number = ? ORDER BY created_at",
        (repo_name, issue_number),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "pinned": bool(r["pinned"]),
            "source": r["source"],
            "body": r["body"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def render_issue_context_entries(
    entries: list[dict],
    *,
    max_entries: int = ISSUE_CONTEXT_MAX_ENTRIES,
    max_chars: int = ISSUE_CONTEXT_MAX_CHARS,
) -> str:
    """Render raw entries into the markdown digest block (pure function): pinned
    criticals first (oldest-first, so the foundational pin stays on top), then
    non-pinned notes newest-first, total capped at *max_entries* and the whole
    block char-capped.  Returns "" when there are no entries (caller omits the
    section).  Shared by the briefing read-path and ``coord context show``.
    """
    if not entries:
        return ""
    pinned = sorted(
        (e for e in entries if e.get("pinned")), key=lambda e: e.get("created_at") or 0
    )
    notes = sorted(
        (e for e in entries if not e.get("pinned")),
        key=lambda e: e.get("created_at") or 0,
        reverse=True,
    )
    note_slots = max(0, max_entries - len(pinned))

    def _fmt(e: dict) -> str:
        tag = "📌 " if e.get("pinned") else ""
        src = f"  _[{e['source']}]_" if e.get("source") else ""
        return f"- {tag}{(e.get('body') or '').strip()}{src}"

    lines = [_fmt(e) for e in pinned] + [_fmt(e) for e in notes[:note_slots]]
    dropped = len(notes) - note_slots
    if dropped > 0:
        lines.append(f"- _… {dropped} older note(s) trimmed — `coord context show` for all_")
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = (
            block[:max_chars].rstrip()
            + "\n- _… (truncated — `coord context show` for full context)_"
        )
    return block


def render_issue_context(
    repo_name: str,
    issue_number: int,
    *,
    max_entries: int = ISSUE_CONTEXT_MAX_ENTRIES,
    max_chars: int = ISSUE_CONTEXT_MAX_CHARS,
) -> str:
    """Render an issue's curated context digest (routes the list read to the
    daemon when set).  Returns "" when empty.  This is what the briefing
    read-path prepends and what ``coord fix-briefing`` includes."""
    return render_issue_context_entries(
        list_issue_context(repo_name, issue_number),
        max_entries=max_entries,
        max_chars=max_chars,
    )


def issue_context_block(repo_name: str, issue_number: int) -> str:
    """The full briefing section (header + digest) prepended to the TOP of every
    agent briefing (#603), or "" when there is no context.

    This is the read-path: it carries findings from earlier attempts on the
    issue (cross-repo dependencies, failed approaches, hard constraints) so the
    next agent doesn't rediscover or contradict them.  FULLY fail-soft — this
    runs on the dispatch hot path, so ANY failure (daemon miss, DB hiccup,
    cross-thread conn) degrades to "no block" and never breaks a dispatch.
    """
    try:
        digest = render_issue_context(repo_name, issue_number)
    except Exception:  # noqa: BLE001 — never let a context read break dispatch
        return ""
    if not digest:
        return ""
    return (
        "## ⚠️ Issue context — READ THIS FIRST\n\n"
        "Findings carried forward from earlier work on this issue (cross-repo "
        "dependencies, approaches already tried, hard constraints). Treat these "
        "as authoritative — do **not** rediscover or contradict them; build on "
        "them. 📌 = pinned critical.\n\n"
        f"{digest}\n\n"
        "---\n\n"
    )


# ── Purge ──────────────────────────────────────────────────────────────────────

def purge_done_assignments(older_than_days: float = 7.0) -> int:
    """Delete old done/failed assignments and closed issues from the database.

    Removes from two tables:

    * ``assignments`` — rows where ``status IN ('done', 'failed')`` and
      ``finished_at < now - older_than_days * 86400``.
    * ``issues`` — rows where ``state = 'closed'`` and
      ``synced_at < now - older_than_days * 86400``.

    Returns the total number of rows deleted across both tables.

    This is the Python-side equivalent of the TUI's 'P' purge action, which
    performs the same DELETE directly via a short-lived Rust rusqlite
    connection.  Exposed here so a future ``coord purge`` CLI command or
    maintenance hook can call it without duplicating the SQL.
    """
    cutoff = time.time() - older_than_days * 86_400
    conn = get_connection()
    deleted_assignments = conn.execute(
        "DELETE FROM assignments "
        "WHERE status IN ('done', 'failed') "
        "AND finished_at IS NOT NULL "
        "AND finished_at < ?",
        (cutoff,),
    ).rowcount
    deleted_issues = conn.execute(
        "DELETE FROM issues "
        "WHERE state = 'closed' "
        "AND synced_at IS NOT NULL "
        "AND synced_at < ?",
        (cutoff,),
    ).rowcount
    # #603: backstop — drop context for any issue no longer open (closed or
    # already purged above), in case drop-on-close was missed.
    conn.execute(
        "DELETE FROM issue_context WHERE (repo_name, issue_number) NOT IN "
        "(SELECT repo_name, number FROM issues WHERE state = 'open')"
    )
    conn.commit()
    return deleted_assignments + deleted_issues
