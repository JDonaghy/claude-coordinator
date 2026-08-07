"""``coord release verify`` — does every deploy lane on every host actually
reflect the released version? (#1834)

The question this answers did not previously have an answer anywhere. On
2026-08-04, hours after v0.4.105 shipped, four independent readouts all said
the fleet was on 0.4.105 and the board daemon was spawning 0.4.103. Nothing
was lying: each readout was reading a different lane, correctly, and no
command compared them.

Two design rules, both learned the expensive way, both load-bearing here:

**Report skew BETWEEN lanes, not staleness WITHIN one.** Every individual
lane was green on 2026-08-04. The defect existed only as a relationship —
daemon 0.4.105 spawning 0.4.103 — so a report that grades each lane against
an absolute and never compares them would have passed. :func:`verify`
therefore computes the lane set first and grades the *disagreement*;
``--expected`` narrows that to "disagreement with a named version" but is
never required for the check to bite.

**Verify the running process, not the venv.** ``pip install --upgrade``
silently no-ops often enough to be a documented fleet gotcha, so a venv
reporting the right version proves nothing about what executes. The
``spawns`` lanes (:mod:`coord.health.checks.spawned_coord`) are the ones that
read a live process; the rest read installs and are kept because a fleet can
be wrong in both ways at once.

**Read-only, always.** ``coord diagnose`` is a documented trap for having
write side effects; this command must be safe mid-flight. It issues GETs to
each agent's ``/health`` and (optionally) the daemon's ``/board`` and does
nothing else. Fixing drift is explicitly *not* its job — ``coord agent
update`` owns that lane, and an automatic ``systemctl`` write across every
host is a far bigger blast radius than detection.

**Thin-client capable.** Lane facts ride the transport that already exists:
every machine computes its own machine-scope health checks and serves them
at ``/health``, and the daemon publishes its own process-local facts in
``/board``'s ``fleet_health`` block. Nothing here shells out over ssh, so it
works from a laptop that holds no credentials and no checkout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Lane severities, deliberately the same vocabulary as coord.health so a
# reader moving between `coord health` and `coord release verify` never has
# to translate. UNKNOWN outranks OK (an unverified lane is not a verified
# one) and is outranked by WARN (it must never page).
SEVERITY_RANK = {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}

EXIT_OK = 0
EXIT_WARN = 1
EXIT_CRIT = 2

# How each machine-scope check id projects into a lane row. The value is the
# lane's display name template; `{machine}` is substituted. Ordering here is
# the ordering in the report.
_VERSION_LANES: tuple[tuple[str, str], ...] = (
    ("agent_venv", "~/.coord-venv"),
    ("cli_venv", "~/.coord-cli-venv"),
)


@dataclass(frozen=True)
class Lane:
    """One (host, lane) row: what version that lane is actually on.

    ``version=None`` means "no data", which is emphatically not "agrees with
    everyone else" — the whole point of #1834 is that a lane nobody can see
    is the one that bites.
    """

    host: str
    lane: str
    version: str | None = None
    editable: bool | None = None
    # Free-text context for the report (a resolved path, a unit name).
    detail: str = ""
    # True for lanes that read a LIVE process rather than an install.
    process: bool = False

    @property
    def label(self) -> str:
        return f"{self.lane} ({self.host})"


@dataclass(frozen=True)
class Finding:
    """Something wrong, named down to the host and the lane."""

    severity: str
    host: str
    lane: str
    summary: str
    detail: str = ""

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 1)


@dataclass
class VerifyReport:
    expected: str | None = None
    lanes: list[Lane] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    # Hosts that could not be reached at all, with the reason.
    unreachable: dict[str, str] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        worst = "ok"
        for f in self.findings:
            if f.rank > SEVERITY_RANK[worst]:
                worst = f.severity
        return worst

    @property
    def ok(self) -> bool:
        return self.severity == "ok"

    @property
    def exit_code(self) -> int:
        sev = self.severity
        if sev == "crit":
            return EXIT_CRIT
        if sev in ("warn", "unknown"):
            return EXIT_WARN
        return EXIT_OK

    @property
    def versions(self) -> dict[str, list[str]]:
        """version -> the lane labels on it, for every lane with data."""
        out: dict[str, list[str]] = {}
        for lane in self.lanes:
            if lane.version:
                out.setdefault(lane.version, []).append(lane.label)
        for labels in out.values():
            labels.sort()
        return out

    def to_dict(self) -> dict:
        return {
            "schema": 1,
            "expected": self.expected,
            "severity": self.severity,
            "exit_code": self.exit_code,
            "unreachable": dict(self.unreachable),
            "versions": self.versions,
            "lanes": [
                {
                    "host": lane.host,
                    "lane": lane.lane,
                    "version": lane.version,
                    "editable": lane.editable,
                    "detail": lane.detail,
                    "process": lane.process,
                }
                for lane in self.lanes
            ],
            "findings": [
                {
                    "severity": f.severity,
                    "host": f.host,
                    "lane": f.lane,
                    "summary": f.summary,
                    "detail": f.detail,
                }
                for f in self.findings
            ],
        }


# ──────────────────────────────────────────────────────────────────────────
# Projection: /health payload -> lanes + findings
# ──────────────────────────────────────────────────────────────────────────


def _results(health: dict | None) -> list[dict]:
    """The machine-scope check rows inside an agent's ``/health`` body.

    The agent nests its health report under a ``health`` key alongside
    ``version``/``tool_versions``/... (``coord/agent.py``); a payload without
    one is an older agent, which is "no data", not "no findings".
    """
    if not health:
        return []
    block = health.get("health") or {}
    return [r for r in (block.get("results") or []) if isinstance(r, dict)]


def _rows(health: dict | None, check_id: str) -> list[dict]:
    return [r for r in _results(health) if r.get("check_id") == check_id]


def lanes_for_host(host: str, health: dict | None) -> list[Lane]:
    """Every deploy lane *host* can speak for, projected from its ``/health``.

    Pure and side-effect free so the whole projection is unit-testable from a
    dict — the transport is the part that needs a live fleet, the judgement
    is not.
    """
    lanes: list[Lane] = []

    for check_id, name in _VERSION_LANES:
        for row in _rows(health, check_id):
            values = row.get("values") or {}
            # cli_venv reports present=False on the many machines that never
            # had one; that is a genuine absence, not a missing lane.
            if check_id == "cli_venv" and not values.get("present"):
                continue
            if row.get("error"):
                lanes.append(Lane(host=host, lane=name, version=None,
                                  detail=str(row.get("error"))[:200]))
                continue
            lanes.append(
                Lane(
                    host=host,
                    lane=name,
                    version=values.get("version") or None,
                    editable=values.get("editable"),
                )
            )

    for row in _rows(health, "spawned_coord"):
        values = row.get("values") or {}
        unit = row.get("subject") or values.get("unit")
        if not unit:
            # The "no coord service running here" singleton row.
            continue
        if values.get("fallback"):
            # No `coord` on the service PATH: coord_argv() falls back to the
            # parent's own interpreter, which cannot be skewed against the
            # parent. Not a lane, and not a gap.
            continue
        lanes.append(
            Lane(
                host=host,
                lane=f"{unit} spawns",
                version=values.get("version") or None,
                editable=values.get("editable"),
                detail=str(values.get("resolved") or ""),
                process=True,
            )
        )

    return lanes


def findings_for_host(host: str, health: dict | None) -> list[Finding]:
    """Per-lane findings that are true regardless of what any other lane says.

    Version skew is NOT computed here — it is a relationship between lanes
    and belongs to :func:`verify`. What lives here is the set of defects that
    are wrong on their own terms: an editable install on a service PATH, a
    unit file that drifted from ``deploy/``, a stale ``coord-tui`` binary, an
    unreadable lane.
    """
    out: list[Finding] = []

    # ── editable installs ────────────────────────────────────────────────
    # #1834: "any editable install on a service PATH is a finding on its own,
    # independent of its current version — it is a drift amplifier that
    # silently tracks a checkout nothing keeps current."
    for row in _rows(health, "agent_venv"):
        if (row.get("values") or {}).get("editable"):
            out.append(
                Finding(
                    severity="crit",
                    host=host,
                    lane="~/.coord-venv",
                    summary="agent venv is an EDITABLE install",
                    detail=(
                        "this machine runs whatever branch its checkout is "
                        "parked on; no release can account for its behaviour"
                    ),
                )
            )

    for row in _rows(health, "spawned_coord"):
        values = row.get("values") or {}
        unit = row.get("subject") or values.get("unit") or "unit"
        if values.get("editable"):
            out.append(
                Finding(
                    severity="crit",
                    host=host,
                    lane=f"{unit} spawns",
                    summary=f"{unit} would spawn an EDITABLE checkout",
                    detail=(
                        f"{values.get('resolved') or 'coord'} resolves to "
                        f"{values.get('module_file') or 'a checkout'} on this "
                        "unit's live PATH — a drift amplifier regardless of "
                        "the version it happens to report today"
                    ),
                )
            )
        elif row.get("severity") == "unknown" and not values.get("fallback"):
            out.append(
                Finding(
                    severity="unknown",
                    host=host,
                    lane=f"{unit} spawns",
                    summary=f"could not read what {unit} would spawn",
                    detail=str(row.get("headroom") or row.get("error") or ""),
                )
            )

    # ── unit files vs deploy/ (#1831, folded in per #1834's lane 3) ──────
    for row in _rows(health, "unit_drift"):
        sev = row.get("severity")
        if sev in ("crit", "warn"):
            out.append(
                Finding(
                    severity=str(sev),
                    host=host,
                    lane=f"unit {row.get('subject') or '?'}",
                    summary=str(row.get("headroom") or "unit drift"),
                    detail=str(row.get("detail") or ""),
                )
            )

    # ── coord-tui, until PKG-3/PKG-4 give it a real channel ──────────────
    for row in _rows(health, "tui_binary"):
        if row.get("severity") == "warn":
            out.append(
                Finding(
                    severity="warn",
                    host=host,
                    lane="coord-tui",
                    summary=str(row.get("headroom") or "tui binary is stale"),
                    detail=str(row.get("detail") or ""),
                )
            )

    return out


def daemon_lanes(daemon_host: dict | None, *, host: str = "daemon") -> list[Lane]:
    """The lanes only the ``coord-serve`` process itself can report.

    ``coord_serve_version`` is genuinely process-local: it is the daemon
    introspecting its own interpreter, which no other machine can do for it
    (that conflation is what #1806 was about). It arrives via ``/board``'s
    ``fleet_health.daemon_host`` block.
    """
    if not daemon_host:
        return []
    if not {"coord_serve_version", "coord_serve_editable"} & set(daemon_host):
        return []
    version = daemon_host.get("coord_serve_version")
    editable = daemon_host.get("coord_serve_editable")
    # A published-but-null version is a lane with no data, NOT an absent lane:
    # it still emits an UNKNOWN row below rather than vanishing from the table.
    return [
        Lane(
            host=host,
            lane="coord-serve process",
            version=version or None,
            editable=editable,
            process=True,
        )
    ]


# ──────────────────────────────────────────────────────────────────────────
# The verdict
# ──────────────────────────────────────────────────────────────────────────


def verify(
    *,
    machine_health: dict[str, dict | None],
    daemon_host: dict | None = None,
    unreachable: dict[str, str] | None = None,
    expected: str | None = None,
    daemon_host_name: str = "daemon",
) -> VerifyReport:
    """Grade a whole fleet's deploy lanes. Pure: no I/O, no config, no clock.

    *machine_health* maps machine name -> that machine's ``/health`` body (or
    ``None`` when it answered with nothing usable). *unreachable* maps
    machine name -> why, for hosts that did not answer at all.

    An unreachable host is UNKNOWN, never OK: #1834's whole thesis is that a
    lane nobody looked at is the one that drifts, so "we could not ask" must
    not render as "verified".
    """
    report = VerifyReport(expected=expected, unreachable=dict(unreachable or {}))

    for host in sorted(machine_health):
        health = machine_health[host]
        report.lanes.extend(lanes_for_host(host, health))
        report.findings.extend(findings_for_host(host, health))

    report.lanes.extend(daemon_lanes(daemon_host, host=daemon_host_name))

    for host, reason in sorted(report.unreachable.items()):
        report.findings.append(
            Finding(
                severity="unknown",
                host=host,
                lane="(all lanes)",
                summary="host unreachable — its lanes are unverified",
                detail=reason,
            )
        )

    # ── lanes with no data ───────────────────────────────────────────────
    for lane in report.lanes:
        if lane.version is None:
            report.findings.append(
                Finding(
                    severity="unknown",
                    host=lane.host,
                    lane=lane.lane,
                    summary="no version reported for this lane",
                    detail=lane.detail,
                )
            )

    if not report.lanes and not report.unreachable:
        report.findings.append(
            Finding(
                severity="unknown",
                host="(fleet)",
                lane="(all lanes)",
                summary="no host reported a single deploy lane",
                detail=(
                    "every machine answered, none had health results — check "
                    "that the agents are running a release with the health "
                    "engine (#1628) enabled"
                ),
            )
        )

    # ── the relationship, which is the actual check ──────────────────────
    versions = report.versions
    if expected:
        for version, labels in sorted(versions.items()):
            if version == expected:
                continue
            report.findings.append(
                Finding(
                    severity="crit",
                    host=_hosts_of(report, labels),
                    lane=", ".join(labels),
                    summary=f"on {version}, expected {expected}",
                    detail=(
                        "the released version is not what this lane is "
                        "actually running"
                    ),
                )
            )
    elif len(versions) > 1:
        # No --expected: skew alone is the finding. This is the 2026-08-04
        # shape — nobody knew what to expect, but two lanes disagreeing was
        # already conclusive.
        spread = "; ".join(
            f"{v}: {', '.join(labels)}" for v, labels in sorted(versions.items())
        )
        report.findings.append(
            Finding(
                severity="crit",
                host="(fleet)",
                lane="(version skew)",
                summary=f"{len(versions)} versions live across the fleet",
                detail=spread,
            )
        )

    report.findings.sort(key=lambda f: (-f.rank, f.host, f.lane))
    return report


def _hosts_of(report: VerifyReport, labels: Iterable[str]) -> str:
    wanted = set(labels)
    hosts = sorted({lane.host for lane in report.lanes if lane.label in wanted})
    return ", ".join(hosts) if hosts else "(fleet)"


# ──────────────────────────────────────────────────────────────────────────
# Transport (thin-client capable; every call is a GET)
# ──────────────────────────────────────────────────────────────────────────


def gather(
    config: Any,
    *,
    timeout: float = 5.0,
    machine_filter: str | None = None,
    check_machine: Callable[..., Any] | None = None,
    board_payload: Callable[[], dict] | None = None,
) -> tuple[dict[str, dict | None], dict[str, str], dict | None, str]:
    """Poll the fleet. Returns ``(machine_health, unreachable, daemon_host,
    daemon_host_name)``.

    Injectable seams (*check_machine*, *board_payload*) exist so the whole
    command can be driven in tests without a live fleet — and so this module
    never has to care whether the board came from a local DB or from a
    daemon over Tailscale.
    """
    from coord import network  # noqa: PLC0415 — import cycle at module scope

    probe = check_machine or network.check_machine

    machines = list(getattr(config, "machines", ()) or ())
    if machine_filter:
        machines = [m for m in machines if m.name == machine_filter]

    health: dict[str, dict | None] = {}
    unreachable: dict[str, str] = {}
    for machine in machines:
        try:
            status = probe(machine, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — a probe must never abort the sweep
            unreachable[machine.name] = f"{type(exc).__name__}: {exc}"
            continue
        if not getattr(status, "is_online", False):
            unreachable[machine.name] = (
                getattr(status, "reason", None) or getattr(status, "state", "offline")
            )
            continue
        health[machine.name] = getattr(status, "health", None)

    daemon_host, daemon_name = _daemon_facts(board_payload)
    return health, unreachable, daemon_host, daemon_name


#: The lane name `coord.health.checks.fleet_deploy_lanes` publishes for the
#: daemon's own install. Matched by string because that is the only contract
#: `/board` offers; `tests/test_release_verify.py` pins the two together so a
#: rename over there fails here loudly instead of silently dropping the lane.
DAEMON_SERVE_LANE = "coord-serve (daemon host)"


def _daemon_facts(board_payload: Callable[[], dict] | None) -> tuple[dict | None, str]:
    """The ``coord-serve`` process's own version, read out of ``/board``.

    ``coord-serve``'s version can only be introspected from the process
    actually running it (#1806) — no other machine's ``/health`` can speak
    for it. The daemon gathers it into its fleet snapshot and publishes it
    inside ``fleet_health.fleet_checks``' ``fleet_deploy_lanes`` row, which
    is the only place a thin client can read it from. (The richer internal
    ``daemon_host`` fact block, which also carries ``coord_serve_editable``,
    is deliberately not on the wire; editability of the daemon's own install
    therefore reads as unknown from a thin client rather than as ``False``.)

    Absent on a fleet whose daemon predates the health engine, and absent in
    a host-mode run with no daemon configured. Both are "no data" for the
    ``coord-serve process`` lane, which :func:`verify` reports as UNKNOWN
    rather than skipping — a daemon nobody could ask is exactly the lane
    2026-08-04 hid in.
    """
    fetch = board_payload
    if fetch is None:

        def fetch() -> dict:  # type: ignore[misc]
            from coord.client import (  # noqa: PLC0415
                fetch_board_payload,
                resolve_board_service,
            )

            svc = resolve_board_service()
            if svc is None:
                return {}
            return fetch_board_payload(svc)

    try:
        payload = fetch() or {}
    except Exception:  # noqa: BLE001 — read-only, best effort, never fatal
        return None, "daemon"

    fleet_health = payload.get("fleet_health") or {}
    for row in fleet_health.get("fleet_checks") or []:
        if not isinstance(row, dict) or row.get("check_id") != "fleet_deploy_lanes":
            continue
        lanes = (row.get("values") or {}).get("lanes") or {}
        if DAEMON_SERVE_LANE in lanes:
            return {"coord_serve_version": lanes[DAEMON_SERVE_LANE]}, "daemon"
    return None, "daemon"


# ──────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────

_SEVERITY_MARK = {"ok": "OK  ", "unknown": "?   ", "warn": "WARN", "crit": "CRIT"}


def render(report: VerifyReport, *, verbose: bool = False) -> str:
    """A per-lane report. Lanes first, then findings, worst first.

    The lane table is printed even on success, deliberately: the failure mode
    this command exists for is a readout that says "fine" while hiding the
    lane it never looked at, so the set of lanes actually inspected is part
    of the answer, not debug output.
    """
    lines: list[str] = []

    if report.expected:
        lines.append(f"expected version: {report.expected}")

    lines.append("lanes:")
    if not report.lanes:
        lines.append("  (none)")
    for lane in sorted(report.lanes, key=lambda l: (l.host, l.lane)):
        version = lane.version or "?"
        marks = []
        if lane.process:
            marks.append("live process")
        if lane.editable:
            marks.append("EDITABLE")
        if lane.detail and verbose:
            marks.append(lane.detail)
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        lines.append(f"  {lane.host:<14} {lane.lane:<26} {version}{suffix}")

    versions = report.versions
    if len(versions) > 1:
        lines.append("")
        lines.append("SKEW:")
        for version, labels in sorted(versions.items()):
            lines.append(f"  {version}: {', '.join(labels)}")

    if report.findings:
        lines.append("")
        lines.append("findings:")
        for f in report.findings:
            lines.append(
                f"  {_SEVERITY_MARK.get(f.severity, '?   ')} "
                f"{f.host}/{f.lane}: {f.summary}"
            )
            if f.detail:
                lines.append(f"         {f.detail}")

    lines.append("")
    counts = {sev: 0 for sev in SEVERITY_RANK}
    for f in report.findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    lines.append(
        f"RELEASE VERIFY: {report.severity.upper()} "
        f"crit={counts.get('crit', 0)} warn={counts.get('warn', 0)} "
        f"unknown={counts.get('unknown', 0)} "
        f"lanes={len(report.lanes)} hosts={len({l.host for l in report.lanes})}"
    )
    return "\n".join(lines)
