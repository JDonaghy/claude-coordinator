"""Durable, append-only audit trail (#1036 — Audit Trail epic, issue A).

``record_audit()`` is called from the ``state._*_local`` / ``issue_store``
write choke points — the handful of functions every board mutation, whether
it arrives from a thin client or lands directly on the daemon, funnels
through (canonical example: ``state._record_test_verdict_local``).  Hooking
there guarantees one row per real transition regardless of topology, without
touching the ~30 CLI call sites that ultimately reach those writers.

This module is deliberately dumb: one table, one INSERT, best-effort.  It
does not define an event taxonomy beyond the ``tier``/``category`` columns
described in the issue — callers pick their own ``event_type`` strings,
reusing the ``coord:event=`` names from :mod:`coord.comments` where they
already exist (``EVENT_COMPLETION`` etc.) so the audit log and the GitHub
message bus agree on vocabulary.

**Never raises into the caller.**  A board mutation must always succeed even
if the audit write fails (disk full, locked DB, schema drift on an old
checkout) — the write that rode in on is the one that matters; the audit
row is best-effort observability on top of it.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from coord.db import get_connection

_log = logging.getLogger(__name__)

__all__ = ["record_audit", "query_audit_log"]

# Valid values are documented in the issue but not enforced here — callers
# are all internal (coord.state / coord.issue_store), and rejecting an
# unrecognized value would defeat "never raises into the caller".
_VALID_TIERS = ("business", "operational")

# #1037: read-side defaults for the paginated `/audit` endpoint / `coord
# audit` CLI.  Hard-capped so a client can't request the whole table in one
# shot (the endpoint is explicitly NOT the /board "everything" snapshot).
DEFAULT_LIMIT = 200
MAX_LIMIT = 500


def record_audit(
    *,
    tier: str,
    category: str,
    event_type: str,
    actor: str,
    summary: str,
    ts: float | None = None,
    repo: str | None = None,
    issue: int | None = None,
    assignment_id: str | None = None,
    machine: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append one row to ``audit_log``.  Best-effort — swallows all failures.

    ``tier`` is ``"business"`` (a real board transition — dispatch, test
    verdict, review verdict, merge, ...) or ``"operational"`` (daemon-tick
    housekeeping; out of scope for this issue, reserved for a later one).
    ``details`` is JSON-serialized into ``details_json``; pass only
    JSON-safe values (str/int/float/bool/None/dict/list).

    Also performs the opportunistic ``audit.max_rows`` trim (#1036
    deliverable 4) after a successful insert, when the config knob is set
    above its default of ``0`` (unlimited).

    #1038: when ``tier="operational"`` and ``audit.level`` is set to
    ``"business"`` (default ``"operational"``), the row is dropped here —
    the single choke point every operational-tier caller funnels through —
    so callers (the daemon-tick hooks) stay unconditional.  Business-tier
    rows are never gated by this check.
    """
    try:
        if tier == "operational" and _resolve_level() == "business":
            return
        _record_audit_unsafe(
            tier=tier,
            category=category,
            event_type=event_type,
            actor=actor,
            summary=summary,
            ts=ts,
            repo=repo,
            issue=issue,
            assignment_id=assignment_id,
            machine=machine,
            details=details,
        )
    except Exception as exc:  # noqa: BLE001 — audit logging must never break the caller
        _log.warning("record_audit: best-effort write failed: %s", exc)


def _record_audit_unsafe(
    *,
    tier: str,
    category: str,
    event_type: str,
    actor: str,
    summary: str,
    ts: float | None,
    repo: str | None,
    issue: int | None,
    assignment_id: str | None,
    machine: str | None,
    details: dict[str, Any] | None,
) -> None:
    """The actual write.  Split out from :func:`record_audit` so the
    try/except wrapper is the ONLY thing between this and the caller —
    keeps the swallow-and-log behavior in one obvious place."""
    conn = get_connection()
    conn.execute(
        """INSERT INTO audit_log (
            ts, tier, category, event_type, actor,
            repo, issue, assignment_id, machine, summary, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ts if ts is not None else time.time(),
            tier,
            category,
            event_type,
            actor,
            repo,
            issue,
            assignment_id,
            machine,
            summary,
            json.dumps(details) if details is not None else None,
        ),
    )
    conn.commit()
    _maybe_trim(conn)


def _maybe_trim(conn) -> None:
    """Opportunistic cap: when ``audit.max_rows`` is set (> 0), delete the
    oldest rows past that count after every insert.

    Default (``max_rows=0``) is unlimited — this is a no-op in the common
    case.  Config is re-loaded on every call rather than cached: audit
    writes are not hot-loop-frequency (one per board transition, not per
    daemon tick), so the extra YAML parse is cheap relative to the SQLite
    round-trip it accompanies, and it means a config edit takes effect on
    the next write without a process restart.
    """
    max_rows = _resolve_max_rows()
    if max_rows <= 0:
        return
    conn.execute(
        "DELETE FROM audit_log WHERE id NOT IN "
        "(SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)",
        (max_rows,),
    )
    conn.commit()


def _resolve_max_rows() -> int:
    """Read ``audit.max_rows`` from coordinator.yml.  Returns 0 (unlimited)
    on any failure — a missing/invalid config must not block audit writes,
    let alone the board mutation that triggered them."""
    try:
        from coord.config import load as _load_config  # noqa: PLC0415

        cfg = _load_config()
        return max(0, int(cfg.audit.max_rows))
    except Exception:  # noqa: BLE001 — best-effort; unlimited is the safe default
        return 0


def _resolve_level() -> str:
    """Read ``audit.level`` from coordinator.yml.  Returns ``"operational"``
    (the default — capture everything) on any failure, so a missing/invalid
    config never silently suppresses audit rows."""
    try:
        from coord.config import load as _load_config  # noqa: PLC0415

        cfg = _load_config()
        level = cfg.audit.level
        return level if level in _VALID_TIERS else "operational"
    except Exception:  # noqa: BLE001 — best-effort; capture-everything is the safe default
        return "operational"


# ── Read side (#1037): paginated query over audit_log ──────────────────────

_AUDIT_COLUMNS = (
    "id", "ts", "tier", "category", "event_type", "actor",
    "repo", "issue", "assignment_id", "machine", "summary", "details_json",
)


def _encode_cursor(ts: float, row_id: int) -> str:
    """Opaque-ish keyset cursor over ``(ts, id)``.  Not meant to be parsed by
    callers — just round-tripped through ``cursor`` on the next request."""
    return f"{ts!r}:{row_id}"


def _decode_cursor(cursor: str | None) -> tuple[float, int] | None:
    """Parse a cursor produced by :func:`_encode_cursor`.  Returns ``None``
    for a blank/malformed cursor — callers treat that as "first page" rather
    than raising, since a stale/garbled cursor should degrade gracefully
    (start over), not 400 the whole request."""
    if not cursor:
        return None
    try:
        ts_part, id_part = cursor.rsplit(":", 1)
        return float(ts_part), int(id_part)
    except (ValueError, TypeError):
        return None


def _row_to_entry(row: Any) -> dict[str, Any]:
    entry = {col: row[col] for col in _AUDIT_COLUMNS}
    details_raw = entry.pop("details_json")
    entry["details"] = json.loads(details_raw) if details_raw else None
    return entry


def query_audit_log(
    *,
    since: float | None = None,
    until: float | None = None,
    event_type: str | None = None,
    category: str | None = None,
    repo: str | None = None,
    issue: int | None = None,
    assignment_id: str | None = None,
    tier: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Keyset-paginated, newest-first read over ``audit_log``.

    Ordered by ``(ts, id) DESC`` — a cursor (not ``OFFSET``) carries the last
    row of the previous page, so pagination stays O(page size) as the table
    grows.  All filters are optional and AND together.  ``limit`` is clamped
    to ``(1, MAX_LIMIT]``, defaulting to ``DEFAULT_LIMIT``.

    Returns ``{"entries": [...], "next_cursor": str | None, "has_more": bool}``.
    Each entry has ``details_json`` decoded into a ``details`` dict (``None``
    when absent), matching the shape callers actually want on the wire.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(limit, MAX_LIMIT))

    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts <= ?")
        params.append(until)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if category:
        clauses.append("category = ?")
        params.append(category)
    if repo:
        clauses.append("repo = ?")
        params.append(repo)
    if issue is not None:
        clauses.append("issue = ?")
        params.append(issue)
    if assignment_id:
        clauses.append("assignment_id = ?")
        params.append(assignment_id)
    if tier:
        clauses.append("tier = ?")
        params.append(tier)

    decoded_cursor = _decode_cursor(cursor)
    if decoded_cursor is not None:
        cur_ts, cur_id = decoded_cursor
        clauses.append("(ts < ? OR (ts = ? AND id < ?))")
        params.extend([cur_ts, cur_ts, cur_id])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_connection()
    # Fetch one extra row to detect has_more without a second COUNT query.
    rows = conn.execute(
        f"SELECT {', '.join(_AUDIT_COLUMNS)} FROM audit_log {where} "
        "ORDER BY ts DESC, id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    entries = [_row_to_entry(r) for r in rows]
    next_cursor = _encode_cursor(rows[-1]["ts"], rows[-1]["id"]) if has_more and rows else None
    return {"entries": entries, "next_cursor": next_cursor, "has_more": has_more}
