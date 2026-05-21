"""Persistence for coordinator state (proposals, board, dispatched assignments,
notifications).

All I/O goes through SQLite via :mod:`coord.db`.  The JSON file constants are
kept as module attributes so that legacy ``monkeypatch.setattr`` calls in tests
don't raise ``AttributeError``, but none of the functions use them for I/O any
more.  Use the ``coord_db`` pytest fixture (defined in tests/conftest.py) to
isolate tests with an in-memory database.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from coord.db import get_connection
from coord.models import Assignment, Board, Proposal, SplitChunk, SplitProposal

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

def _json_loads(s: str | None) -> object:
    if s is None:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def _row_to_assignment(row: object) -> Assignment:
    """Convert a sqlite3.Row (or dict-like) into an Assignment dataclass."""
    d = dict(row)
    return Assignment(
        assignment_id=d.get("assignment_id"),
        machine_name=d["machine_name"],
        repo_name=d["repo_name"],
        issue_number=d["issue_number"],
        issue_title=d["issue_title"],
        status=d.get("status", "running"),
        type=d.get("type", "work"),
        branch=d.get("branch"),
        pr_url=d.get("pr_url"),
        briefing=d.get("briefing") or "",
        files_allowed=_json_loads(d.get("files_allowed")) or [],
        files_forbidden=_json_loads(d.get("files_forbidden")) or [],
        model=d.get("model"),
        dispatched_at=d.get("dispatched_at"),
        finished_at=d.get("finished_at"),
        smoke_test=d.get("smoke_test"),
        smoke_test_reason=d.get("smoke_test_reason"),
        review_state=d.get("review_state"),
        review_of_assignment_id=d.get("review_of_assignment_id"),
        review_target=d.get("review_target"),
        required_gates=_json_loads(d.get("required_gates")) or [],
        plan=_json_loads(d.get("plan")),
        unreachable_count=d.get("unreachable_count") or 0,
    )


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
    )


_UPSERT_SQL = """
    INSERT INTO assignments (
        assignment_id, machine_name, repo_name, issue_number, issue_title,
        status, type, branch, pr_url, briefing,
        files_allowed, files_forbidden, model, dispatched_at, finished_at,
        smoke_test, smoke_test_reason, review_state, review_of_assignment_id,
        review_target, required_gates, plan, unreachable_count
    ) VALUES (
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?, ?, ?
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
        required_gates     = excluded.required_gates
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
    """
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
    }


def record_dispatched(
    *,
    assignment_id: str,
    proposal: Proposal,
    repo_github: str,
) -> None:
    """Record a newly dispatched assignment in the assignments table."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO assignments (
            assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, type, briefing,
            files_allowed, model, dispatched_at, required_gates
        ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
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
        ),
    )
    conn.commit()


def record_dispatched_assignment(
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
            review_target, required_gates
        ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
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
            required_gates = excluded.required_gates""",
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
        ),
    )
    conn.commit()


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
    """
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


# ── Plan persistence ────────────────────────────────────────────────────────────

def save_plan(assignment_id: str, plan_dict: dict) -> None:
    """Persist a parsed WorkerPlan for *assignment_id*."""
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
        # Delete DB rows that were pruned from the board (e.g. by Board.gc()).
        # Without this, pruned assignments survive in the DB and reappear on the
        # next load_board().
        current_ids = [a.assignment_id for a in board.active + board.completed]
        if current_ids:
            placeholders = ",".join("?" * len(current_ids))
            conn.execute(
                f"DELETE FROM assignments WHERE assignment_id NOT IN ({placeholders})",
                current_ids,
            )
        else:
            conn.execute("DELETE FROM assignments")
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
    active: list[Assignment] = []
    completed: list[Assignment] = []
    for row in rows:
        a = _row_to_assignment(row)
        # Attach plan data if present
        if a.assignment_id and a.assignment_id in plans_by_id:
            a.plan = plans_by_id[a.assignment_id]
        if a.status in ("running", "pending"):
            active.append(a)
        else:
            completed.append(a)
    round_number_row = conn.execute(
        "SELECT value FROM board_meta WHERE key = 'round_number'"
    ).fetchone()
    round_number = int(round_number_row["value"]) if round_number_row else 0
    return Board(active=active, completed=completed, round_number=round_number)


def build_board() -> Board:
    """Reconstruct a Board from the database.

    In the SQLite world this is equivalent to :func:`load_board` but always
    returns a Board (never None).  Also infers ``review_state`` for completed
    work assignments by joining against review-type assignments.
    """
    conn = get_connection()
    board = _query_board(conn)
    _infer_review_state(board, conn)
    return board


def _infer_review_state(board: Board, conn: sqlite3.Connection) -> None:
    """Set review_state on completed work assignments from their linked reviews."""
    # Build index: work_assignment_id → (review_status, review_assignment_id)
    review_rows = conn.execute(
        "SELECT assignment_id, review_of_assignment_id, status FROM assignments "
        "WHERE type = 'review' AND review_of_assignment_id IS NOT NULL"
    ).fetchall()
    review_status_for: dict[str, str] = {}
    for row in review_rows:
        review_status_for[row["review_of_assignment_id"]] = row["status"]

    notified_rows = conn.execute("SELECT assignment_id FROM notifications").fetchall()
    notified_ids = {r["assignment_id"] for r in notified_rows}

    for a in board.completed:
        if a.type != "work" or a.assignment_id is None:
            continue
        if a.review_state is not None:
            continue  # explicitly set — don't override
        review_aid = next(
            (r["assignment_id"] for r in review_rows
             if r["review_of_assignment_id"] == a.assignment_id),
            None,
        )
        if review_aid is None:
            continue
        if review_aid in notified_ids or review_status_for.get(
            a.assignment_id, ""
        ) in ("done", "failed"):
            a.review_state = "done"
        else:
            a.review_state = "dispatched"
