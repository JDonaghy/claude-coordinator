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

# #252: workers emit a SMOKE_TESTS block before exiting.  The whole block
# is captured (greedy across newlines) and parsed below.  Optional `(none
# — change is internal)` form folds to an empty list.
_SMOKE_BLOCK_RE = re.compile(
    r"SMOKE_TESTS:\s*(.*?)\s*END_SMOKE_TESTS",
    re.DOTALL | re.IGNORECASE,
)
_SMOKE_NONE_RE = re.compile(
    r"^\(?\s*none\b.*?(?:internal|change)?\s*\)?\s*$",
    re.IGNORECASE,
)
_SMOKE_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


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


def _extract_smoke_tests_from_text(text: str) -> list[str] | None:
    """#252: pull the SMOKE_TESTS block out of *text*.

    Returns:
      * ``None`` — no block emitted (graceful degradation; the TUI shows
        a "no smoke tests provided" placeholder).
      * ``[]`` — explicit "(none — change is internal)" form.
      * ``list[str]`` — one entry per bullet, stripped of leading "- "/"* ".

    Tolerant of leading/trailing whitespace and stray empty lines.  Picks
    the LAST block in the text — workers occasionally redo their summary
    if they reconsider the change.
    """
    matches = list(_SMOKE_BLOCK_RE.finditer(text))
    if not matches:
        return None
    block = matches[-1].group(1)

    # Try the "(none — change is internal)" short form on the first
    # non-empty line of the captured block.
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _SMOKE_NONE_RE.match(stripped):
            return []
        break

    bullets: list[str] = []
    for line in block.splitlines():
        m = _SMOKE_BULLET_RE.match(line)
        if m:
            item = m.group(1).strip()
            if item:
                bullets.append(item)
    # If the block existed but had no bullets and wasn't the "none" form
    # either, treat as "no smoke tests provided" (None) — the worker
    # didn't actually fill in the template.
    if not bullets:
        return None
    return bullets


def parse_smoke_tests_from_log(
    log_path: str | Path, tail_bytes: int = 65_536,
) -> list[str] | None:
    """#252: read the tail of *log_path* and extract any SMOKE_TESTS block.

    Handles both stream-json logs (decodes assistant text events first)
    and legacy plain-text logs.  Returns the same three-state result as
    :func:`_extract_smoke_tests_from_text`.
    """
    p = Path(log_path)
    if not p.exists():
        return None

    from coord.worker_events import is_stream_json  # noqa: PLC0415

    if is_stream_json(p):
        # Collect assistant text from the structured events.  Workers may
        # emit the block in a single assistant turn, so concatenating all
        # of them is enough.
        from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415
        texts: list[str] = []
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    event = parse_event(line.rstrip("\n"))
                    if event is None or event.type != "assistant":
                        continue
                    t = _assistant_text(event)
                    if t:
                        texts.append(t)
        except OSError:
            return None
        return _extract_smoke_tests_from_text("\n".join(texts))

    # Plain-text path: read the tail (large enough to catch the block
    # even when followed by many turns of build output).
    try:
        size = p.stat().st_size
        with open(p, encoding="utf-8", errors="replace") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # skip partial line
            text = f.read()
    except OSError:
        return None
    return _extract_smoke_tests_from_text(text)


def parse_smoke_tests_from_agent(
    host: str,
    assignment_id: str,
    port: int = 7433,
    timeout: float = 15.0,
) -> list[str] | None:
    """#252: fetch a worker's log via the agent's ``/logs/<id>`` endpoint
    and extract the SMOKE_TESTS block.

    Use this instead of :func:`parse_smoke_tests_from_log` when the worker
    ran on a remote agent and the log isn't on the coordinator's local
    filesystem.  Mirrors :func:`coord.review.parse_review_from_agent` and
    :func:`coord.plan_parser.parse_plan_from_agent`.  Returns the same
    three-state result as :func:`_extract_smoke_tests_from_text`.
    """
    import httpx  # noqa: PLC0415

    url = f"http://{host}:{port}/logs/{assignment_id}"
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        text = resp.text
    except (httpx.HTTPError, httpx.TimeoutException):
        return None
    if not text:
        return None

    # Detect stream-json the same way is_stream_json() does for files.
    stream_json = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stream_json = stripped.startswith("{")
        break

    if stream_json:
        from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415
        texts: list[str] = []
        for line in text.splitlines():
            event = parse_event(line.rstrip("\n"))
            if event is None or event.type != "assistant":
                continue
            t = _assistant_text(event)
            if t:
                texts.append(t)
        return _extract_smoke_tests_from_text("\n".join(texts))

    return _extract_smoke_tests_from_text(text)


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
