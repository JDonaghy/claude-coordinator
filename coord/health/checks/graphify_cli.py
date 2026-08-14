"""Is the ``graphify`` CLI installed where the agent runs? (#2237 item 6)

Layer 1 of graphify's four (pipx CLI -> built graph -> ``.git/hooks`` ->
versioned ``.githooks/`` shims, see ``docs/GRAPHIFY_SETUP.md``), and the only
one whose absence makes every other layer unfixable on that machine.

Until this check existed, a machine with no ``graphify`` on ``$PATH`` failed
*silently and repeatedly*: the agent's self-heal (#1729) would try
``graphify update .`` on a stale checkout, get ``command not found``, record
that reason against the current HEAD so it would not retry (guard 3 —
correct as a retry policy), and then say nothing anyone reads. Every graph
operation on that machine failed one-by-one for a reason only visible inside
a per-checkout failure record. Asking the question once, at machine scope,
turns N silent per-HEAD failures into one finding with a fix line.

**WARN, not CRIT.** A machine with no graphify still dispatches, still
builds, still merges — its workers just fall back to grep (the worker prompt
has an explicit escape hatch for exactly that, see #2212). That is degraded,
not useless, which is the line ``claude_binary`` draws at CRIT.
"""

from __future__ import annotations

import subprocess

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import shorten_path


def _version(path: str) -> str | None:
    """``graphify --version`` output, or ``None`` if it cannot be asked.

    Best-effort and never fatal: a binary that resolves but will not answer
    is still reported as installed (the check's question is "is it here"),
    just without a version string.
    """
    try:
        r = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10.0
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or r.stderr or "").strip() or None


@check(
    id="graphify_cli",
    scope="machine",
    title="graphify cli",
    order=51,
    description="The graphify CLI is installed, so graphs here can be built and healed.",
)
def probe_graphify_cli(ctx: HealthContext) -> CheckResult:
    from coord.graph_health import graphify_cli_path  # noqa: PLC0415

    path = graphify_cli_path()

    if path is None:
        return CheckResult(
            check_id="graphify_cli",
            scope="machine",
            severity=Severity.WARN,
            headroom="not installed on this machine",
            threshold="warn when absent",
            detail=(
                "no graph on this machine can be built or self-healed; every "
                "worker here silently degrades to grep. "
                "fix: pipx install graphify  (see docs/GRAPHIFY_SETUP.md)"
            ),
            values={"path": None, "installed": False, "version": None},
        )

    version = _version(path)
    return CheckResult(
        check_id="graphify_cli",
        scope="machine",
        severity=Severity.OK,
        headroom=(
            f"{shorten_path(path, str(ctx.home))}"
            + (f" ({version})" if version else "")
        ),
        threshold="warn when absent",
        values={"path": path, "installed": True, "version": version},
    )
