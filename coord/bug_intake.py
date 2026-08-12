"""The bug-lane intake contract (#1964, docs/TEST_FIRST_BUG_LANE.md "The
intake contract").

A bug entering the test-first bug lane carries four fields instead of the
feature lane's UX mock:

1. **Expected behaviour** — what should happen, in observable terms.
2. **Actual behaviour** — what happens instead.
3. **Reproduction** — the shortest path to see it.
4. **Evidence** — screenshot, wireframe, or a reference implementation that
   behaves correctly (a sibling backend, or a prior release).

This module is the single source of truth for how those four fields are
rendered into an issue body and parsed back out of one, so the fields
survive as addressable sections rather than prose a later author (human or
agent) has to re-derive:

- ``coord issue create --expected/--actual/--repro/--evidence``
  (:mod:`coord.commands.issues`) calls :func:`format_bug_report` to compose
  the body.
- ``.github/ISSUE_TEMPLATE/bug_report.md`` renders the exact same four
  headings for a human filing straight through the GitHub UI — a
  hand-authored ``contract.md`` (docs/TEST_FIRST_BUG_LANE.md, #1964
  Deliverable 2) is written by reading that issue directly, no parsing
  required. :func:`parse_bug_report` exists for the agent-assisted path and
  for tooling that wants the four fields back out programmatically (e.g. a
  future bug-contract-author briefing) — it is not on the hand-authoring
  critical path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Canonical section headings, in report order. `.github/ISSUE_TEMPLATE/
# bug_report.md` renders these same four strings as its Markdown headings —
# tests/test_bug_intake.py checks the template file against this module
# directly so the two can never drift apart silently.
EXPECTED_HEADING = "Expected behaviour"
ACTUAL_HEADING = "Actual behaviour"
REPRO_HEADING = "Reproduction"
EVIDENCE_HEADING = "Evidence"

#: Ordered (attribute name, heading text) pairs — single source of truth for
#: both the render order (:func:`format_bug_report`) and the parse targets
#: (:func:`parse_bug_report`).
FIELDS: tuple[tuple[str, str], ...] = (
    ("expected", EXPECTED_HEADING),
    ("actual", ACTUAL_HEADING),
    ("repro", REPRO_HEADING),
    ("evidence", EVIDENCE_HEADING),
)


@dataclass(frozen=True)
class BugReport:
    """The four intake fields, parsed out of (or destined for) an issue
    body. See module docstring for what each field means."""

    expected: str
    actual: str
    repro: str
    evidence: str


def format_bug_report(*, expected: str, actual: str, repro: str, evidence: str) -> str:
    """Render the four intake fields as a Markdown issue body, one
    addressable ``## `` section per field, in report order.

    Used by ``coord issue create --expected/--actual/--repro/--evidence`` so
    the fields land in the issue as structured sections rather than a single
    freeform paragraph. Round-trips through :func:`parse_bug_report`.
    """
    values = {
        "expected": expected, "actual": actual, "repro": repro, "evidence": evidence,
    }
    parts = [
        f"## {heading}\n\n{values[attr].strip()}" for attr, heading in FIELDS
    ]
    return "\n\n".join(parts) + "\n"


def parse_bug_report(body: str) -> BugReport | None:
    """Extract the four intake fields back out of an issue body rendered by
    :func:`format_bug_report`, or hand-written to the same four headings.

    Tolerant of heading level (``#`` through ``######``) and of surrounding
    prose before the first heading or after the last section's text, so a
    human-edited issue (extra context added above/below the four sections)
    still parses. Matching is on heading text only, case-insensitively.

    Returns ``None`` when any of the four sections is missing or empty — a
    partial bug report is not a bug report the lane can act on. Callers that
    want a best-effort partial read should not use this function.
    """
    heading_alts = "|".join(re.escape(heading) for _, heading in FIELDS)
    pattern = re.compile(
        rf"^#{{1,6}}\s*({heading_alts})\s*$", re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(body))
    if not matches:
        return None

    by_heading: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        by_heading[m.group(1).strip().lower()] = body[start:end].strip()

    kwargs: dict[str, str] = {}
    for attr, heading in FIELDS:
        value = by_heading.get(heading.lower())
        if not value:
            return None
        kwargs[attr] = value
    return BugReport(**kwargs)
