"""Fleet-wide health aggregation for the always-visible surfaces (#1631, H-4).

Consumes the ``fleet_health`` block H-3 computes
(:mod:`coord.health.fleet_snapshot`) — either straight off ``GET /board``
(thin client) or reassembled locally from :func:`coord.state.load_machine_health`
(host mode, no ``board_service`` configured, so there is no daemon process to
ask for the fleet-scope checks). Every unit counted here already carries its
own ``severity`` string, chosen by a probe upstream; this module only counts
and picks the worst — it never looks at a check's raw ``values``.

Two renderers consume :class:`FleetHealthSummary`: ``coord status``'s footer
(:func:`render_fleet_footer`, this module) and coord-tui's status-bar
indicator + detail overlay (``tui/src/app``, a from-scratch Rust port of the
same counting rule — see that crate's ``fleet_health`` module for the
mirrored logic and its own doc comment on why it can't just call this file).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from coord.health.models import Severity

# Ascending severity order for the footer's non-OK enumeration — mirrors
# Severity.rank, so "FLEET: WARN 2, CRIT 1" lists the worst last (#1631's
# own example). OK is never listed here: zero non-OK units *is* "OK".
_FOOTER_ORDER = (Severity.UNKNOWN, Severity.WARN, Severity.CRIT)


@dataclass(frozen=True)
class FleetHealthSummary:
    """One aggregate verdict + per-severity counts.

    One *unit* per machine (its own already-rolled-up ``severity`` — H-3's
    ``_effective_severity``: unknown when stale/never-polled/offline, never
    silently carried forward as green) plus one unit per fleet-scope check
    (``fleet_checks``, each already a ``CheckResult.to_dict()``). Counting at
    this granularity — not per individual per-machine check row — is what
    keeps "3 machines, 8 checks each" from reading as "24 problems" when
    really it's "1 machine is unhappy".
    """

    worst: Severity
    counts: dict[str, int]  # "ok"/"unknown"/"warn"/"crit" -> count
    unit_count: int  # total machines + fleet checks contributing


def _severity_of(value: "str | None") -> Severity:
    try:
        return Severity(value or "unknown")
    except ValueError:
        return Severity.UNKNOWN


def summarize_fleet_health(fleet_health: "dict | None") -> FleetHealthSummary:
    """Aggregate a ``fleet_health`` block into one :class:`FleetHealthSummary`.

    *fleet_health* is either the dict at ``/board``'s ``fleet_health`` key
    (schema: ``{"machine_health": [...], "fleet_checks": [...]}``, see
    ``coord.health.fleet_snapshot.FleetHealthSnapshot.to_dict``) or the
    locally-assembled equivalent from :func:`local_fleet_health_block`.
    ``None``/missing keys degrade to "no units" (``worst=OK``, all counts
    zero) rather than raising — a daemon predating #1630 simply never sends
    this key.
    """
    counts = {"ok": 0, "warn": 0, "crit": 0, "unknown": 0}
    worst = Severity.OK
    block = fleet_health or {}
    machine_health = block.get("machine_health") or []
    fleet_checks = block.get("fleet_checks") or []
    units = [m.get("severity") for m in machine_health] + [
        c.get("severity") for c in fleet_checks
    ]
    for raw in units:
        sev = _severity_of(raw)
        counts[sev.value] += 1
        if sev.rank > worst.rank:
            worst = sev
    return FleetHealthSummary(worst=worst, counts=counts, unit_count=len(units))


def render_fleet_footer(summary: FleetHealthSummary) -> str:
    """``FLEET: <state>  (coord health for detail)`` — the ``coord status``
    footer.

    ``OK`` states its OK-ness rather than printing nothing (#1631's own
    framing: silence is indistinguishable from a broken check). Always
    returns a non-empty string — callers should always print it, never
    conditionally skip it.
    """
    if summary.worst is Severity.OK:
        state = "OK"
    else:
        parts = [
            f"{sev.label} {summary.counts[sev.value]}"
            for sev in _FOOTER_ORDER
            if summary.counts[sev.value]
        ]
        state = ", ".join(parts) if parts else "OK"
    return f"FLEET: {state}  (coord health for detail)"


def local_fleet_health_block(machine_names: "list[str]") -> dict:
    """Best-effort ``fleet_health``-shaped block for host mode (no
    ``board_service`` configured — see ``coord.commands.status.status``).

    Per-machine severities come straight from the local DB's
    ``machine_health`` table (written by whichever ``coord serve`` tick loop
    last ran on this host) via the SAME row-assembly H-3's daemon-side
    projection uses (:func:`coord.health.fleet_snapshot._machine_health_rows`)
    — so a machine that never reported reads ``unknown`` here exactly like it
    does on ``/board``, not silently green.

    ``fleet_checks`` is always empty on this path: those probes (board
    latency, phantom-running rows, deploy-lane skew, …) only exist inside a
    live ``coord serve`` process's in-memory ``FleetHealthRefresher`` — a
    separate ``coord status`` invocation has no way to reach into that
    process's memory, and re-running the probes here would mean duplicating
    daemon-only, subprocess-heavy fact-gathering into a read path that must
    stay cheap. A thin client (``board_service`` configured) gets the full
    picture via ``GET /board`` instead.
    """
    from coord.health.fleet_snapshot import _machine_health_rows
    from coord.state import load_machine_health

    raw = load_machine_health()
    rows = _machine_health_rows(machine_names, raw, now=time.time())
    return {"machine_health": rows, "fleet_checks": []}
