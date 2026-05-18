"""Parse worker progress signals from log output."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


STATUS_RE = re.compile(r"^STATUS:\s*(.+)$", re.MULTILINE)
STUCK_RE = re.compile(r"^STUCK:\s*(.+)$", re.MULTILINE)
CONFIDENCE_RE = re.compile(r"confidence:\s*(high|medium|low)", re.IGNORECASE)


@dataclass
class WorkerProgress:
    updates: list[str] = field(default_factory=list)
    stuck: str | None = None
    warnings: list[str] = field(default_factory=list)
    latest_confidence: str | None = None

    def to_dict(self) -> dict:
        return {
            "updates": self.updates,
            "stuck": self.stuck,
            "warnings": self.warnings,
            "latest_confidence": self.latest_confidence,
        }


def parse_progress(log_path: str | Path, tail_bytes: int = 32_768) -> WorkerProgress:
    """Parse STATUS and STUCK lines from a worker log.

    Reads only the tail of the log to stay cheap on large files.
    """
    p = Path(log_path)
    if not p.exists():
        return WorkerProgress()

    size = p.stat().st_size
    with open(p) as f:
        if size > tail_bytes:
            f.seek(size - tail_bytes)
            f.readline()  # skip partial line
        text = f.read()

    updates = STATUS_RE.findall(text)
    stuck_matches = STUCK_RE.findall(text)

    progress = WorkerProgress(
        updates=updates[-10:],
        stuck=stuck_matches[-1] if stuck_matches else None,
    )

    # Extract latest confidence
    if updates:
        conf = CONFIDENCE_RE.search(updates[-1])
        if conf:
            progress.latest_confidence = conf.group(1).lower()

    # Detect warning patterns
    _detect_warnings(progress, updates)

    return progress


def _detect_warnings(progress: WorkerProgress, all_updates: list[str]) -> None:
    if progress.stuck:
        progress.warnings.append("worker is STUCK and waiting for guidance")

    # Two consecutive low-confidence updates
    confidences = []
    for u in all_updates[-5:]:
        m = CONFIDENCE_RE.search(u)
        if m:
            confidences.append(m.group(1).lower())
    if len(confidences) >= 2 and confidences[-1] == "low" and confidences[-2] == "low":
        progress.warnings.append("confidence dropped to low on consecutive updates")
