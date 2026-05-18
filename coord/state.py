"""Persistence for coordinator state (proposals, dispatched assignments, notifications)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from coord.models import Proposal

COORD_DIR = Path.home() / ".coord"
PROPOSALS_FILE = COORD_DIR / "pending_proposals.json"
DISPATCHED_FILE = COORD_DIR / "dispatched.json"
NOTIFIED_FILE = COORD_DIR / "notified.json"


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
            "dispatched_at": time.time(),
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
) -> None:
    p = path or NOTIFIED_FILE
    data = load_notified(p)
    data[assignment_id] = {"event": event, "posted_at": time.time()}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")
