"""`coord audit` — CLI query surface over the append-only audit trail (#1037).

Routes through the ``coord.state.list_audit_log`` seam (daemon when
``board_service`` is configured, local DB otherwise) — same pattern as
``coord context show`` — so a thin client never opens ``~/.coord/coord.db``
directly. Human table by default (relative timestamps); ``--json`` for
scripting/tests. Also doubles as the manual + CI verification path for
#1036 (capture) / #1038 (operational tier, once it lands).
"""

from __future__ import annotations

import json as _json
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION


def _parse_timestamp(raw: str | None) -> float | None:
    """Accept an epoch number (``1720000000`` / ``1720000000.5``) or an
    ISO-8601 string (``2026-07-10`` / ``2026-07-10T12:00:00``)."""
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError as e:
        raise click.BadParameter(f"not an epoch number or ISO-8601 timestamp: {raw!r}") from e


def _relative_time(ts: float | None) -> str:
    if ts is None:
        return "-"
    delta = time.time() - ts
    if delta < 0:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    for seconds, unit in (
        (60, "s"), (3600, "m"), (86400, "h"), (86400 * 30, "d"),
    ):
        if delta < seconds:
            prior = {60: 1, 3600: 60, 86400: 3600, 86400 * 30: 86400}[seconds]
            return f"{int(delta // prior)}{unit} ago"
    return f"{int(delta // (86400 * 30))}mo ago"


def _truncate(s: str, width: int) -> str:
    return s if len(s) <= width else s[: width - 1] + "…"


@click.command(
    "audit",
    help="Query the audit trail (#1036/#1037) — dispatch, verdicts, merges, notifications.",
)
@click.option("--since", "since_raw", default=None, help="Epoch seconds or ISO-8601 — only rows at/after this time.")
@click.option("--until", "until_raw", default=None, help="Epoch seconds or ISO-8601 — only rows at/before this time.")
@click.option("--type", "event_type", default=None, help="Filter by event_type (e.g. test_passed, dispatched).")
@click.option("--category", default=None, help="Filter by category (e.g. test, merge, dispatch, review).")
@click.option("--repo", default=None, help="Filter by repo name.")
@click.option("--issue", default=None, type=int, help="Filter by issue number.")
@click.option("--assignment", "assignment_id", default=None, help="Filter by assignment_id.")
@click.option("--tier", default=None, help="Filter by tier (business|operational).")
@click.option("--limit", default=200, show_default=True, type=int, help="Max rows to return (hard-capped at 500).")
@click.option("--cursor", default=None, help="Keyset cursor from a previous run's next_cursor (for manual pagination).")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output the raw {entries, next_cursor, has_more} JSON.")
@_CONFIG_OPTION
def audit(
    since_raw: str | None,
    until_raw: str | None,
    event_type: str | None,
    category: str | None,
    repo: str | None,
    issue: int | None,
    assignment_id: str | None,
    tier: str | None,
    limit: int,
    cursor: str | None,
    output_json: bool,
    config_path: Path,  # noqa: ARG001 — accepted for --config-flag consistency; audit reads need no coordinator.yml
) -> None:
    """Print (or dump as JSON) a page of the audit trail, newest-first."""
    from coord.state import list_audit_log  # noqa: PLC0415

    since = _parse_timestamp(since_raw)
    until = _parse_timestamp(until_raw)

    try:
        result = list_audit_log(
            since=since, until=until, event_type=event_type, category=category,
            repo=repo, issue=issue, assignment_id=assignment_id, tier=tier,
            limit=limit, cursor=cursor,
        )
    except Exception as e:  # noqa: BLE001 — surface a clean CLI error, not a traceback
        click.echo(f"error: audit read failed: {e}", err=True)
        raise SystemExit(1) from e

    if output_json:
        click.echo(_json.dumps(result, indent=2, default=str))
        return

    entries = result.get("entries") or []
    if not entries:
        click.echo("(no audit entries match)")
        return

    click.echo(
        f"{'WHEN':<10} {'TIER':<8} {'CATEGORY':<10} {'EVENT':<20} {'ACTOR':<11} "
        f"{'REPO':<12} {'ISSUE':<7} {'SUMMARY'}"
    )
    for e in entries:
        click.echo(
            f"{_relative_time(e.get('ts')):<10} "
            f"{_truncate(e.get('tier') or '-', 8):<8} "
            f"{_truncate(e.get('category') or '-', 10):<10} "
            f"{_truncate(e.get('event_type') or '-', 20):<20} "
            f"{_truncate(e.get('actor') or '-', 11):<11} "
            f"{_truncate(e.get('repo') or '-', 12):<12} "
            f"{str(e.get('issue') if e.get('issue') is not None else '-'):<7} "
            f"{_truncate(e.get('summary') or '', 60)}"
        )

    if result.get("has_more"):
        click.echo(
            f"\n… more rows available — rerun with --cursor {result.get('next_cursor')!r}"
        )
