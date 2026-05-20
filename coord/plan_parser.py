"""Parse structured plan output from plan-only worker logs.

Plan workers (``type="plan"`` assignments) are instructed to output a fixed
set of section headings.  This module extracts those headings from either a
plain-text log or a stream-json log and returns a :class:`WorkerPlan`
dataclass.

Recognised headings (as defined in ``WORKER_PLAN_PROMPT`` in ``coord.agent``):

    PLAN:        (optional) short one-line summary of the overall plan
    FILES_READ:  comma-separated list of every file the worker examined
    FILES_MODIFY: comma-separated list of files that would need to change
    APPROACH:    concise implementation approach (multi-sentence)
    RISKS:       potential blockers, conflicts, or tricky areas
    ESTIMATE:    rough complexity: trivial | small | medium | large
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# All section keywords the parser recognises.  Order matters for display but
# not for parsing.
SECTION_KEYWORDS = ["PLAN", "FILES_READ", "FILES_MODIFY", "APPROACH", "RISKS", "ESTIMATE"]

# Match a section header at the start of a line, capturing the keyword.
_SECTION_RE = re.compile(
    r"^(" + "|".join(SECTION_KEYWORDS) + r"):\s*",
    re.MULTILINE,
)


@dataclass
class WorkerPlan:
    """Structured plan extracted from a plan-only worker log."""

    plan: str = ""
    files_read: list[str] = field(default_factory=list)
    files_modify: list[str] = field(default_factory=list)
    approach: str = ""
    risks: str = ""
    estimate: str = ""
    raw_text: str = ""

    def is_empty(self) -> bool:
        """True iff no structured sections were found."""
        return not any(
            [
                self.plan,
                self.files_read,
                self.files_modify,
                self.approach,
                self.risks,
                self.estimate,
            ]
        )

    def to_dict(self) -> dict:
        return {
            "plan": self.plan,
            "files_read": self.files_read,
            "files_modify": self.files_modify,
            "approach": self.approach,
            "risks": self.risks,
            "estimate": self.estimate,
            "raw_text": self.raw_text,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorkerPlan:
        return cls(
            plan=data.get("plan", ""),
            files_read=data.get("files_read", []),
            files_modify=data.get("files_modify", []),
            approach=data.get("approach", ""),
            risks=data.get("risks", ""),
            estimate=data.get("estimate", ""),
            raw_text=data.get("raw_text", ""),
        )


# ── Text extraction ──────────────────────────────────────────────────────────


def _extract_text_from_stream_json(log_path: Path) -> str:
    """Concatenate all assistant text blocks from a stream-json log."""
    from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415

    parts: list[str] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                event = parse_event(line.rstrip("\n"))
                if event is None:
                    continue
                if event.type == "assistant":
                    text = _assistant_text(event)
                    if text:
                        parts.append(text)
    except OSError:
        pass
    return "\n".join(parts)


# ── Section parsing ──────────────────────────────────────────────────────────


def _parse_sections(text: str) -> dict[str, str]:
    """Split *text* on section headers and return a keyword → content dict."""
    if not text:
        return {}
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return {}
    result: dict[str, str] = {}
    for i, m in enumerate(matches):
        keyword = m.group(1)
        content_start = m.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        result[keyword] = text[content_start:content_end].strip()
    return result


def _parse_file_list(value: str) -> list[str]:
    """Split a comma-separated file list, stripping blank entries."""
    if not value:
        return []
    return [f.strip() for f in value.split(",") if f.strip()]


# ── Public API ───────────────────────────────────────────────────────────────


def parse_plan_text(text: str) -> WorkerPlan:
    """Parse structured plan sections from raw text.

    Returns a :class:`WorkerPlan` whose ``raw_text`` is always set to *text*,
    and whose section fields are populated from any recognised headings found.
    """
    sections = _parse_sections(text)
    return WorkerPlan(
        plan=sections.get("PLAN", ""),
        files_read=_parse_file_list(sections.get("FILES_READ", "")),
        files_modify=_parse_file_list(sections.get("FILES_MODIFY", "")),
        approach=sections.get("APPROACH", ""),
        risks=sections.get("RISKS", ""),
        estimate=sections.get("ESTIMATE", ""),
        raw_text=text,
    )


def parse_plan_from_log(log_path: str | Path) -> WorkerPlan | None:
    """Parse a :class:`WorkerPlan` from a worker log file.

    Handles both stream-json (``--output-format stream-json``) and plain-text
    log formats.  Returns ``None`` if the file does not exist or contains no
    recognised plan sections.
    """
    from coord.worker_events import is_stream_json  # noqa: PLC0415

    p = Path(log_path)
    if not p.exists():
        return None

    if is_stream_json(p):
        text = _extract_text_from_stream_json(p)
    else:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    plan = parse_plan_text(text)
    return None if plan.is_empty() else plan
