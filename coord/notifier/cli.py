"""``coord notifier`` — inspect and exercise the phone-push channel (#1632).

The notifier's healthy state is **silence**, which makes it the single
easiest subsystem in the fleet to have quietly broken for a month without
noticing.  Every subcommand here exists to make that impossible:
``status`` says whether it is even on and where it would send, ``baselines``
shows what it has learned (and which strata are still cold), ``pending``
shows what quiet hours is holding, and ``test`` proves the transport end to
end without waiting for something to actually go wrong.
"""

from __future__ import annotations

import json
import time

import click

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.notifier import service, store
from coord.notifier.baseline import MIN_SAMPLES
from coord.notifier.digest import is_quiet
from coord.notifier.models import Message
from coord.notifier.transport import NullTransport, build_transport, safe_send


def _fmt(secs: float | None) -> str:
    if secs is None:
        return "-"
    mins = float(secs) / 60.0
    return f"{mins:.0f}m" if mins < 90 else f"{mins / 60.0:.1f}h"


@click.group("notifier")
def notifier_group() -> None:
    """Phone push for "nobody is coming" — status, baselines, test send."""


@notifier_group.command("status")
@_CONFIG_OPTION
@click.option("--json", "as_json", is_flag=True, default=False)
def notifier_status(config_path, as_json: bool) -> None:
    """Show whether the notifier is on, where it sends, and what is held."""
    cfg = _load_config(config_path).notifications
    state = store.load_state()
    now = time.time()
    quiet = is_quiet(cfg.quiet_hours, now)

    payload = {
        "enabled": cfg.enabled,
        "transport": cfg.transport,
        "target": (
            f"{cfg.ntfy_url}/{cfg.ntfy_topic}"
            if cfg.transport == "ntfy" and cfg.ntfy_url and cfg.ntfy_topic
            else None
        ),
        "web_base_url": cfg.web_base_url,
        "quiet_hours": (
            None
            if cfg.quiet_hours is None
            else {
                "start": cfg.quiet_hours.start.strftime("%H:%M"),
                "end": cfg.quiet_hours.end.strftime("%H:%M"),
                "tz": cfg.quiet_hours.tz,
            }
        ),
        "in_quiet_hours": quiet,
        "held_events": len(state.deferred),
        "held_overflow": state.overflow,
        "ledger_entries": len(state.ledger),
        "urgent_drives": sorted(store.urgent_keys(state, now=now)),
        "recent_nudges": len(state.nudges),
        "state_path": str(store.state_path()),
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    click.echo(f"notifier: {'ENABLED' if cfg.enabled else 'disabled'}  "
               f"transport={cfg.transport}")
    if payload["target"]:
        click.echo(f"  target:       {payload['target']}")
    elif cfg.enabled:
        click.secho("  target:       (none — nothing will be delivered)", fg="yellow")
    if cfg.quiet_hours is not None:
        qh = payload["quiet_hours"]
        click.echo(
            f"  quiet hours:  {qh['start']}–{qh['end']} {qh['tz']}"
            f"{'  [ACTIVE — events are being held]' if quiet else ''}"
        )
    else:
        click.echo("  quiet hours:  (none configured — everything delivers immediately)")
    click.echo(f"  held:         {len(state.deferred)} event(s)"
               + (f" (+{state.overflow} dropped)" if state.overflow else ""))
    click.echo(f"  ledger:       {len(state.ledger)} subject/condition pair(s) already told")
    if payload["urgent_drives"]:
        click.echo(f"  urgent:       {', '.join(payload['urgent_drives'])}")
    click.echo(f"  state:        {store.state_path()}")


@notifier_group.command("baselines")
@_CONFIG_OPTION
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option("--cold/--no-cold", default=True, help="Include strata with no baseline yet.")
def notifier_baselines(config_path, as_json: bool, cold: bool) -> None:
    """What the fleet's history says "far too long" means, per stratum.

    Prints the p90 the predicate uses *and* the 2x-median alternative side
    by side — #1632 asks for both to be evaluated against real data before
    either is committed to permanently, which is impossible if only the
    chosen one is ever computed.
    """
    conf = _load_config(config_path)
    baselines = service.compute_baselines(conf)
    rows = [
        b for b in baselines.values() if cold or not b.cold
    ]
    rows.sort(key=lambda b: (b.stratum.repo, b.stratum.type, b.stratum.tier))

    if as_json:
        click.echo(json.dumps(
            [
                {
                    "repo": b.stratum.repo,
                    "type": b.stratum.type,
                    "tier": b.stratum.tier,
                    "samples": b.samples,
                    "cold": b.cold,
                    "duration_threshold_secs": b.duration_threshold,
                    "silence_threshold_secs": b.silence_threshold,
                    "median_secs": b.median_secs,
                    "p90_secs": b.percentile_secs,
                    "p2x_median_secs": b.p2x_median_secs,
                }
                for b in rows
            ],
            indent=2,
        ))
        return

    if not rows:
        click.echo("no completed legs on the board yet — every stratum is cold")
        return
    click.echo(f"{'stratum':<48} {'n':>4}  {'median':>7} {'p90':>7} {'2xmed':>7} "
               f"{'silence':>7}")
    for b in rows:
        marker = "  (cold)" if b.cold else ""
        click.echo(
            f"{str(b.stratum):<48} {b.samples:>4}  "
            f"{_fmt(b.median_secs):>7} {_fmt(b.percentile_secs):>7} "
            f"{_fmt(b.p2x_median_secs):>7} {_fmt(b.silence_threshold):>7}{marker}"
        )
    cold_count = sum(1 for b in rows if b.cold)
    if cold_count:
        click.echo(
            f"\n{cold_count} stratum/strata under {MIN_SAMPLES} samples use a generous "
            "absolute ceiling and say so in the notification text."
        )


@notifier_group.command("pending")
@click.option("--json", "as_json", is_flag=True, default=False)
def notifier_pending(as_json: bool) -> None:
    """Events quiet hours is holding for the next digest."""
    state = store.load_state()
    if as_json:
        click.echo(json.dumps([e.to_dict() for e in state.deferred], indent=2))
        return
    if not state.deferred:
        click.echo("nothing held")
        return
    for event in state.deferred:
        click.echo(f"  {event.title}")
    if state.overflow:
        click.secho(f"  (+{state.overflow} dropped — queue cap reached)", fg="yellow")


@notifier_group.command("tick")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, default=False,
              help="Run the predicate and print what WOULD be sent; deliver nothing.")
def notifier_tick(config_path, dry_run: bool) -> None:
    """Run one notifier tick by hand.

    The daemon does this on its own clock (#1616); this is for shaking out
    false positives without waiting for one.
    """
    conf = _load_config(config_path)
    transport = NullTransport() if dry_run else None
    result = service.tick(conf, transport=transport, persist=not dry_run)
    click.echo(result.summary())
    for event in result.raised:
        click.echo(f"  [{event.condition}] {event.title}")
        for line in event.body.splitlines():
            click.echo(f"      {line}")
    for event, error in result.failed:
        click.secho(f"  UNDELIVERED {event.title}: {error}", fg="yellow")


@notifier_group.command("test")
@_CONFIG_OPTION
def notifier_test(config_path) -> None:
    """Send a test push, proving the transport end to end.

    Exits non-zero on failure. Note that a *failed* test send is the only
    way this channel ever reports its own breakage — by construction it has
    no other symptom, since its healthy state is silence.
    """
    cfg = _load_config(config_path).notifications
    transport = build_transport(cfg)
    message = Message(
        title="coord notifier test",
        body="If you can read this, the fleet notifier can reach your phone.",
        tags=("coord", "white_check_mark"),
        click_url=cfg.web_base_url,
    )
    result = safe_send(transport, message)
    if result.ok:
        click.secho(f"sent via {transport.name}", fg="green")
        return
    click.secho(f"send failed via {transport.name}: {result.error}", fg="red")
    raise SystemExit(1)


@notifier_group.command("urgent")
@_CONFIG_OPTION
@click.argument("repo")
@click.argument("issue", type=int)
@click.option("--clear", is_flag=True, default=False, help="Remove the opt-out.")
@click.option("--hours", type=float, default=None,
              help="Override how long the opt-out lasts (default: notifications.urgent_ttl_hours).")
def notifier_urgent(
    config_path, repo: str, issue: int, clear: bool, hours: float | None
) -> None:
    """Opt one drive out of quiet hours (or clear the opt-out).

    The exception to quiet hours is a **deadline, not a severity**: the
    operator knows when something is time-critical and the system does not.
    Scoped to this one issue, and it expires on its own.
    """
    if clear:
        store.clear_urgent(repo, issue)
        click.echo(f"{repo}#{issue}: quiet-hours opt-out cleared")
        return
    cfg = _load_config(config_path).notifications
    ttl = float(hours if hours is not None else cfg.urgent_ttl_hours) * 3600.0
    expires = time.time() + ttl
    store.mark_urgent(repo, issue, expires_at=expires)
    click.echo(
        f"{repo}#{issue}: will pierce quiet hours until "
        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(expires))}"
    )


__all__ = ["notifier_group"]
