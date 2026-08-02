"""Fleet-scope: the board daemon's own ``/board`` latency + payload size (#1630, #1597).

A 5.3MB ``/board`` payload already broke ``coord report-result`` once (#1336)
by blowing a 5s prefetch timeout — this check exists so that regression shows
up as a fleet-health row instead of a mysterious timeout report weeks later.
Like the other fleet checks in this package, the daemon's ``/board`` handler
(``coord.serve_app``) is the thing that actually measures its own latency and
serialized body size on each rebuild; this probe only judges the numbers it's
handed.
"""

from __future__ import annotations

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import human_bytes

# #1336: the 5.3MB payload that broke a 5s prefetch. Warn well before that.
_PAYLOAD_WARN_BYTES = 2 * 1024 * 1024
_PAYLOAD_CRIT_BYTES = 5 * 1024 * 1024
_LATENCY_WARN_MS = 1500.0
_LATENCY_CRIT_MS = 4000.0


@check(
    id="fleet_board_latency",
    scope="fleet",
    title="board latency+size",
    order=20,
    description="The daemon's own /board rebuild latency and serialized payload size.",
)
def probe_board_latency(ctx: HealthContext) -> CheckResult:
    if ctx.fleet is None:
        return CheckResult(
            check_id="fleet_board_latency",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no fleet snapshot (fleet checks only run on the daemon)",
        )

    dh = ctx.fleet.daemon_host or {}
    latency_ms = dh.get("board_latency_ms")
    payload_bytes = dh.get("board_payload_bytes")

    if latency_ms is None and payload_bytes is None:
        return CheckResult(
            check_id="fleet_board_latency",
            scope="fleet",
            severity=Severity.UNKNOWN,
            headroom="no /board build measured yet",
        )

    severity = Severity.OK
    reasons: list[str] = []
    if payload_bytes is not None:
        if payload_bytes >= _PAYLOAD_CRIT_BYTES:
            severity = Severity.CRIT
            reasons.append(f"payload {human_bytes(payload_bytes)}")
        elif payload_bytes >= _PAYLOAD_WARN_BYTES:
            severity = Severity.WARN
            reasons.append(f"payload {human_bytes(payload_bytes)}")
    if latency_ms is not None:
        if latency_ms >= _LATENCY_CRIT_MS:
            severity = Severity.CRIT
            reasons.append(f"latency {latency_ms:.0f}ms")
        elif latency_ms >= _LATENCY_WARN_MS and severity is not Severity.CRIT:
            severity = Severity.WARN
            reasons.append(f"latency {latency_ms:.0f}ms")

    parts = []
    if latency_ms is not None:
        parts.append(f"{latency_ms:.0f}ms")
    if payload_bytes is not None:
        parts.append(human_bytes(payload_bytes))
    headroom = " / ".join(parts) if parts else "no data"
    if severity is not Severity.OK:
        headroom += f" ({', '.join(reasons)})"

    return CheckResult(
        check_id="fleet_board_latency",
        scope="fleet",
        severity=severity,
        headroom=headroom,
        threshold=(
            f"warn at {human_bytes(_PAYLOAD_WARN_BYTES)}/{_LATENCY_WARN_MS:.0f}ms, "
            f"crit at {human_bytes(_PAYLOAD_CRIT_BYTES)}/{_LATENCY_CRIT_MS:.0f}ms"
        ),
        values={"latency_ms": latency_ms, "payload_bytes": payload_bytes},
    )
