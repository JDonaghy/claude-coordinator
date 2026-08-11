"""Systemd unit *enablement* vs. the per-role manifest (#2098).

`unit_drift` (:mod:`coord.health.checks.unit_drift`) answers "does the
installed unit's *content* match the release" and deliberately treats
absence as OK — most hosts don't run every deploy-lane unit, and which
units a host runs is a topology decision neither that check nor
`coord.deploy_units` will infer (#1831). Neither of those probes answers
the question that actually cost a day: **an installed unit whose content
is byte-perfect can still be sitting there disabled.**

`coord-release-propagate.timer` was `cp`'d onto dellserver correctly and
then never `systemctl --user enable --now`'d, because the runbook that
would have said so never mentioned it (fixed in
`docs/AGENT_OPERATIONS.md`, but the doc fix alone leaves the *next* missed
enable step just as invisible). A disabled timer and a deferring timer
both produce zero log lines — the fleet ran 11 releases behind for a day
with every readout looking normal, because nothing distinguished "not
enabled" from "enabled and just hasn't fired yet".

`coord.deploy_manifest` is this check's reference: a role -> unit-name
table that used to exist only as prose (or, before that, only as
`~/.config/systemd/user/timers.target.wants/` symlinks on dellserver, which
is machine state that dies with the machine). This probe reads
:func:`~coord.deploy_manifest.all_manifest_units` but only asks about units
this host has *already chosen to install* — same "don't guess topology"
boundary as `unit_drift`: an uninstalled manifest unit is not a fault,
because most hosts are workers and are not supposed to run the daemon
lanes. An *installed* one that the manifest expects and that
`systemctl --user is-enabled` reports as anything other than enabled is
exactly the state that hid the propagate timer.
"""

from __future__ import annotations

import subprocess

from coord.deploy_manifest import all_manifest_units
from coord.health.checks.unit_drift import resolve_systemd_user_dir
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check

# `systemctl --user is-enabled` states that mean "this will run". `static`
# is deliberately excluded: every manifest unit ships an `[Install]`
# section (it is either a timer or a persistent service), so an installed
# manifest unit reporting `static` has lost that section relative to
# `deploy/` — a real fault, not a unit that can't be enabled by design.
#
# `alias` means systemctl resolved the queried name to a *different* unit
# via an `Alias=` in that other unit's `[Install]` section and reports the
# alias target's own state — i.e. "enabled, under another name" — not a
# distinct half-enabled state. None of the units this check reads out of
# `deploy/` declare an `Alias=`, so this should not currently be reachable
# in practice; it is kept in the accepted set (rather than omitted or
# treated as WARN) because, per `systemd.unit`(5), it never means "this
# will NOT run" — the one thing this check exists to catch — the opposite
# would risk a false WARN on a legitimately-running unit.
#
# systemctl/`coord --version` are both fast, but a wedged one must not eat
# the ~2s registry budget for the whole tick (mirrors
# `coord.health.checks.spawned_coord`'s `_SYSTEMCTL_TIMEOUT`).
_SYSTEMCTL_TIMEOUT = 5.0

_ENABLED_STATES = {"enabled", "enabled-runtime", "alias"}


def _is_enabled(unit: str, *, runner=None) -> tuple[str | None, str | None]:
    """`(state, error)` from `systemctl --user is-enabled <unit>`.

    systemctl exits non-zero for every state that isn't `enabled` —
    `disabled`, `static`, `masked`, `not-found`... — so returncode is
    never the signal here, only stdout is. Split out with an injectable
    *runner* (mirrors `coord.deploy_units.daemon_reload`) so the probe is
    testable without a real systemd.
    """
    run = runner or subprocess.run
    try:
        proc = run(
            ["systemctl", "--user", "is-enabled", unit],
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT,
        )
    except FileNotFoundError:
        return None, "systemctl not found (no systemd on this host)"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    state = (proc.stdout or "").strip() or (proc.stderr or "").strip() or "unknown"
    return state, None


@check(
    id="unit_enablement",
    scope="machine",
    title="unit enablement",
    order=48,
    description=(
        "Installed deploy-lane units the per-role manifest "
        "(coord/deploy_manifest.py) says should run are actually "
        "`systemctl --user enable`d, not just present (#2098)."
    ),
)
def probe_unit_enablement(ctx: HealthContext) -> list[CheckResult]:
    installed_dir = resolve_systemd_user_dir(ctx)
    results: list[CheckResult] = []

    for name in all_manifest_units():
        installed_path = installed_dir / name
        if not installed_path.exists():
            # Not this host's topology. `unit_drift` reports the same
            # absence as OK for the same reason (#1831/#1927) — this probe
            # only judges units a host has already chosen to install.
            continue

        state, error = _is_enabled(name)
        values: dict = {"installed_path": str(installed_path), "state": state}

        if error:
            results.append(
                CheckResult(
                    check_id="unit_enablement",
                    scope="machine",
                    subject=name,
                    severity=Severity.UNKNOWN,
                    headroom=f"could not check enablement: {error}",
                    error=error,
                    values=values,
                )
            )
            continue

        if state in _ENABLED_STATES:
            results.append(
                CheckResult(
                    check_id="unit_enablement",
                    scope="machine",
                    subject=name,
                    severity=Severity.OK,
                    headroom=state,
                    values=values,
                )
            )
            continue

        results.append(
            CheckResult(
                check_id="unit_enablement",
                scope="machine",
                subject=name,
                severity=Severity.WARN,
                headroom=(
                    f"installed but {state} — a disabled unit and a working "
                    "one produce identical evidence until something needed "
                    "it (#2098)"
                ),
                detail=f"systemctl --user daemon-reload && systemctl --user enable --now {name}",
                threshold="warn when a manifest-listed, installed unit is not enabled",
                values=values,
            )
        )

    if not results:
        return [
            CheckResult(
                check_id="unit_enablement",
                scope="machine",
                severity=Severity.OK,
                headroom="no manifest-listed unit installed on this host",
                values={"installed_dir": str(installed_dir)},
            )
        ]
    return results
