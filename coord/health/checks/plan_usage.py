"""Max-plan 5h/weekly window headroom (#1628).

Another wrapper, not a rewrite: ``coord.usage_limits`` already probes this
(``claude -p "/usage" --output-format json``, ~700ms, $0, 0 turns) and
already caches it for 60s because the endpoint is itself rate-limited and
Claude Code serves the bars up to an hour stale anyway.  Going through
:func:`~coord.usage_limits.get_plan_limits` means a ``coord health`` run
next to a ``coord drive`` preflight shares one probe rather than racing it
into that rate limit.

Marked ``cost="network"``: it shells out to ``claude`` and hits Anthropic's
API, so it is excluded from the cheap set that has to fit the ~2s registry
budget, and ``coord health --no-network`` skips it entirely (recording that
it was skipped — "we didn't look" must never render as "nothing wrong").

A probe that returns ``status="unknown"`` — no OAuth subscription, an API
key / Bedrock / Vertex auth mode where the windows simply don't apply, a
timeout — reports UNKNOWN, never OK.  Same rule as the dispatch gate: a read
we can't trust is not a clean bill of health.
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import COST_NETWORK, check


@check(
    id="plan_usage",
    scope="machine",
    title="plan usage",
    order=80,
    cost=COST_NETWORK,
    description="Headroom left in the account's Max-plan 5h/weekly windows.",
)
def probe_plan_usage(ctx: HealthContext) -> CheckResult:
    from coord.usage_limits import get_plan_limits  # noqa: PLC0415

    th = ctx.thresholds
    limits = get_plan_limits()

    if not limits.ok:
        return CheckResult(
            check_id="plan_usage",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom="plan windows unavailable (no subscription probe)",
            error=limits.error or "probe returned unknown",
            values=limits.to_dict(),
        )

    windows: list[tuple[str, float, str | None]] = []
    if limits.session_pct is not None:
        windows.append(("session", limits.session_pct, limits.session_resets_at))
    if limits.week_pct is not None:
        windows.append(("week", limits.week_pct, limits.week_resets_at))

    peak = max((pct for _, pct, _ in windows), default=0.0)
    if peak >= th.plan_usage_crit_pct:
        severity = Severity.CRIT
    elif peak >= th.plan_usage_warn_pct:
        severity = Severity.WARN
    else:
        severity = Severity.OK

    parts = []
    for label, pct, resets in windows:
        reset_note = f" resets {resets}" if resets else ""
        parts.append(f"{label} {pct:.0f}% used{reset_note}")
    headroom = "; ".join(parts) or "no windows reported"

    values = limits.to_dict()
    values.update(
        {
            "peak_pct": peak,
            "warn_pct": th.plan_usage_warn_pct,
            "crit_pct": th.plan_usage_crit_pct,
        }
    )

    return CheckResult(
        check_id="plan_usage",
        scope="machine",
        severity=severity,
        headroom=headroom,
        threshold=f"crit at {th.plan_usage_crit_pct:.0f}%",
        values=values,
    )
