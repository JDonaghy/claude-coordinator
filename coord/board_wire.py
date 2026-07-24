"""Wire-bounding policy for the ``/board`` collection payload (#1337).

**Invariant 2 of the /board read path: no collection endpoint returns
unbounded text.**  The #762 fix bounded the board's *row count* (retention
cap) — and the payload kept growing anyway, because the growth was in
per-row text size: ``assignments.review_findings`` + ``issues.body`` alone
were ~46 % of a 5.5 MB payload polled every few seconds.  Bounding rows
while text grows per-row is how this failure class (#762 → #715 → #1336)
kept coming back.

This module is the single place the per-field wire policy lives:

* **Preview fields** — ``review_findings`` (body inside the JSON envelope),
  ``test_reason``, ``smoke_test_reason`` — are cut to a preview and flagged
  with ``<field>_truncated: true`` (+ ``<field>_len``).  Consumers that need
  the full text (fix-worker briefings, the TUI findings pane) fetch the
  single-resource detail endpoint ``GET /assignment/{id}``.
* **Bounded documents** — ``issues.body``, ``test_plan`` — get a *high* hard
  cap that today truncates nothing real (issue-body p99 ≈ 9 KB, cap 16 KB)
  but bounds the pathological row, because clients parse these semantically
  (work orders, ``## Files`` globs) and an aggressive prefix cut would break
  those parses.  Full body: ``GET /issue/{repo}/{number}``.

Everything here is wire-only: the DB row, the detail endpoints, and local
(non-daemon) reads are untouched.  The bounded fields are also excluded from
the whole-board upsert's UPDATE clause (``coord.state._UPSERT_SQL``), so a
bounded preview can never round-trip over the full stored text via
``POST /board``.

Enforced by tests/test_board_read_path.py — a payload-budget test fails the
suite if a seeded board's wire exceeds its budget, so instance #4 of this
class shows up as a red test, not a fleet incident.
"""

from __future__ import annotations

import json

# Preview size for operator-facing free text.  Large enough that a short
# review / test reason arrives whole; everything longer is a preview + flag.
PREVIEW_CHARS = 2000
# Hard cap for semantically-parsed documents (issue bodies, test plans).
DOCUMENT_CHARS = 16384

# Appended to truncated *plain-text* fields so a human reading the preview
# (TUI pane, dialog) knows it is one — machine consumers use the flags.
TRUNCATION_NOTICE = "\n… [truncated on the /board wire — full text: detail endpoint]"


def _preview(text: str, cap: int) -> str:
    return text[:cap] + TRUNCATION_NOTICE


def _bound_text_field(row: dict, field: str, cap: int) -> None:
    """Truncate ``row[field]`` to *cap* chars, stamping ``<field>_truncated``
    and ``<field>_len`` when it was cut.  Flags are additive-only (absent when
    nothing was cut) so old clients see an unchanged shape."""
    val = row.get(field)
    if not isinstance(val, str) or len(val) <= cap:
        return
    row[f"{field}_len"] = len(val)
    row[f"{field}_truncated"] = True
    row[field] = _preview(val, cap)


def _bound_review_findings(row: dict, cap: int) -> None:
    """Envelope-aware preview for ``review_findings``.

    The column is a JSON envelope ``{"verdict": ..., "body": ...}`` kept as a
    *raw string* on the wire (the TUI parses it).  Truncating the raw string
    would corrupt the JSON, so parse, preview the body inside, and
    re-serialize — the verdict always survives intact.  A legacy/unparseable
    blob falls back to plain-text truncation.
    """
    raw = row.get("review_findings")
    if not isinstance(raw, str) or len(raw) <= cap:
        return
    row["review_findings_len"] = len(raw)
    row["review_findings_truncated"] = True
    try:
        env = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        env = None
    if isinstance(env, dict) and isinstance(env.get("body"), str):
        env["body"] = _preview(env["body"], cap)
        env["truncated"] = True
        row["review_findings"] = json.dumps(env)
    else:
        row["review_findings"] = raw[:cap]


def _bound_test_plan(row: dict, cap: int) -> None:
    """``test_plan`` is a decoded ``{"steps": [...]}`` object on the wire —
    a prefix cut is meaningless, so a pathological plan is dropped whole
    (flagged); the TUI's existing ``None`` handling shows its placeholder and
    the detail endpoint serves the full plan."""
    val = row.get("test_plan")
    if val is None:
        return
    try:
        size = len(json.dumps(val))
    except (TypeError, ValueError):
        return
    if size <= cap:
        return
    row["test_plan_len"] = size
    row["test_plan_truncated"] = True
    row["test_plan"] = None


def bound_assignment_row(row: dict) -> None:
    """Apply the wire policy to one ``/board`` assignment row (mutates)."""
    _bound_review_findings(row, PREVIEW_CHARS)
    _bound_text_field(row, "test_reason", PREVIEW_CHARS)
    _bound_text_field(row, "smoke_test_reason", PREVIEW_CHARS)
    _bound_text_field(row, "failure_reason", PREVIEW_CHARS)
    _bound_test_plan(row, DOCUMENT_CHARS)


def bound_issue_row(row: dict) -> None:
    """Apply the wire policy to one ``/board`` issue row (mutates)."""
    _bound_text_field(row, "body", DOCUMENT_CHARS)


def bound_board_payload(projection: dict) -> None:
    """Bound every unbounded free-text field in a ``/board`` projection.

    Called by the daemon's board builder AFTER the derived sections
    (milestone work orders, plan roster, epic children) are computed — those
    parse full issue bodies server-side and must see them unbounded.
    """
    for row in projection.get("assignments", ()):
        if isinstance(row, dict):
            bound_assignment_row(row)
    for row in projection.get("issues", ()):
        if isinstance(row, dict):
            bound_issue_row(row)
