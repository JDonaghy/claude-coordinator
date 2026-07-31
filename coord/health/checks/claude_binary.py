"""Is the ``claude`` binary actually resolvable where the agent runs? (#1628)

This is the #859 failure in health-check form.  ``coord-serve`` runs under
``systemd --user`` with an empty ``Environment=``, so a bare ``claude`` on
``$PATH`` resolves fine in an interactive shell and not at all in the daemon
— which is why :func:`coord.test_orchestrator.resolve_claude_bin` exists.
That resolution is reused verbatim here rather than re-derived: if the
resolver's rules ever change, this check must change with them, and the only
way to guarantee that is to call it.

A machine whose ``claude`` is missing is not degraded, it is *useless* — it
will accept dispatches and fail every one — so this is CRIT with no warn
level, exactly as the issue's table specifies.
"""

from __future__ import annotations

import os

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import shorten_path


@check(
    id="claude_binary",
    scope="machine",
    title="claude binary",
    order=50,
    description="The claude binary resolves and is executable for the agent.",
)
def probe_claude_binary(ctx: HealthContext) -> CheckResult:
    from coord.test_orchestrator import resolve_claude_bin  # noqa: PLC0415

    path = resolve_claude_bin()
    shown = shorten_path(path, str(ctx.home))

    exists = os.path.exists(path)
    executable = exists and os.access(path, os.X_OK)

    if executable:
        severity, headroom = Severity.OK, shown
    elif exists:
        severity, headroom = Severity.CRIT, f"{shown} is not executable"
    else:
        severity, headroom = Severity.CRIT, f"{shown} does not exist"

    return CheckResult(
        check_id="claude_binary",
        scope="machine",
        severity=severity,
        headroom=headroom,
        threshold="crit when unresolvable",
        detail=(
            ""
            if executable
            else "every dispatch to this machine will fail; set $CLAUDE_BIN or reinstall"
        ),
        values={
            "path": path,
            "exists": exists,
            "executable": executable,
            "claude_bin_env": os.environ.get("CLAUDE_BIN"),
        },
    )
