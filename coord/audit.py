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

__all__ = ["record_audit"]

# Valid values are documented in the issue but not enforced here — callers
# are all internal (coord.state / coord.issue_store), and rejecting an
# unrecognized value would defeat "never raises into the caller".
_VALID_TIERS = ("business", "operational")


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
    """
    try:
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
