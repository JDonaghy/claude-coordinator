"""Persistence for coordinator state (proposals, board snapshots)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from coord.models import Proposal

COORD_DIR = Path.home() / ".coord"
PROPOSALS_FILE = COORD_DIR / "pending_proposals.json"


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
