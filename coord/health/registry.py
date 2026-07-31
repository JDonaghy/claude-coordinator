"""The check registry (#1628).

**The acceptance bar for this abstraction is that adding a check touches
exactly one file: the new check module.**  Not the renderer, not the CLI,
not a transport.  That is enforced two ways:

1. Registration is a decorator (:func:`check`) applied in the check's own
   module — there is no central list of checks to append to.
2. Discovery is :func:`pkgutil.iter_modules` over ``coord.health.checks`` —
   dropping ``coord/health/checks/foo.py`` into the package is the whole
   installation step.  ``checks/__init__.py`` deliberately imports nothing.

The other half of the contract is fail-soft.  :func:`run_all` wraps every
probe in a bare ``except Exception`` and converts a raised probe into an
``unknown`` result carrying the error text.  A health engine that dies on
its weakest check reports nothing, which is worse than reporting the rest
plus one ``?``.
"""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from coord.health.models import (
    SCOPES,
    CheckResult,
    HealthContext,
    Severity,
    unknown_result,
)

# What a probe costs to run.  ``cheap`` probes are local syscalls/subprocesses
# and the whole cheap set is budgeted under ~2s, because this eventually runs
# on a timer on every agent.  ``network`` probes (the one PyPI simple-index
# fetch, the ``claude -p /usage`` round trip) are skipped when the caller
# passes ``allow_network=False``.
COST_CHEAP = "cheap"
COST_NETWORK = "network"

ProbeFn = Callable[[HealthContext], CheckResult | Sequence[CheckResult] | None]


@dataclass(frozen=True)
class Check:
    """A self-contained unit of fleet-degradation signal."""

    id: str
    scope: str
    probe: ProbeFn
    title: str = ""
    description: str = ""
    cost: str = COST_CHEAP
    # Display/run ordering.  Lives on the check so a new module can slot
    # itself into the report without anyone editing a renderer's list.
    order: int = 100

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"check {self.id!r}: scope must be one of {SCOPES}, got {self.scope!r}")
        if self.cost not in (COST_CHEAP, COST_NETWORK):
            raise ValueError(f"check {self.id!r}: cost must be 'cheap' or 'network'")
        if not self.title:
            object.__setattr__(self, "title", self.id)


_REGISTRY: dict[str, Check] = {}
_discovered = False


def register(chk: Check) -> Check:
    """Add *chk* to the registry.  Re-registering the same id replaces it
    (module reload under pytest must not raise)."""
    _REGISTRY[chk.id] = chk
    return chk


def check(
    *,
    id: str,  # noqa: A002 — matches the field name in the issue's spec
    scope: str,
    title: str = "",
    description: str = "",
    cost: str = COST_CHEAP,
    order: int = 100,
) -> Callable[[ProbeFn], ProbeFn]:
    """Decorator form: register the decorated function as a check's probe.

    The probe returns one :class:`CheckResult`, a sequence of them (one per
    disk / per checkout / ...), or ``None`` for "nothing to report here".
    """

    def _wrap(fn: ProbeFn) -> ProbeFn:
        register(
            Check(
                id=id,
                scope=scope,
                probe=fn,
                title=title,
                description=description,
                cost=cost,
                order=order,
            )
        )
        return fn

    return _wrap


def discover(force: bool = False) -> None:
    """Import every module under ``coord.health.checks`` so its decorators run.

    This is the *entire* registration mechanism.  There is no list to edit.
    """
    global _discovered
    if _discovered and not force:
        return
    from coord.health import checks as _checks_pkg

    for mod in pkgutil.iter_modules(_checks_pkg.__path__):
        if mod.name.startswith("_"):
            continue
        importlib.import_module(f"{_checks_pkg.__name__}.{mod.name}")
    _discovered = True


def all_checks() -> list[Check]:
    """Every registered check, in stable report order."""
    discover()
    return sorted(_REGISTRY.values(), key=lambda c: (c.order, c.id))


def get(check_id: str) -> Check | None:
    discover()
    return _REGISTRY.get(check_id)


def run_check(chk: Check, ctx: HealthContext) -> list[CheckResult]:
    """Run one check, fail-soft.

    A probe that raises — any exception, including ``KeyboardInterrupt``'s
    non-``Exception`` siblings excluded — yields a single ``unknown`` result
    naming the error, and the run continues.
    """
    try:
        out = chk.probe(ctx)
    except Exception as exc:  # noqa: BLE001 — fail soft is the requirement
        return [
            unknown_result(
                chk.id,
                scope=chk.scope,
                title=chk.title,
                error=f"{type(exc).__name__}: {exc}",
            )
        ]
    if out is None:
        return []
    results = [out] if isinstance(out, CheckResult) else list(out)
    # A probe that forgets its own title/scope shouldn't produce rows the
    # renderer can't label.  Backfill from the check definition.
    return [
        dataclasses.replace(r, title=chk.title) if r.title == r.check_id else r
        for r in results
    ]


@dataclass
class HealthReport:
    """The full outcome of one registry run."""

    results: list[CheckResult] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    duration_secs: float = 0.0

    @property
    def severity(self) -> Severity:
        from coord.health.models import worst

        return worst([r.severity for r in self.results])

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in Severity}
        for r in self.results:
            out[r.severity.value] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        """The ``coord health --json`` contract (H-3/H-4 consume this)."""
        return {
            "schema": 1,
            "severity": self.severity.value,
            "counts": self.counts(),
            "skipped": list(self.skipped),
            "duration_secs": round(self.duration_secs, 3),
            "results": [r.to_dict() for r in self.results],
        }


def run_all(
    ctx: HealthContext,
    *,
    scopes: Iterable[str] | None = None,
    only: Iterable[str] | None = None,
) -> HealthReport:
    """Run the registry against *ctx*.

    * ``scopes`` — restrict to these scopes (``coord health --local`` passes
      ``("machine", "checkout")``; ``fleet`` probes arrive in H-3).
    * ``only`` — restrict to these check ids.
    * ``ctx.allow_network`` False skips ``cost="network"`` checks, recording
      their ids in :attr:`HealthReport.skipped` so "we didn't look" never
      reads as "nothing wrong".
    * ``thresholds.disabled_checks`` skips by operator config, same way.
    """
    import time  # noqa: PLC0415 — local so a frozen-clock test can patch it

    started = time.monotonic()
    scope_filter = set(scopes) if scopes is not None else None
    only_filter = set(only) if only is not None else None
    disabled = set(getattr(ctx.thresholds, "disabled_checks", ()) or ())

    report = HealthReport()
    for chk in all_checks():
        if scope_filter is not None and chk.scope not in scope_filter:
            continue
        if only_filter is not None and chk.id not in only_filter:
            continue
        if chk.id in disabled:
            report.skipped.append(f"{chk.id} (disabled in coordinator.yml)")
            continue
        if chk.cost == COST_NETWORK and not ctx.allow_network:
            report.skipped.append(f"{chk.id} (network probe, --no-network)")
            continue
        report.results.extend(run_check(chk, ctx))
    report.duration_secs = time.monotonic() - started
    return report
