"""Fleet-health check engine (#1628).

Answers one question for a catalogue of fleet-degradation signals: **how much
headroom is left?**  Runs on one machine, against that machine and the
checkouts ``coordinator.yml`` says live on it.  No transport, no board state,
no renderer beyond ``coord health`` — those are H-3 and H-4.

The shape, in the order you'd meet it:

* ``models`` — :class:`~coord.health.models.CheckResult`, which carries raw
  values **and** the rendered headroom string.  Renderers never re-derive
  severity from raw numbers; that rule is what keeps the logic from forking
  once there is more than one surface.
* ``registry`` — the ``@check`` decorator and ``run_all``.  Adding a check
  means adding one module under ``checks/``; discovery is ``pkgutil`` over
  that package, so nothing central needs editing.
* ``checks/`` — the seed probes: disk, cargo targets, worktrees, agent
  install/version, claude binary, repo branch/dirt, graph freshness, plan
  usage.  Two of them (``graph``, ``plan_usage``) are thin wrappers over
  ``coord.graph_health`` and ``coord.usage_limits`` rather than forks.
* ``render`` / ``cli`` — the one surface this child ships.

Every probe is cheap, read-only, and fails soft: a probe that raises becomes
an ``unknown`` result carrying the error text, never a failed run.
"""

from coord.health.models import (
    SCOPES,
    CheckResult,
    Checkout,
    HealthContext,
    Severity,
    unknown_result,
    worst,
)
from coord.health.registry import (
    COST_CHEAP,
    COST_NETWORK,
    Check,
    HealthReport,
    all_checks,
    check,
    discover,
    get,
    register,
    run_all,
    run_check,
)

__all__ = [
    "COST_CHEAP",
    "COST_NETWORK",
    "SCOPES",
    "Check",
    "CheckResult",
    "Checkout",
    "HealthContext",
    "HealthReport",
    "Severity",
    "all_checks",
    "check",
    "discover",
    "get",
    "register",
    "run_all",
    "run_check",
    "unknown_result",
    "worst",
]
