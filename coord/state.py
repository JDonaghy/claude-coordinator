"""Persistence for coordinator state (proposals, board, dispatched assignments, notifications)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from coord.models import Assignment, Board, Proposal, SplitProposal, SplitChunk

COORD_DIR = Path.home() / ".coord"
PROPOSALS_FILE = COORD_DIR / "pending_proposals.json"
SPLITS_FILE = COORD_DIR / "pending_splits.json"
DISPATCHED_FILE = COORD_DIR / "dispatched.json"
NOTIFIED_FILE = COORD_DIR / "notified.json"
BOARD_FILE = COORD_DIR / "board.json"
SESSION_FILE = COORD_DIR / "session.json"


def write_session_start() -> None:
    """Write session.json with clean_shutdown=False. Called on first dispatch."""
    COORD_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "started_at": datetime.utcnow().isoformat() + "Z",
        "clean_shutdown": False,
    }
    SESSION_FILE.write_text(json.dumps(data, indent=2) + "\n")


def write_session_end(
    *,
    completed_ids: list[str],
    issues_closed: list[int],
    total_cost_usd: float,
) -> None:
    """Write session.json with clean_shutdown=True and summary."""
    existing = load_session()
    data = {
        "started_at": existing.get("started_at") if existing else None,
        "ended_at": datetime.utcnow().isoformat() + "Z",
        "clean_shutdown": True,
        "completed_this_session": completed_ids,
        "issues_closed": issues_closed,
        "total_cost_usd": total_cost_usd,
    }
    COORD_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, indent=2) + "\n")


def load_session() -> dict | None:
    """Load session.json. Returns None if missing."""
    if not SESSION_FILE.exists():
        return None
    try:
        return json.loads(SESSION_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_proposals(proposals: list[Proposal]) -> Path:
    COORD_DIR.mkdir(parents=True, exist_ok=True)
    data = [asdict(p) for p in proposals]
    PROPOSALS_FILE.write_text(json.dumps(data, indent=2) + "\n")
    return PROPOSALS_FILE


def load_proposals() -> list[Proposal]:
    if not PROPOSALS_FILE.exists():
        return []
    data = json.loads(PROPOSALS_FILE.read_text())
    return [Proposal(**d) for d in data]


def clear_proposals() -> None:
    PROPOSALS_FILE.unlink(missing_ok=True)


def save_split_proposals(splits: list[SplitProposal]) -> Path:
    COORD_DIR.mkdir(parents=True, exist_ok=True)
    data = [asdict(s) for s in splits]
    SPLITS_FILE.write_text(json.dumps(data, indent=2) + "\n")
    return SPLITS_FILE


def load_split_proposals() -> list[SplitProposal]:
    if not SPLITS_FILE.exists():
        return []
    data = json.loads(SPLITS_FILE.read_text())
    return [
        SplitProposal(
            id=d["id"],
            repo_name=d["repo_name"],
            issue_number=d["issue_number"],
            issue_title=d["issue_title"],
            rationale=d.get("rationale", ""),
            chunks=[SplitChunk(**c) for c in d.get("chunks", [])],
        )
        for d in data
    ]


def clear_split_proposals() -> None:
    SPLITS_FILE.unlink(missing_ok=True)


# ── Dispatched-assignment ledger ─────────────────────────────────────────

def load_dispatched(path: Path | None = None) -> list[dict]:
    """Records each successful dispatch so we can map agent state → GH issue."""
    p = path or DISPATCHED_FILE
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def record_dispatched(
    *,
    assignment_id: str,
    proposal: Proposal,
    repo_github: str,
    path: Path | None = None,
) -> None:
    p = path or DISPATCHED_FILE
    records = load_dispatched(p)
    records.append(
        {
            "assignment_id": assignment_id,
            "machine_name": proposal.machine_name,
            "repo_name": proposal.repo_name,
            "repo_github": repo_github,
            "issue_number": proposal.issue_number,
            "issue_title": proposal.issue_title,
            "files_likely": list(proposal.files_likely),
            "briefing": proposal.briefing,
            "model": proposal.model,
            "dispatched_at": time.time(),
        }
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, indent=2) + "\n")


def record_dispatched_assignment(
    *,
    assignment: Assignment,
    repo_github: str,
    path: Path | None = None,
) -> None:
    """Record a dispatched assignment (review, smoke, retry) in the ledger.

    Same ledger as record_dispatched() but accepts an Assignment directly
    instead of a Proposal.
    """
    p = path or DISPATCHED_FILE
    records = load_dispatched(p)
    records.append(
        {
            "assignment_id": assignment.assignment_id,
            "machine_name": assignment.machine_name,
            "repo_name": assignment.repo_name,
            "repo_github": repo_github,
            "issue_number": assignment.issue_number,
            "issue_title": assignment.issue_title,
            "files_likely": list(assignment.files_allowed),
            "briefing": assignment.briefing,
            "model": assignment.model,
            "dispatched_at": assignment.dispatched_at or time.time(),
        }
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, indent=2) + "\n")


# ── Notification ledger ──────────────────────────────────────────────────

def load_notified(path: Path | None = None) -> dict[str, dict]:
    """Map of assignment_id → {event, posted_at}. Tracks which transitions
    have already produced a GitHub comment, so notify is idempotent."""
    p = path or NOTIFIED_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def mark_notified(
    assignment_id: str,
    event: str,
    path: Path | None = None,
    *,
    branch: str | None = None,
) -> None:
    p = path or NOTIFIED_FILE
    data = load_notified(p)
    entry: dict = {"event": event, "posted_at": time.time()}
    if branch:
        entry["branch"] = branch
    data[assignment_id] = entry
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")


# ── Board persistence ───────────────────────────────────────────────────


def save_board(board: Board, path: Path | None = None) -> Path:
    """Atomically persist the board to disk."""
    p = path or BOARD_FILE
    data = {
        "round_number": board.round_number,
        "active": [asdict(a) for a in board.active],
        "completed": [asdict(a) for a in board.completed],
        "saved_at": time.time(),
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)
    return p


def load_board(path: Path | None = None) -> Board | None:
    """Load a previously saved board, or None if no state exists."""
    p = path or BOARD_FILE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    active = [Assignment(**a) for a in data.get("active", [])]
    completed = [Assignment(**a) for a in data.get("completed", [])]
    return Board(
        active=active,
        completed=completed,
        round_number=data.get("round_number", 0),
    )


def build_board(
    dispatched_path: Path | None = None,
    notified_path: Path | None = None,
) -> Board:
    """Reconstruct a Board from the dispatched ledger and notification state.

    This is the ground-truth builder: it reads the append-only dispatched
    log and the notified ledger to determine which assignments are active
    vs completed. Use this when the board file is missing or suspect.
    """
    from coord.comments import EVENT_COMPLETION

    dispatched = load_dispatched(dispatched_path)
    notified = load_notified(notified_path)

    board = Board()
    for record in dispatched:
        aid = record["assignment_id"]
        a = Assignment(
            machine_name=record["machine_name"],
            repo_name=record["repo_name"],
            issue_number=record["issue_number"],
            issue_title=record["issue_title"],
            files_allowed=record.get("files_likely", []),
            briefing=record.get("briefing", ""),
            assignment_id=aid,
            status="running",
            dispatched_at=record.get("dispatched_at"),
            model=record.get("model"),
        )
        n = notified.get(aid)
        if n:
            a.status = "done" if n["event"] == EVENT_COMPLETION else "failed"
            a.finished_at = n.get("posted_at")
            if n.get("branch"):
                a.branch = n["branch"]
            board.completed.append(a)
        else:
            board.active.append(a)

    return board
