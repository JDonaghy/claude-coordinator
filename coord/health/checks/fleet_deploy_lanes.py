"""Fleet-scope: are the four deploy lanes running the same release? (#1630)

The four lanes named in the issue: each agent's ``~/.coord-venv`` (N of
them), the ``coord-serve`` process on the daemon host, the operator's
``~/.coord-cli-venv``, and the locally-built ``tui/`` binary.  The CLI venv
lane exists *specifically* because it was found three releases stale on
2026-07-29 — silently driving `coord` commands without fixes everyone
believed were live.  This module never probes the filesystem itself: the
daemon's health-poll tick (``coord.serve_app``) gathers the raw per-lane
facts into ``HealthContext.fleet.daemon_host`` (the same "gather in the
context, judge in the probe" split ``build_context``/``local_checkouts``
already use for machine-scope checks) — this module only turns those facts
into a severity, exactly like every other check in this package.

Both checks below are fail-soft toward UNKNOWN, never toward OK: a lane this
daemon has no data for (an agent that never reported, a CLI venv that was
never configured) must not read as "in sync" just because there was nothing
to disagree with.
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check


def _agent_lane_versions(ctx: HealthContext) -> dict[str, str | None]:
    """machine_name -> its reported ``agent_venv`` version, or ``None``.

    ``None`` covers both "machine offline" and "machine online but its
    ``agent_venv`` check didn't run/errored" — both are "no data", not
    "matches everyone else".
    """
    out: dict[str, str | None] = {}
    if ctx.fleet is None:
        return out
    for name, entry in ctx.fleet.machines.items():
        checks = (entry or {}).get("checks") or {}
        version = None
        for r in checks.get("results", []) or []:
            if r.get("check_id") == "agent_venv" and not r.get("error"):
                version = (r.get("values") or {}).get("version") or None
                break
        out[name] = version
    return out


@check(
    id="fleet_deploy_lanes",
    scope="fleet",
    title="deploy lanes",
    order=10,
    description=(
        "Every ~/.coord-venv (per agent), the daemon's own coord-serve "
        "install, and ~/.coord-cli-venv all report the same "
        "claude-coordinator version."
    ),
)
def probe_deploy_lanes(ctx: HealthContext) -> CheckResult:
    if ctx.fleet is None:
        return CheckResult(
            check_id="fleet_deploy_lanes",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no fleet snapshot (fleet checks only run on the daemon)",
        )

    lanes: dict[str, str | None] = dict(_agent_lane_versions(ctx))
    dh = ctx.fleet.daemon_host or {}
    lanes["coord-serve (daemon host)"] = dh.get("coord_serve_version")
    lanes["~/.coord-cli-venv"] = dh.get("cli_venv_version")

    known = {v for v in lanes.values() if v}
    missing = sorted(name for name, v in lanes.items() if not v)

    if not known:
        return CheckResult(
            check_id="fleet_deploy_lanes",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no lane has a resolvable version yet",
            detail=f"no data for: {', '.join(missing)}" if missing else "",
            values={"lanes": lanes},
        )

    if len(known) > 1:
        by_version: dict[str, list[str]] = {}
        for name, v in lanes.items():
            if v:
                by_version.setdefault(v, []).append(name)
        skew_desc = "; ".join(
            f"{v}: {', '.join(sorted(names))}" for v, names in sorted(by_version.items())
        )
        return CheckResult(
            check_id="fleet_deploy_lanes",
            scope="fleet",
            severity=Severity.CRIT,
            headroom=f"{len(known)} versions live across the fleet",
            detail=skew_desc,
            threshold="crit when any lane disagrees",
            values={"lanes": lanes},
        )

    (version,) = known
    headroom = f"all lanes on {version}"
    if missing:
        headroom += f" ({len(missing)} lane(s) with no data)"
    return CheckResult(
        check_id="fleet_deploy_lanes",
        scope="fleet",
        severity=Severity.OK if not missing else Severity.UNKNOWN,
        headroom=headroom,
        detail=f"no data for: {', '.join(missing)}" if missing else "",
        values={"lanes": lanes},
    )


@check(
    id="fleet_tui_binary",
    scope="fleet",
    title="tui binary",
    order=11,
    description=(
        "The locally-built tui/ binary is not older than the tui/ source "
        "tree it was supposedly built from."
    ),
)
def probe_tui_binary(ctx: HealthContext) -> CheckResult:
    if ctx.fleet is None:
        return CheckResult(
            check_id="fleet_tui_binary",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no fleet snapshot (fleet checks only run on the daemon)",
        )

    dh = ctx.fleet.daemon_host or {}
    path = dh.get("tui_binary_path")
    if not path:
        # The daemon always resolves a path (config override, else the README's
        # ~/.local/bin/coord-tui), so this only fires for a snapshot assembled
        # without daemon-host facts at all — "no data", never "in sync".
        return CheckResult(
            check_id="fleet_tui_binary",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no tui binary path in the daemon-host facts",
        )

    binary_mtime = dh.get("tui_binary_mtime")
    source_mtime = dh.get("tui_source_mtime")
    if binary_mtime is None:
        return CheckResult(
            check_id="fleet_tui_binary",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom=f"no binary at {path}",
            detail=(
                "build and install it (`cd tui && cargo build && cp "
                "target/debug/coord-tui ~/.local/bin/coord-tui`), or set "
                "health.tui_binary_path if it lives elsewhere"
            ),
            values={"path": path},
        )
    if source_mtime is None:
        return CheckResult(
            check_id="fleet_tui_binary",
            scope="fleet",
            severity=Severity.OK,
            headroom="binary present (tui/ source tree not found to compare)",
            values={"path": path, "binary_mtime": binary_mtime},
        )

    if source_mtime > binary_mtime:
        stale_hours = (source_mtime - binary_mtime) / 3600.0
        return CheckResult(
            check_id="fleet_tui_binary",
            scope="fleet",
            severity=Severity.WARN,
            headroom=f"binary is {stale_hours:.1f}h older than tui/ source",
            detail="rebuild the tui/ binary — source changed since the last local build",
            threshold="warn when source/ is newer than the built binary",
            values={
                "path": path,
                "binary_mtime": binary_mtime,
                "source_mtime": source_mtime,
            },
        )

    return CheckResult(
        check_id="fleet_tui_binary",
        scope="fleet",
        severity=Severity.OK,
        headroom="up to date with tui/ source",
        values={
            "path": path,
            "binary_mtime": binary_mtime,
            "source_mtime": source_mtime,
        },
    )
