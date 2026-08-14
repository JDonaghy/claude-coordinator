"""Local persistence for the portal sync bridge (#1982, epic #836).

The record half of the keystone: every read and write of the four
``portal_*`` tables (:mod:`coord.db`'s ``_ensure_schema``) goes through here,
so :mod:`coord.portal_sync` — the loop — never touches SQL and the ownership
rule is enforceable by reading one file.

**Ownership**, restated because it is the property the whole design hangs on
(``docs/CUSTOMER_PORTAL.md``, "The sync bridge"):

* ``portal_events`` / ``portal_submissions.customer_json`` mirror
  **portal-owned** facts. Written here only from a pulled event; nothing in
  coord ever pushes them back.
* ``portal_outbox`` / the rest of ``portal_submissions`` hold **coord-owned**
  facts. The portal never writes them.

Nothing is co-written, so there is no merge and no split-brain.

**Idempotency.** Inbound rows are keyed on the portal's own event id and
inserted with ``INSERT OR IGNORE``, so replaying a page from a stale cursor
is a no-op. Outbound rows allocate their ``(seq, revision)`` once, at
enqueue, and keep it across every retry — the portal dedupes on
``(submission_id, revision)`` against a watermark, so re-sending an
unconfirmed row can only ever come back ``already_applied``.

This module runs on the **daemon host**, where the local DB is canonical. It
is deliberately not daemon-routed: the sync loop is a daemon-side tick, and a
thin client has no business writing the bridge's cursor. See
:func:`coord.portal_sync.sync_tick`.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

# Outbox row states. `pending` rows are retried forever (a portal outage is
# transient by assumption); `rejected` rows are terminal and need an operator,
# because the portal rejecting a field is a statement about the request, not
# about the network — retrying reproduces it exactly.
STATE_PENDING = "pending"
STATE_APPLIED = "applied"
STATE_REJECTED = "rejected"


def _conn() -> sqlite3.Connection:
    # Imported lazily (and via coord.db, not a cached handle) so tests'
    # `override_connection` in-memory DB is picked up per call.
    from coord.db import get_connection  # noqa: PLC0415

    return get_connection()


# ── inbound: the portal-owned mirror ────────────────────────────────────────


@dataclass(frozen=True)
class PortalEvent:
    """One customer-authored event, exactly as the portal reported it.

    ``payload`` is the raw event dict. The bridge deliberately stores it
    verbatim rather than projecting it into columns: parsing an event into
    coord-side work is a separate, downstream concern (a new submission
    becomes an epic; a sign-off verdict releases Gate A) and that consumer
    must be able to re-read the original rather than whatever subset this
    module thought was interesting.
    """

    event_id: str
    submission_id: str
    kind: str
    occurred_at: str
    payload: dict[str, Any]
    received_at: float
    handled_at: float | None = None


def _event_from_row(row: sqlite3.Row) -> PortalEvent:
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        payload = {}
    return PortalEvent(
        event_id=row["event_id"],
        submission_id=row["submission_id"],
        kind=row["kind"],
        occurred_at=row["occurred_at"],
        payload=payload if isinstance(payload, dict) else {},
        received_at=row["received_at"],
        handled_at=row["handled_at"],
    )


def record_events(events: list[dict[str, Any]], *, now: float | None = None) -> int:
    """Persist a pulled page of events; return how many were NEW.

    ``INSERT OR IGNORE`` on the portal's own event id, so replaying a page
    (daemon restarted before the cursor advanced) inserts nothing and returns
    0 rather than duplicating the inbox.

    An event with no usable id is skipped rather than stored under a
    synthesised key: a synthetic id would defeat the dedupe on the *next*
    replay and silently double-count. It is counted in neither total.

    All rows commit in ONE transaction. The caller advances the cursor only
    after this returns, which is what makes a mid-page crash replay the page
    instead of skipping it.
    """
    if not events:
        return 0
    stamp = time.time() if now is None else now
    conn = _conn()
    inserted = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        event_id = str(ev.get("id") or ev.get("event_id") or "").strip()
        if not event_id:
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO portal_events
                (event_id, submission_id, kind, occurred_at, payload_json,
                 received_at, handled_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                event_id,
                str(ev.get("submission_id") or ""),
                str(ev.get("type") or ev.get("kind") or ""),
                str(ev.get("at") or ev.get("occurred_at") or ""),
                json.dumps(ev, sort_keys=True),
                stamp,
            ),
        )
        inserted += cur.rowcount or 0
    conn.commit()
    return inserted


def unhandled_events(limit: int = 100) -> list[PortalEvent]:
    """Events pulled but not yet consumed by anything coord-side, oldest first."""
    rows = _conn().execute(
        """
        SELECT * FROM portal_events
         WHERE handled_at IS NULL
         ORDER BY received_at ASC, rowid ASC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_event_from_row(r) for r in rows]


def mark_event_handled(event_id: str, *, now: float | None = None) -> None:
    """Stamp an event as consumed. Idempotent — re-stamping is a plain UPDATE."""
    conn = _conn()
    conn.execute(
        "UPDATE portal_events SET handled_at = ? WHERE event_id = ?",
        (time.time() if now is None else now, event_id),
    )
    conn.commit()


def mirror_customer_facts(
    submission_id: str, facts: dict[str, Any], *, now: float | None = None
) -> None:
    """Merge *facts* into a submission's read-only customer mirror.

    Merge rather than replace: a pull page carries whatever changed, not the
    whole record, and clobbering the mirror with a partial event would lose
    the intake text the moment a sign-off verdict arrived.

    Never called with coord-owned fields — see :data:`MIRRORED_KEYS` for the
    filter the sync loop applies before it gets here.
    """
    if not submission_id:
        return
    stamp = time.time() if now is None else now
    conn = _conn()
    _ensure_submission_row(conn, submission_id, stamp)
    row = conn.execute(
        "SELECT customer_json FROM portal_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    try:
        current = json.loads(row["customer_json"]) if row else {}
    except (ValueError, TypeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current.update(facts)
    conn.execute(
        """
        UPDATE portal_submissions
           SET customer_json = ?, updated_at = ?
         WHERE submission_id = ?
        """,
        (json.dumps(current, sort_keys=True), stamp, submission_id),
    )
    conn.commit()


# ── per-submission bookkeeping ──────────────────────────────────────────────


@dataclass(frozen=True)
class SubmissionRecord:
    """Coord's view of one submission: allocators + what the portal CONFIRMED.

    ``last_status`` / ``design_round`` are deliberately "confirmed applied",
    not "last enqueued". The ordering guard reads them, and a guard that
    trusted intent rather than confirmation would wave through exactly the
    case it exists to stop.
    """

    submission_id: str
    last_revision: int
    last_seq: int
    last_status: str
    design_round: int
    open_question: str
    customer: dict[str, Any]
    first_seen_at: float
    updated_at: float


def _submission_from_row(row: sqlite3.Row) -> SubmissionRecord:
    try:
        customer = json.loads(row["customer_json"])
    except (ValueError, TypeError):
        customer = {}
    return SubmissionRecord(
        submission_id=row["submission_id"],
        last_revision=row["last_revision"],
        last_seq=row["last_seq"],
        last_status=row["last_status"],
        design_round=row["design_round"],
        open_question=row["open_question"],
        customer=customer if isinstance(customer, dict) else {},
        first_seen_at=row["first_seen_at"],
        updated_at=row["updated_at"],
    )


def _ensure_submission_row(
    conn: sqlite3.Connection, submission_id: str, stamp: float
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO portal_submissions
            (submission_id, first_seen_at, updated_at)
        VALUES (?, ?, ?)
        """,
        (submission_id, stamp, stamp),
    )


def get_submission(submission_id: str) -> SubmissionRecord | None:
    row = _conn().execute(
        "SELECT * FROM portal_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    return _submission_from_row(row) if row else None


def list_submissions() -> list[SubmissionRecord]:
    rows = _conn().execute(
        "SELECT * FROM portal_submissions ORDER BY first_seen_at ASC, submission_id ASC"
    ).fetchall()
    return [_submission_from_row(r) for r in rows]


def seed_revision(submission_id: str, revision: int, *, now: float | None = None) -> None:
    """Raise a submission's revision allocator to at least *revision*.

    Called when a pulled event tells coord the portal is already at a higher
    revision than coord has ever allocated — which happens the first time a
    submission is seen at all, and after any hand `coord portal push`. Only
    ever raises: a lower value would make the next allocation collide with
    the portal's watermark and come back `already_applied` forever, which is
    the one way a "successful" push can silently drop a fact.
    """
    if revision <= 0:
        return
    stamp = time.time() if now is None else now
    conn = _conn()
    _ensure_submission_row(conn, submission_id, stamp)
    conn.execute(
        """
        UPDATE portal_submissions
           SET last_revision = MAX(last_revision, ?), updated_at = ?
         WHERE submission_id = ?
        """,
        (revision, stamp, submission_id),
    )
    conn.commit()


# ── outbound: the coord-owned queue ─────────────────────────────────────────


@dataclass(frozen=True)
class OutboxRow:
    """One queued coord-owned push."""

    id: int
    submission_id: str
    seq: int
    revision: int
    kind: str
    fields: dict[str, Any]
    announces: str
    requires_kind: str
    state: str
    reason: str
    attempts: int
    enqueued_at: float
    sent_at: float | None


def _outbox_from_row(row: sqlite3.Row) -> OutboxRow:
    try:
        fields = json.loads(row["fields_json"])
    except (ValueError, TypeError):
        fields = {}
    return OutboxRow(
        id=row["id"],
        submission_id=row["submission_id"],
        seq=row["seq"],
        revision=row["revision"],
        kind=row["kind"],
        fields=fields if isinstance(fields, dict) else {},
        announces=row["announces"],
        requires_kind=row["requires_kind"],
        state=row["state"],
        reason=row["reason"],
        attempts=row["attempts"],
        enqueued_at=row["enqueued_at"],
        sent_at=row["sent_at"],
    )


def enqueue(
    submission_id: str,
    kind: str,
    fields: dict[str, Any],
    *,
    announces: str = "",
    requires_kind: str = "",
    now: float | None = None,
) -> OutboxRow:
    """Append one coord-owned fact to the outbox and return the stored row.

    Allocates ``seq`` (per-submission FIFO position) and ``revision`` (the
    portal's dedupe key) in the SAME transaction that inserts the row, so two
    concurrent enqueues cannot hand out the same number.

    Nothing is sent here — :func:`coord.portal_sync.sync_tick` drains the
    queue. That split is what makes the ordering guarantee hold across a
    crash: the intent is durable before any customer-visible effect happens.
    """
    stamp = time.time() if now is None else now
    conn = _conn()
    with conn:  # one transaction: allocate + insert
        _ensure_submission_row(conn, submission_id, stamp)
        row = conn.execute(
            "SELECT last_revision, last_seq FROM portal_submissions "
            "WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        revision = int(row["last_revision"]) + 1
        seq = int(row["last_seq"]) + 1
        conn.execute(
            """
            UPDATE portal_submissions
               SET last_revision = ?, last_seq = ?, updated_at = ?
             WHERE submission_id = ?
            """,
            (revision, seq, stamp, submission_id),
        )
        cur = conn.execute(
            """
            INSERT INTO portal_outbox
                (submission_id, seq, revision, kind, fields_json, announces,
                 requires_kind, state, reason, attempts, enqueued_at, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?, NULL)
            """,
            (
                submission_id,
                seq,
                revision,
                kind,
                json.dumps(fields, sort_keys=True),
                announces,
                requires_kind,
                STATE_PENDING,
                stamp,
            ),
        )
        row_id = int(cur.lastrowid or 0)
    stored = _conn().execute(
        "SELECT * FROM portal_outbox WHERE id = ?", (row_id,)
    ).fetchone()
    return _outbox_from_row(stored)


def pending_outbox(limit: int | None = None) -> list[OutboxRow]:
    """Pending rows in strict per-submission FIFO order, oldest submission first.

    Ordered by ``(submission_id, seq)`` so the drain can walk it and stop at
    the first row of a submission it cannot send — head-of-line blocking is
    the desired behaviour here, per submission and only per submission: a
    stuck design round must not let its own `awaiting-signoff` overtake it,
    and must not stall a different customer's submission.
    """
    sql = (
        "SELECT * FROM portal_outbox WHERE state = ? "
        "ORDER BY submission_id ASC, seq ASC"
    )
    params: tuple[Any, ...] = (STATE_PENDING,)
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)
    return [_outbox_from_row(r) for r in _conn().execute(sql, params).fetchall()]


def outbox_for_submission(submission_id: str) -> list[OutboxRow]:
    rows = _conn().execute(
        "SELECT * FROM portal_outbox WHERE submission_id = ? ORDER BY seq ASC",
        (submission_id,),
    ).fetchall()
    return [_outbox_from_row(r) for r in rows]


def mark_applied(row: OutboxRow, *, now: float | None = None) -> None:
    """Flip a row to ``applied`` and fold its effect into the confirmed record.

    Both writes land in one transaction. ``design_round`` / ``last_status``
    are the guard's inputs, so a row that is `applied` while the submission
    record still says "no design round" would be exactly the window this
    design exists to close.
    """
    stamp = time.time() if now is None else now
    conn = _conn()
    with conn:
        conn.execute(
            """
            UPDATE portal_outbox
               SET state = ?, reason = '', sent_at = ?, attempts = attempts + 1
             WHERE id = ?
            """,
            (STATE_APPLIED, stamp, row.id),
        )
        _ensure_submission_row(conn, row.submission_id, stamp)
        if row.kind == "status":
            conn.execute(
                "UPDATE portal_submissions SET last_status = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (str(row.fields.get("status", "")), stamp, row.submission_id),
            )
        elif row.kind == "design_round":
            round_no = _round_number(row.fields)
            conn.execute(
                """
                UPDATE portal_submissions
                   SET design_round = MAX(design_round, ?), updated_at = ?
                 WHERE submission_id = ?
                """,
                (round_no, stamp, row.submission_id),
            )
        elif row.kind == "question":
            conn.execute(
                "UPDATE portal_submissions SET open_question = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (str(row.fields.get("question", "")), stamp, row.submission_id),
            )


def _round_number(fields: dict[str, Any]) -> int:
    """Best-effort round number out of a design_round payload; 1 if unstated.

    1, not 0: the guard asks "has a design round landed", and a payload that
    simply didn't bother to number itself has still landed one.
    """
    design = fields.get("design_round")
    if isinstance(design, dict):
        for key in ("round", "round_number", "number"):
            value = design.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return 1


def mark_rejected(row: OutboxRow, reason: str, *, now: float | None = None) -> None:
    """Flip a row to the terminal ``rejected`` state with the portal's reason.

    Terminal on purpose: `rejected` means the portal understood the request
    and refused it (an unowned field, a status outside the pinned
    vocabulary). Retrying reproduces it forever and, because the row keeps
    its `seq`, would block every later row for that submission behind an
    error no amount of waiting fixes.
    """
    conn = _conn()
    conn.execute(
        """
        UPDATE portal_outbox
           SET state = ?, reason = ?, sent_at = ?, attempts = attempts + 1
         WHERE id = ?
        """,
        (STATE_REJECTED, reason[:500], time.time() if now is None else now, row.id),
    )
    conn.commit()


def note_attempt(row: OutboxRow, reason: str) -> None:
    """Record a failed-but-retryable send (transport error, portal 5xx).

    Leaves the row ``pending`` — the next tick tries the same ``(seq,
    revision)`` again, which is why a retry can never duplicate a fact.
    """
    conn = _conn()
    conn.execute(
        "UPDATE portal_outbox SET attempts = attempts + 1, reason = ? WHERE id = ?",
        (reason[:500], row.id),
    )
    conn.commit()


def note_hold(row: OutboxRow, reason: str) -> None:
    """Record that a row was withheld by the ordering guard.

    Not an attempt: nothing was sent, and counting it as one would make the
    attempts column read as "the portal keeps failing" when the truth is
    "coord deliberately has not asked yet".
    """
    conn = _conn()
    conn.execute(
        "UPDATE portal_outbox SET reason = ? WHERE id = ?",
        (reason[:500], row.id),
    )
    conn.commit()


# ── cursor + liveness ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SyncState:
    """The bridge's cursor and last-contact timestamps."""

    pull_cursor: str | None = None
    last_pull_at: float | None = None
    last_push_at: float | None = None
    last_heartbeat_at: float | None = None
    last_error: str = ""


def get_sync_state() -> SyncState:
    row = _conn().execute(
        "SELECT * FROM portal_sync_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return SyncState()
    return SyncState(
        pull_cursor=row["pull_cursor"],
        last_pull_at=row["last_pull_at"],
        last_push_at=row["last_push_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        last_error=row["last_error"] or "",
    )


def _update_sync_state(**columns: Any) -> None:
    if not columns:
        return
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO portal_sync_state (id, last_error) VALUES (1, '')"
    )
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(
        f"UPDATE portal_sync_state SET {assignments} WHERE id = 1",  # noqa: S608
        tuple(columns.values()),
    )
    conn.commit()


def set_pull_cursor(cursor: str | None, *, now: float | None = None) -> None:
    """Advance the replay point. Called only AFTER a page's rows have committed."""
    _update_sync_state(
        pull_cursor=cursor, last_pull_at=time.time() if now is None else now
    )


def note_pull(*, now: float | None = None) -> None:
    """Record a completed pull that returned nothing new (cursor unchanged)."""
    _update_sync_state(last_pull_at=time.time() if now is None else now)


def note_push(*, now: float | None = None) -> None:
    _update_sync_state(last_push_at=time.time() if now is None else now)


def note_heartbeat(*, now: float | None = None) -> None:
    _update_sync_state(last_heartbeat_at=time.time() if now is None else now)


def note_error(error: str) -> None:
    _update_sync_state(last_error=error[:500])


def clear_error() -> None:
    _update_sync_state(last_error="")
