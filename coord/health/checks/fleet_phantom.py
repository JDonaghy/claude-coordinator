"""Fleet-scope: phantom ``running`` board rows (#1630).

A phantom ``running`` row (board says running, the owning agent no longer
agrees) silently disables ``coord retry`` fleet-wide — see
``coord.reconcile.describe_no_candidate_machines`` and ``coord/diagnose.py``
for the interactive/manual side of this failure mode.  This check is the
passive, always-on fleet-health signal for the same class of row: the
daemon's health-poll tick cross-references board ``running`` rows against
each machine's own ``/status`` and hands this probe the count it found — read
only, never a write, never a finalize (that stays ``coord diagnose``'s job).
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check


@check(
    id="fleet_phantom_running",
    scope="fleet",
    title="phantom running rows",
    order=21,
    description=(
        "Board rows marked running whose owning agent no longer reports "
        "them as running — known to silently disable `coord retry`."
    ),
)
def probe_phantom_running(ctx: HealthContext) -> CheckResult:
    if ctx.fleet is None:
        return CheckResult(
            check_id="fleet_phantom_running",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no fleet snapshot (fleet checks only run on the daemon)",
        )

    dh = ctx.fleet.daemon_host or {}
    phantom = dh.get("phantom_running")
    if phantom is None:
        return CheckResult(
            check_id="fleet_phantom_running",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no phantom-row scan yet",
        )

    count = len(phantom)
    if count == 0:
        return CheckResult(
            check_id="fleet_phantom_running",
            scope="fleet",
            severity=Severity.OK,
            headroom="0 phantom running rows",
        )

    sample = ", ".join(
        f"{p.get('repo_name')}#{p.get('issue_number')}@{p.get('machine')}"
        for p in phantom[:5]
    )
    return CheckResult(
        check_id="fleet_phantom_running",
        scope="fleet",
        severity=Severity.CRIT,
        headroom=f"{count} phantom running row{'s' if count != 1 else ''}",
        detail=f"e.g. {sample}" + (", ..." if count > 5 else ""),
        threshold="crit when any phantom row is found",
        values={"count": count, "assignment_ids": [p.get("assignment_id") for p in phantom]},
    )
