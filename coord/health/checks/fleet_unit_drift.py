"""Fleet-scope: does any machine's installed unit drift from the released
unit files? (#1831, #1927)

Aggregates every machine's own `unit_drift` machine-scope check
(:mod:`coord.health.checks.unit_drift`) instead of reading the filesystem
itself — a unit file is inherently a fact about whichever machine it's
installed on, exactly like `cli_venv`/`tui_binary`
(:mod:`coord.health.checks.deploy_lane_facts`) and the fleet aggregator that
reads them (:mod:`coord.health.checks.fleet_deploy_lanes`).

Fail-soft toward UNKNOWN, never toward OK: a fleet with no machine yet
reporting `unit_drift` (every agent still predates #1831) must not read as
"nothing wrong" just because nothing has disagreed.
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check


def _all_unit_drift_results(ctx: HealthContext) -> list[tuple[str, dict]]:
    """[(machine_name, result_dict), ...] for every reported `unit_drift`
    result across the fleet — one entry per (machine, unit), since the
    machine-scope check returns one result per deploy-lane unit."""
    out: list[tuple[str, dict]] = []
    if ctx.fleet is None:
        return out
    for name, entry in ctx.fleet.machines.items():
        checks = (entry or {}).get("checks") or {}
        for r in checks.get("results", []) or []:
            if r.get("check_id") == "unit_drift" and not r.get("error"):
                out.append((name, r))
    return out


@check(
    id="fleet_unit_drift",
    scope="fleet",
    title="unit drift",
    order=12,
    description=(
        "Every machine's installed systemd units match deploy/, and none "
        "shadow the release with an editable checkout on PATH (#1831)."
    ),
)
def probe_fleet_unit_drift(ctx: HealthContext) -> CheckResult:
    if ctx.fleet is None:
        return CheckResult(
            check_id="fleet_unit_drift",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no fleet snapshot (fleet checks only run on the daemon)",
        )

    all_results = _all_unit_drift_results(ctx)
    if not all_results:
        return CheckResult(
            check_id="fleet_unit_drift",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no machine has reported unit_drift data yet",
            detail="every agent predates #1831, or unit_drift is disabled",
        )

    shadowed = [
        (m, r) for m, r in all_results if r.get("severity") == "crit"
    ]
    stale = [(m, r) for m, r in all_results if r.get("severity") == "warn"]
    checked = [(m, r) for m, r in all_results if (r.get("values") or {}).get("installed")]
    # #1927: a machine that diffed against its own git working copy proved
    # nothing — a stale checkout and a stale installed unit agree with each
    # other. Those results arrive as UNKNOWN and must not be counted toward
    # the fleet's green.
    unverified = [
        (m, r)
        for m, r in all_results
        if (r.get("values") or {}).get("installed")
        and (r.get("values") or {}).get("reference_verified") is False
    ]

    values = {
        "machines": sorted({m for m, _ in all_results}),
        "checked_units": len(checked),
        "shadowed": [
            {"machine": m, "unit": r.get("subject")} for m, r in shadowed
        ],
        "stale": [{"machine": m, "unit": r.get("subject")} for m, r in stale],
        "unverified_reference": [
            {"machine": m, "unit": r.get("subject")} for m, r in unverified
        ],
    }

    if shadowed:
        names = ", ".join(f"{m}/{r.get('subject')}" for m, r in shadowed)
        return CheckResult(
            check_id="fleet_unit_drift",
            scope="fleet",
            severity=Severity.CRIT,
            headroom=f"editable checkout shadows the release on PATH: {names}",
            detail=(
                "shutil.which(\"coord\") in a unit's subprocesses resolves a "
                "stale checkout instead of the pinned release — see each "
                "machine's own unit_drift check for the fix"
            ),
            threshold="crit when any unit's PATH lets a .venv/bin entry precede the release",
            values=values,
        )

    if stale:
        names = ", ".join(f"{m}/{r.get('subject')}" for m, r in stale)
        return CheckResult(
            check_id="fleet_unit_drift",
            scope="fleet",
            severity=Severity.WARN,
            headroom=f"installed unit(s) stale vs the released units: {names}",
            detail="cp the reference unit over the installed one and restart it — see the machine-scope unit_drift detail for the exact command",
            threshold="warn when any installed unit's content != the released unit",
            values=values,
        )

    if unverified:
        names = ", ".join(
            sorted({f"{m}/{r.get('subject')}" for m, r in unverified})
        )
        return CheckResult(
            check_id="fleet_unit_drift",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom=f"reference is an unverified working copy on: {names}",
            detail=(
                "these machines diffed their installed units against a git "
                "checkout nothing verifies is current, so a match proves "
                "nothing — both sides go stale together (#1927). Install a "
                "release wheel there; it ships the reference units."
            ),
            values=values,
        )

    return CheckResult(
        check_id="fleet_unit_drift",
        scope="fleet",
        severity=Severity.OK,
        headroom=(
            f"{len(checked)} installed unit(s) across {len(values['machines'])} "
            "machine(s) match the released units"
            if checked
            else "no machine has any deploy-lane unit installed"
        ),
        values=values,
    )
