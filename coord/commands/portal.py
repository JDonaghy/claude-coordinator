"""``coord portal`` — operate the coord-portal sync bridge (#2179, #1982).

#2179 shipped the thin HTTP client (``coord/portal_bridge.py``) and the three
commands that prove a credential pair works by hand: ``status``,
``heartbeat``, ``push``.

#1982 added the loop those calls were missing — ``coord/portal_sync.py``,
running on the daemon's ``_tick_loop`` — and with it the commands that let an
operator see and drive it without waiting for a tick:

* ``sync`` runs one full pass now (pull → push → heartbeat);
* ``outbox`` shows what is queued, held, or rejected, and why;
* ``events`` shows what has been pulled in;
* ``enqueue-*`` puts a coord-owned fact on the queue, which is the supported
  way to push one — unlike ``push``, the queue enforces the ordering rule
  that keeps a customer from being emailed toward an empty screen (#835);
* ``requeue`` revives a row the drain retired after burning its retry budget.

The state-touching commands (``sync``, ``outbox``, ``events``, ``enqueue-*``,
``requeue``)
read and write the daemon's own ``~/.coord/coord.db`` and are therefore
**daemon-host commands**. Run from a thin client they operate on that box's
empty local DB, which is not where the bridge lives.
"""

from __future__ import annotations

import json

import click

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.portal_bridge import (
    PortalBridgeError,
    SUBMISSION_STATUSES,
    client_from_config,
)


@click.group("portal")
def portal_group() -> None:
    """coord-portal sync bridge — status, manual push, heartbeat (#2179)."""


@portal_group.command("status")
@_CONFIG_OPTION
@click.option("--json", "as_json", is_flag=True, default=False)
def portal_status(config_path, as_json: bool) -> None:
    """Show whether the portal bridge is configured, without sending anything."""
    cfg = _load_config(config_path).portal
    payload = {
        "enabled": cfg.enabled,
        "base_url": cfg.base_url,
        "credentials_set": bool(cfg.bridge_client_id and cfg.bridge_client_secret),
        "timeout_secs": cfg.timeout_secs,
        "max_retries": cfg.max_retries,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not cfg.enabled:
        click.echo("portal: disabled (no 'portal:' block, or portal.enabled: false)")
        return
    click.echo(f"portal: ENABLED  base_url={cfg.base_url}  "
               f"credentials={'set' if payload['credentials_set'] else 'MISSING'}")


@portal_group.command("heartbeat")
@_CONFIG_OPTION
def portal_heartbeat(config_path) -> None:
    """Send one heartbeat, proving the credential pair and base_url work.

    Exits non-zero on failure — a 401 here means BRIDGE_CLIENT_ID/SECRET on
    this side do not match what the portal has (or Cloudflare Access is
    rejecting the request before it even gets there).
    """
    cfg = _load_config(config_path).portal
    client = client_from_config(cfg)
    if client is None:
        click.secho(
            "portal is not enabled — nothing to do (see `coord portal status`)",
            fg="yellow",
        )
        raise SystemExit(1)
    try:
        ok = client.heartbeat()
    except PortalBridgeError as exc:
        click.secho(f"heartbeat failed: {exc}", fg="red")
        raise SystemExit(1) from exc
    if ok:
        click.secho("heartbeat sent", fg="green")
        return
    click.secho("heartbeat sent but portal did not confirm 'ok'", fg="red")
    raise SystemExit(1)


@portal_group.command("push")
@_CONFIG_OPTION
@click.argument("submission_id")
@click.argument("revision", type=int)
@click.argument("status", type=click.Choice(SUBMISSION_STATUSES))
def portal_push(config_path, submission_id: str, revision: int, status: str) -> None:
    """Push one status update by hand: SUBMISSION_ID REVISION STATUS.

    REVISION must be strictly greater than whatever the portal last accepted
    for this submission, or the push comes back `already_applied` (a
    success, not an error — see coord-portal's src/bridge/updates.ts).

    ESCAPE HATCH: this sends immediately and bypasses the outbox, so it also
    bypasses the ordering guard `coord portal enqueue-status` applies.
    Pushing `awaiting-signoff` or `needs-input` this way emails the customer
    whether or not the thing it announces exists (#835). It also leaves the
    local revision allocator behind the portal's watermark until the next
    pull re-seeds it. Prefer `enqueue-status` for anything a customer sees.
    """
    cfg = _load_config(config_path).portal
    client = client_from_config(cfg)
    if client is None:
        click.secho(
            "portal is not enabled — nothing to do (see `coord portal status`)",
            fg="yellow",
        )
        raise SystemExit(1)
    try:
        result = client.push_status(submission_id, revision, status)
    except PortalBridgeError as exc:
        click.secho(f"push failed: {exc}", fg="red")
        raise SystemExit(1) from exc
    if result.ok:
        click.secho(f"{result.outcome}: {submission_id}@{revision} -> {status}", fg="green")
        return
    click.secho(
        f"rejected: {submission_id}@{revision} -> {status} ({result.reason})", fg="red"
    )
    raise SystemExit(1)


# ── #1982: the sync loop's operator surface ─────────────────────────────────


@portal_group.command("sync")
@_CONFIG_OPTION
@click.option("--json", "as_json", is_flag=True, default=False)
def portal_sync_once(config_path, as_json: bool) -> None:
    """Run one full sync pass now: pull, then push, then heartbeat.

    The same pass the daemon runs on its tick — this just does not wait for
    it. Exits non-zero if the pass reported any error, so it can be used as a
    smoke check; the pass itself never raises, and a failure in one phase
    does not stop the other two.

    Daemon-host command: it reads and writes the daemon's ~/.coord/coord.db.
    """
    from coord import portal_sync as _sync  # noqa: PLC0415

    config = _load_config(config_path)
    if not config.portal.enabled:
        click.secho(
            "portal is not enabled — nothing to do (see `coord portal status`)",
            fg="yellow",
        )
        raise SystemExit(1)
    result = _sync.sync_tick(config)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "enabled": result.enabled,
                    "pulled": result.pulled,
                    "applied": result.applied,
                    "rejected": result.rejected,
                    "held": result.held,
                    "heartbeat_ok": result.heartbeat_ok,
                    "errors": result.errors,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        click.echo(result.summary())
        for err in result.errors:
            click.secho(f"  {err}", fg="red")
    if result.errors:
        raise SystemExit(1)


@portal_group.command("outbox")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option(
    "--all", "show_all", is_flag=True, default=False,
    help="Include applied/rejected rows, not just what is still queued.",
)
def portal_outbox(as_json: bool, show_all: bool) -> None:
    """List queued coord-owned pushes and why any of them are held.

    A `pending` row with a reason is HELD, not failing: the ordering guard is
    refusing to announce something to the customer before the thing it
    announces has been confirmed applied.
    """
    from coord import portal_store  # noqa: PLC0415
    from coord.portal_sync import ordering_block_reason  # noqa: PLC0415

    if show_all:
        rows = [
            row
            for sub in portal_store.list_submissions()
            for row in portal_store.outbox_for_submission(sub.submission_id)
        ]
    else:
        rows = portal_store.pending_outbox()

    state = portal_store.get_sync_state()
    if as_json:
        click.echo(
            json.dumps(
                {
                    "cursor": state.pull_cursor,
                    "last_pull_at": state.last_pull_at,
                    "last_push_at": state.last_push_at,
                    "last_heartbeat_at": state.last_heartbeat_at,
                    "last_error": state.last_error,
                    "rows": [
                        {
                            "submission_id": r.submission_id,
                            "seq": r.seq,
                            "revision": r.revision,
                            "kind": r.kind,
                            "state": r.state,
                            "announces": r.announces,
                            "attempts": r.attempts,
                            "reason": r.reason,
                            "held_because": (
                                ordering_block_reason(r)
                                if r.state == portal_store.STATE_PENDING
                                else None
                            ),
                        }
                        for r in rows
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    click.echo(
        f"cursor={state.pull_cursor or '-'}  "
        f"last_heartbeat_at={state.last_heartbeat_at or '-'}"
    )
    if state.last_error:
        click.secho(f"last error: {state.last_error}", fg="red")
    if not rows:
        click.echo("outbox: empty")
        return
    for r in rows:
        held = (
            ordering_block_reason(r)
            if r.state == portal_store.STATE_PENDING
            else None
        )
        line = (
            f"{r.submission_id} seq={r.seq} rev={r.revision} "
            f"{r.kind:<13} {r.state}"
        )
        if held:
            click.secho(f"{line}  HELD — {held}", fg="yellow")
        elif r.state == portal_store.STATE_REJECTED:
            click.secho(f"{line}  {r.reason}", fg="red")
        else:
            click.echo(line)


@portal_group.command("events")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option("--limit", type=int, default=20, show_default=True)
def portal_events(as_json: bool, limit: int) -> None:
    """List pulled, not-yet-consumed customer events (the inbound half)."""
    from coord import portal_store  # noqa: PLC0415

    events = portal_store.unhandled_events(limit=limit)
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "event_id": e.event_id,
                        "submission_id": e.submission_id,
                        "kind": e.kind,
                        "occurred_at": e.occurred_at,
                        "payload": e.payload,
                    }
                    for e in events
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return
    if not events:
        click.echo("no unhandled portal events")
        return
    for e in events:
        click.echo(f"{e.occurred_at or '-'}  {e.submission_id}  {e.kind}  {e.event_id}")


@portal_group.command("enqueue-status")
@click.argument("submission_id")
@click.argument("status", type=click.Choice(SUBMISSION_STATUSES))
def portal_enqueue_status(submission_id: str, status: str) -> None:
    """Queue an up-mapped status for SUBMISSION_ID (sent on the next sync).

    Unlike `push`, this allocates the revision for you and refuses a status
    that would summon the customer to an empty screen — `awaiting-signoff`
    with no design round queued, `needs-input` with no question (#835).
    """
    from coord.portal_sync import PortalSyncError, enqueue_status  # noqa: PLC0415

    try:
        row = enqueue_status(submission_id, status)
    except PortalSyncError as exc:
        click.secho(str(exc), fg="red")
        raise SystemExit(1) from exc
    click.secho(
        f"queued: {row.submission_id} seq={row.seq} rev={row.revision} status={status}",
        fg="green",
    )


@portal_group.command("enqueue-design-round")
@click.argument("submission_id")
@click.argument("payload_json")
def portal_enqueue_design_round(submission_id: str, payload_json: str) -> None:
    """Queue a design round for SUBMISSION_ID. PAYLOAD_JSON is the D1 metadata.

    The mock bundle is an R2 object uploaded out of band; PAYLOAD_JSON is
    expected to carry whatever reference the customer's browser follows, plus
    a `round` number if this is not the first.
    """
    from coord.portal_sync import PortalSyncError, enqueue_design_round  # noqa: PLC0415

    try:
        payload = json.loads(payload_json)
    except ValueError as exc:
        click.secho(f"PAYLOAD_JSON is not valid JSON: {exc}", fg="red")
        raise SystemExit(1) from exc
    try:
        row = enqueue_design_round(submission_id, payload)
    except PortalSyncError as exc:
        click.secho(str(exc), fg="red")
        raise SystemExit(1) from exc
    click.secho(
        f"queued: {row.submission_id} seq={row.seq} rev={row.revision} design_round",
        fg="green",
    )


@portal_group.command("enqueue-question")
@click.argument("submission_id")
@click.argument("question")
def portal_enqueue_question(submission_id: str, question: str) -> None:
    """Queue an open question for SUBMISSION_ID (sent on the next sync)."""
    from coord.portal_sync import PortalSyncError, enqueue_question  # noqa: PLC0415

    try:
        row = enqueue_question(submission_id, question)
    except PortalSyncError as exc:
        click.secho(str(exc), fg="red")
        raise SystemExit(1) from exc
    click.secho(
        f"queued: {row.submission_id} seq={row.seq} rev={row.revision} question",
        fg="green",
    )


@portal_group.command("requeue")
@click.argument("submission_id")
@click.argument("seq", type=int)
def portal_requeue(submission_id: str, seq: int) -> None:
    """Put a retired outbox row (SUBMISSION_ID SEQ) back in the queue.

    The drain retires a row that has burned its retry budget — a payload the
    portal keeps refusing, or an outage that outlasted the budget. That state
    is terminal by design (it also keeps any announcement behind it held,
    which is the fail-closed half of #835), so this is the lever that clears
    it once the underlying problem is fixed. The row gets a fresh revision,
    since the old one may be below the portal's watermark by now.

    Find the seq with `coord portal outbox --all`.
    """
    from coord import portal_store  # noqa: PLC0415

    row = portal_store.requeue(submission_id, seq)
    if row is None:
        click.secho(
            f"no outbox row for {submission_id} seq={seq} "
            f"(list them with `coord portal outbox --all`)",
            fg="red",
        )
        raise SystemExit(1)
    click.secho(
        f"requeued: {row.submission_id} seq={row.seq} rev={row.revision} {row.kind}",
        fg="green",
    )
