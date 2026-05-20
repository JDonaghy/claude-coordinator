"""Parse worker progress signals from log output.

Two paths are supported:

* **stream-json**: when the worker was launched with
  ``--output-format stream-json --verbose`` (the new default), each log line
  is a structured event. We delegate to :mod:`coord.worker_events`.
* **plain text** (legacy): we fall back to the old ``STATUS:``/``STUCK:``
  regex scan for backwards compatibility with logs from older agents and
  for non-claude worker commands used in tests.
"""

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
    """Parse progress from a worker log.

    Detects stream-json automatically and switches parsing strategies. Reads
    only the tail of the log to stay cheap on large files.
    """
    from coord.worker_events import detect_anomalies, is_stream_json, parse_log

    p = Path(log_path)
    if not p.exists():
        return WorkerProgress()

    if is_stream_json(p):
        summary = parse_log(p, tail_bytes=tail_bytes)
        progress = WorkerProgress()
        # Synthesise a single rolling "update" line so coord status keeps
        # showing recent activity for stream-json workers.
        if summary.num_turns or summary.last_tool:
            tool_part = summary.last_tool or "thinking"
            progress.updates.append(f"Turn {summary.num_turns}: {tool_part}")
        # Surface anomaly patterns as warnings.
        progress.warnings.extend(detect_anomalies(p, tail_bytes=tail_bytes))
        if summary.stop_reason and summary.stop_reason not in (
            "end_turn",
            "stop_sequence",
            None,
        ):
            progress.warnings.append(f"unusual stop: {summary.stop_reason}")
        return progress

    # ── Plain-text fallback ───────────────────────────────────────────────
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
