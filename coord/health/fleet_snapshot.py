"""Tick-refreshed fleet-health snapshot, aggregated onto board state (#1630).

Mirrors :mod:`coord.gate_snapshot`'s shape exactly, and for the same reason:
the ``/board`` read path must perform no per-request I/O (#1336 invariant 1).
Polling N agents' ``/health`` endpoints, shelling out to ``pip show`` twice
for the daemon-host deploy lanes, and cross-referencing every machine's
``/status`` for phantom rows is all real network/subprocess work — it runs
on the daemon's slow tick cadence (``coord.serve_app``'s ``_health_poll_
tick``), never inline inside a ``GET /board``.

**Advisory only (the hard constraint of #1630).** This module writes to
:func:`coord.state.save_machine_health` and hands its snapshot to
``/board``'s response body — never to a :class:`coord.models.Board`. Nothing
here is reachable from ``coord.merge_queue.plan``, ``coord.dispatch``, or
``coord.review``'s routing, because none of those take this module's output
as an argument. See ``tests/test_health_advisory_only.py``.

**Unknown, not green, when a signal is missing.** A machine that never
responds, or one whose last response is older than :data:`STALE_AFTER_
SECONDS`, is surfaced as ``severity="unknown"`` — never silently dropped
(that would look identical to "nothing wrong", #1485's exact failure mode)
and never left at its last-known-good severity forever (that would look
identical to "still healthy", the regression #1630 itself calls out).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

log = logging.getLogger("coord.serve")

# How old a machine's last-received health poll can get before it reads as
# `unknown` rather than its last-known severity. A few multiples of the
# health-poll tick's own cadence (COORD_HEALTH_POLL_INTERVAL, default 60s) —
# generous enough that one slow/missed tick doesn't flap a healthy machine
# to unknown, tight enough that a genuinely dead agent doesn't stay green
# for hours. Overridable for tests.
STALE_AFTER_SECONDS = float(os.environ.get("COORD_HEALTH_STALE_SECS", "900"))

# #1337/#1336/#1597: this repo has hit multi-MB /board payloads from
# unbounded per-item detail three times. The health block budgets itself
# independently and much tighter — 256 KiB is enormous headroom for what's
# at most a few dozen machines x a dozen short check rows, while still
# catching a pathological probe (e.g. one that dumps a huge `values` blob)
# before it repeats that history.
MAX_HEALTH_BLOCK_BYTES = 256 * 1024


def _trim_check_result(r: dict) -> dict:
    """Drop the bytes-heavy, lowest-value fields from an OK-severity result.

    Applied only when the whole block is over budget (see
    :func:`bound_health_payload`) — a healthy check's `values`/`detail` are
    the least actionable bytes in the payload; a WARN/CRIT/UNKNOWN result is
    left untouched because that's exactly the row someone is about to read.
    """
    if r.get("severity") != "ok":
        return r
    trimmed = dict(r)
    trimmed["values"] = {}
    trimmed["detail"] = ""
    return trimmed


def _hard_truncate_result(r: dict, *, max_field_len: int = 200) -> dict:
    """Last-resort trim applied regardless of severity: cap `detail` and
    `values` to a fixed size. Unlike :func:`_trim_check_result` this DOES
    touch WARN/CRIT/UNKNOWN rows — by the time this runs, staying under
    budget has won out over "never touch the row someone is about to read";
    a truncated-but-present WARN/CRIT beats a payload dropped/rejected
    outright for being oversized.
    """
    trimmed = dict(r)
    detail = trimmed.get("detail") or ""
    if len(detail) > max_field_len:
        trimmed["detail"] = detail[:max_field_len] + "…"
    values = trimmed.get("values") or {}
    if values and len(json.dumps(values)) > max_field_len:
        trimmed["values"] = {"_truncated": True}
    return trimmed


def bound_health_payload(
    machine_health: list[dict],
    fleet_checks: list[dict],
    *,
    max_bytes: int = MAX_HEALTH_BLOCK_BYTES,
) -> tuple[list[dict], list[dict], bool]:
    """Trim *machine_health*/*fleet_checks* to fit under *max_bytes* serialized.

    Escalating, cheapest-first stages, each re-checked before reaching for
    the next:

    1. strip `values`/`detail` off OK-severity result rows fleet-wide (the
       common case: everything's fine, most bytes are the least useful).
    2. cap each machine's `results` list to its first 25 entries (H-1's
       registry runs well under this per machine — a backstop, not the
       expected path).
    3. hard-truncate `detail`/`values` on EVERY remaining row, including
       WARN/CRIT/UNKNOWN — this is the guarantee-of-last-resort: no single
       pathological probe (a check that dumps a huge blob into `values`) can
       blow the budget, full stop. #1337/#1336/#1597 are all "an unbounded
       per-item field made the payload huge"; this stage exists so the same
       shape of bug in a *health* probe is caught here instead of repeating
       that history a fourth time.

    Returns ``(machine_health, fleet_checks, truncated)`` — *truncated* is
    True iff stage 2 or 3 actually altered data (as opposed to stage 1's
    already-lossless-for-a-healthy-row trim), so a caller can log/flag it
    rather than truncate silently (no-silent-caps).
    """

    def _size(mh: list[dict], fc: list[dict]) -> int:
        return len(json.dumps({"machine_health": mh, "fleet_checks": fc}))

    if _size(machine_health, fleet_checks) <= max_bytes:
        return machine_health, fleet_checks, False

    trimmed_mh = [
        {**m, "results": [_trim_check_result(r) for r in (m.get("results") or [])]}
        for m in machine_health
    ]
    trimmed_fc = [_trim_check_result(r) for r in fleet_checks]
    if _size(trimmed_mh, trimmed_fc) <= max_bytes:
        return trimmed_mh, trimmed_fc, False

    capped_mh = [
        {**m, "results": (m.get("results") or [])[:25]} for m in trimmed_mh
    ]
    if _size(capped_mh, trimmed_fc) <= max_bytes:
        return capped_mh, trimmed_fc, True

    hard_mh = [
        {**m, "results": [_hard_truncate_result(r) for r in (m.get("results") or [])]}
        for m in capped_mh
    ]
    hard_fc = [_hard_truncate_result(r) for r in trimmed_fc]
    if _size(hard_mh, hard_fc) <= max_bytes:
        return hard_mh, hard_fc, True

    # Stage 4, the actual mathematical guarantee: every row is now bounded
    # to a fixed small size, so the ONLY remaining unbounded dimension is
    # machine count. Drop machines from the tail until the fleet fits — a
    # fleet large enough to need this is not a realistic coordinator install
    # (dozens, not thousands, of machines), so this is a backstop for "the
    # bound must hold, full stop," not an expected path.
    lo, hi = 0, len(hard_mh)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _size(hard_mh[:mid], hard_fc) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return hard_mh[:lo], hard_fc, True


@dataclass(frozen=True)
class FleetHealthSnapshot:
    """Immutable, atomically-swapped view of the last fleet-health refresh.

    ``machine_health`` is a list (not a dict) so it serializes directly into
    ``/board``'s JSON body in a stable, order-preserving shape — one entry
    per machine in ``config.machines`` order.
    """

    machine_health: list[dict] = field(default_factory=list)
    fleet_checks: list[dict] = field(default_factory=list)
    refreshed_at: float | None = None
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "schema": 1,
            "refreshed_at": self.refreshed_at,
            "machine_health": self.machine_health,
            "fleet_checks": self.fleet_checks,
            "truncated": self.truncated,
        }


def _effective_severity(entry: dict, *, now: float) -> tuple[str, bool]:
    """(severity, stale) for one machine's aggregated health row.

    `unknown` — never a carried-forward "ok" — whenever the daemon can't
    currently vouch for the number: offline/unreachable, no health block at
    all (old agent, or an agent that hasn't completed its first local run
    yet), or a received_at older than STALE_AFTER_SECONDS.
    """
    received_at = entry.get("received_at")
    stale = received_at is None or (now - received_at) > STALE_AFTER_SECONDS
    if stale or entry.get("state") != "online":
        return "unknown", stale
    checks = entry.get("health") or {}
    severity = checks.get("severity")
    return (severity or "unknown"), stale


def _machine_health_rows(machine_names: list[str], raw: dict, *, now: float) -> list[dict]:
    rows: list[dict] = []
    for name in machine_names:
        entry = raw.get(name) or {"state": "unknown", "reason": "never polled",
                                   "latency_ms": None, "received_at": None, "health": None}
        severity, stale = _effective_severity(entry, now=now)
        checks = entry.get("health") or {}
        # #1630: `results` is the last-known detail even when `severity` above
        # has been downgraded to "unknown" for staleness — a renderer needs
        # both "trust this right now? no" (severity/stale) AND "what did we
        # last see?" (results/checked_at) to tell "OK" apart from "last
        # measured OK, a while ago" per the issue's own framing. Only truly
        # absent when there is no last-known data at all (never reported, or
        # unreachable with no prior successful poll).
        rows.append(
            {
                "machine": name,
                "state": entry.get("state", "unknown"),
                "reason": entry.get("reason", ""),
                "latency_ms": entry.get("latency_ms"),
                "received_at": entry.get("received_at"),
                "stale": stale,
                "severity": severity,
                "checked_at": checks.get("checked_at"),
                "results": checks.get("results", []),
            }
        )
    return rows


class FleetHealthRefresher:
    """Owns the current :class:`FleetHealthSnapshot`; refreshed by the daemon tick.

    ``snapshot()`` is what the ``/board`` read path consumes — a bare
    attribute read, no I/O. ``refresh(config)`` is the only method that
    talks to agents/subprocesses and must only ever run from the daemon's
    tick machinery (or a test driving it explicitly).
    """

    def __init__(self) -> None:
        self._snapshot = FleetHealthSnapshot()
        # #1597/#1336/#1337: the /board handler is the only thing that
        # actually measures its own rebuild latency and serialized payload
        # size — it calls `record_board_stats` right after each publish.
        # `None` until the first /board build after daemon startup, which
        # the fleet_board_latency check already reports as UNKNOWN rather
        # than fabricating a 0.
        self._board_latency_ms: float | None = None
        self._board_payload_bytes: int | None = None

    def snapshot(self) -> FleetHealthSnapshot:
        return self._snapshot

    def record_board_stats(self, latency_ms: float, payload_bytes: int) -> None:
        """Called by the /board handler after each publish (#1630/#1597).

        Deliberately NOT I/O and NOT gated on the tick cadence — recording a
        float+int is free, and the alternative (recomputing board latency
        independently from this refresher) would mean building a whole
        second board just to measure it, defeating the point of the check.
        """
        self._board_latency_ms = latency_ms
        self._board_payload_bytes = payload_bytes

    def refresh(self, config) -> FleetHealthSnapshot:  # noqa: ANN001 — coord.config.Config
        from coord import network, state  # noqa: PLC0415
        from coord.health.context import build_context  # noqa: PLC0415
        from coord.health.models import FleetSnapshot  # noqa: PLC0415
        from coord.health.registry import run_all  # noqa: PLC0415

        now = time.time()
        machines = list(getattr(config, "machines", ()) or ())

        # Last-known health blocks, read BEFORE this poll — a machine that's
        # merely offline for one tick keeps its last-known check results (so
        # a renderer can still show "disk 90% full, last seen 4 min ago")
        # while `state`/`severity` flip to unreachable/unknown immediately.
        # Only a poll that ACTUALLY returns a fresh health block replaces it;
        # `received_at` always advances to `now` either way, since that's the
        # daemon's own "did I just try to poll this machine" clock, which is
        # what STALE_AFTER_SECONDS below guards (a dead tick loop, not a
        # merely-offline machine).
        previously_known = state.load_machine_health()

        # ── 1. poll every agent's /health, persist each as-of-now ──────────
        for machine in machines:
            try:
                status = network.check_machine(machine)
            except Exception as exc:  # noqa: BLE001 — one bad machine must not abort the tick
                log.warning("health poll: %s raised %s", machine.name, exc)
                status = None
            last_known_health = (previously_known.get(machine.name) or {}).get("health")
            if status is None:
                state.save_machine_health(
                    machine.name, state="unknown", reason="poll raised",
                    latency_ms=None, health=last_known_health, received_at=now,
                )
                continue
            health_block = (
                status.health.get("health") if status.health else None
            ) or last_known_health
            state.save_machine_health(
                machine.name,
                state=status.state,
                reason=status.reason,
                latency_ms=status.latency_ms,
                health=health_block,
                received_at=now,
            )

        raw = state.load_machine_health()
        machine_names = [m.name for m in machines]
        machine_health = _machine_health_rows(machine_names, raw, now=now)

        # ── 2. daemon-host-local facts the fleet checks need ───────────────
        daemon_host = self._daemon_host_facts(config)
        daemon_host["phantom_running"] = self._phantom_running_rows(machines)
        daemon_host["board_latency_ms"] = self._board_latency_ms
        daemon_host["board_payload_bytes"] = self._board_payload_bytes

        # ── 3. run the fleet-scope registry over the assembled snapshot ────
        by_name = {row["machine"]: {
            "state": row["state"], "reason": row["reason"],
            "latency_ms": row["latency_ms"], "received_at": row["received_at"],
            "checks": {"results": row["results"], "checked_at": row["checked_at"]},
        } for row in machine_health}
        fleet = FleetSnapshot(machines=by_name, daemon_host=daemon_host)
        ctx = build_context(config, now=now, allow_network=False)
        ctx.fleet = fleet
        try:
            report = run_all(ctx, scopes=("fleet",))
            fleet_checks = report.to_dict()["results"]
        except Exception as exc:  # noqa: BLE001 — fail soft, never break the tick
            log.warning("fleet health checks failed", exc_info=True)
            fleet_checks = [{
                "key": "fleet_health_error", "check_id": "fleet_health_error",
                "scope": "fleet", "subject": None, "title": "fleet health",
                "label": "fleet health", "severity": "unknown",
                "headroom": f"fleet check run failed: {exc}", "threshold": "",
                "detail": "", "trend": None, "values": {}, "error": str(exc),
            }]

        machine_health, fleet_checks, truncated = bound_health_payload(
            machine_health, fleet_checks
        )
        if truncated:
            log.warning(
                "fleet health snapshot exceeded %d bytes even after trimming "
                "OK-severity detail — per-machine results capped to 25 rows",
                MAX_HEALTH_BLOCK_BYTES,
            )

        self._snapshot = FleetHealthSnapshot(
            machine_health=machine_health,
            fleet_checks=fleet_checks,
            refreshed_at=now,
            truncated=truncated,
        )
        return self._snapshot

    @staticmethod
    def _daemon_host_facts(config) -> dict:  # noqa: ANN001
        """Best-effort facts about the deploy lanes only the daemon host can see.

        Every lookup is individually fail-soft (a missing venv, an
        unconfigured tui_binary_path) — this must never raise, since a
        raise here would take the whole tick down with it.
        """
        import sys  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from coord.health.checks.agent_install import pip_show  # noqa: PLC0415

        facts: dict = {}

        try:
            own = pip_show(Path(sys.executable))
            facts["coord_serve_version"] = own.get("Version") or None
            facts["coord_serve_editable"] = bool(own.get("Editable project location"))
        except Exception:  # noqa: BLE001
            facts["coord_serve_version"] = None
            facts["coord_serve_editable"] = None

        thresholds = getattr(config, "health", None)
        cli_python_cfg = getattr(thresholds, "cli_venv_python", None)
        cli_python = (
            Path(cli_python_cfg).expanduser()
            if cli_python_cfg
            else Path.home() / ".coord-cli-venv" / "bin" / "python3"
        )
        try:
            if cli_python.exists():
                cli = pip_show(cli_python)
                facts["cli_venv_version"] = cli.get("Version") or None
                facts["cli_venv_editable"] = bool(cli.get("Editable project location"))
            else:
                facts["cli_venv_version"] = None
                facts["cli_venv_editable"] = None
        except Exception:  # noqa: BLE001
            facts["cli_venv_version"] = None
            facts["cli_venv_editable"] = None

        tui_path_cfg = getattr(thresholds, "tui_binary_path", None)
        # No path-arithmetic guessing at "tui/src relative to the binary" —
        # a `target/release/...` layout isn't guaranteed, and a wrong guess
        # would silently compare against the wrong tree. Operators configure
        # both explicitly (or leave tui_source_dir unset, which the check
        # already treats as "can't compare, but the binary exists" — OK, not
        # a fabricated pass/fail).
        tui_src_cfg = getattr(thresholds, "tui_source_dir", None)
        facts["tui_binary_path"] = tui_path_cfg
        facts["tui_binary_mtime"] = None
        facts["tui_source_mtime"] = None
        if tui_path_cfg:
            try:
                binary = Path(tui_path_cfg).expanduser()
                if binary.exists():
                    facts["tui_binary_mtime"] = binary.stat().st_mtime
            except OSError:
                pass
        if tui_src_cfg:
            try:
                src = Path(tui_src_cfg).expanduser()
                if src.is_dir():
                    newest = 0.0
                    for p in src.rglob("*.rs"):
                        try:
                            newest = max(newest, p.stat().st_mtime)
                        except OSError:
                            continue
                    if newest:
                        facts["tui_source_mtime"] = newest
            except OSError:
                pass

        return facts

    @staticmethod
    def _phantom_running_rows(machines: list) -> list[dict]:
        """Board rows marked running whose owning machine no longer agrees.

        Read-only: fetches each busy machine's own `/status` and compares —
        never writes to the board, never finalizes anything (that stays
        `coord diagnose`'s job). A machine that doesn't answer `/status` is
        skipped entirely for this check (its offline-ness is already
        reported by the per-machine health row; "phantom" specifically means
        "reachable AND disagrees", not "unreachable").
        """
        from coord import network  # noqa: PLC0415
        from coord.state import build_board  # noqa: PLC0415

        try:
            board = build_board()
        except Exception:  # noqa: BLE001
            return []

        running_by_machine: dict[str, list] = {}
        for a in board.active:
            if a.status == "running" and a.assignment_id:
                running_by_machine.setdefault(a.machine_name, []).append(a)

        phantom: list[dict] = []
        for machine in machines:
            rows = running_by_machine.get(machine.name)
            if not rows:
                continue
            try:
                result = network.fetch_status(machine)
            except Exception:  # noqa: BLE001
                continue
            if not result.ok or not isinstance(result.data, dict):
                continue
            live_ids = {
                e.get("id") for e in (result.data.get("active") or []) if isinstance(e, dict)
            }
            for a in rows:
                if a.assignment_id not in live_ids:
                    phantom.append(
                        {
                            "assignment_id": a.assignment_id,
                            "machine": machine.name,
                            "repo_name": a.repo_name,
                            "issue_number": a.issue_number,
                        }
                    )
        return phantom
