"""``coord portal`` — exercise the coord-portal sync bridge client (#2179).

Everything that automatically decides *when* to push a status, and *which*
coord-side work a portal submission maps to, is out of scope here (see
``coord/portal_bridge.py``'s module docstring — that is follow-up work this
issue deliberately did not take on). What exists today is the thin client
plus enough CLI surface to:

* prove the credential pair actually works (``coord portal heartbeat``),
  which is also how an operator manually verifies #2179's acceptance shape
  end to end once ``BRIDGE_CLIENT_ID``/``BRIDGE_CLIENT_SECRET`` are set — a
  submission's ``/deliveries`` row going ``queued`` → ``sent`` in production
  is only reachable at all once something has pushed a status; and
* push one status update by hand (``coord portal push``), so a submission's
  status can be moved even before anything automatic drives it.
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
