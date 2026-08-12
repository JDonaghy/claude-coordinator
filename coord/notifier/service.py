"""One notifier tick: collect → predicate → dedupe → quiet hours → send (#1632).

The whole subsystem is **advisory and isolated**.  :func:`tick` catches
everything, including a broken predicate or an unwritable state file, and
reports it in its result instead of raising.  An unreachable ntfy server
must not affect dispatch, routing, the board, or any verdict — that is the
#1485 lesson (``/health`` data read as authoritative silently degraded
review routing) restated as a hard rule, and it is asserted by
``tests/test_notifier_isolation.py``.

There is no clock in here.  #1616 gave the pipeline one — `coord serve`'s
30 s ``_tick_loop`` — and this hangs off it (see
``coord.serve_app._notifier_tick``).  Shipping a second independent clock
is exactly how a fleet ends up with two that disagree.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from coord.notifier import collect as collect_mod
from coord.notifier.baseline import Baseline, Stratum, build_baselines
from coord.notifier.digest import (
    build_digest,
    digest_due,
    is_quiet,
    partition,
    to_message,
)
from coord.notifier.models import NotifyEvent
from coord.notifier.predicate import evaluate, select_deliverable
from coord.notifier.store import (
    NotifierState,
    load_state,
    record_delivered,
    record_held,
    save_state,
)
from coord.notifier.transport import Transport, build_transport, safe_send

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    """What one tick did.  Never an exception, always a report."""

    enabled: bool = False
    quiet: bool = False
    raised: list[NotifyEvent] = field(default_factory=list)
    delivered: list[NotifyEvent] = field(default_factory=list)
    deferred: list[NotifyEvent] = field(default_factory=list)
    digest: NotifyEvent | None = None
    failed: list[tuple[NotifyEvent, str]] = field(default_factory=list)
    error: str | None = None

    def __bool__(self) -> bool:
        return bool(self.delivered or self.deferred or self.digest)

    def summary(self) -> str:
        if not self.enabled:
            return "notifier disabled"
        if self.error:
            return f"notifier error: {self.error}"
        bits = [f"{len(self.raised)} raised"]
        if self.delivered:
            bits.append(f"{len(self.delivered)} delivered")
        if self.deferred:
            bits.append(f"{len(self.deferred)} held (quiet hours)")
        if self.digest:
            bits.append("digest sent")
        if self.failed:
            bits.append(f"{len(self.failed)} undelivered")
        return ", ".join(bits)


def _ceilings(cfg: Any) -> dict[str, float] | None:
    """Merge configured cold-start ceilings (minutes) over the defaults."""
    from coord.notifier.baseline import DEFAULT_COLD_CEILINGS  # noqa: PLC0415

    overrides = getattr(cfg, "cold_ceiling_mins", None) or {}
    if not overrides:
        return None
    merged = dict(DEFAULT_COLD_CEILINGS)
    merged.update({str(k): float(v) * 60.0 for k, v in overrides.items()})
    return merged


def compute_baselines(
    config: Any,
    *,
    rows: Sequence[Mapping[str, Any]] | None = None,
    labels_by_issue: Mapping[tuple[str, int], list[str]] | None = None,
) -> dict[Stratum, Baseline]:
    """Learn the stratified baselines this fleet's history supports."""
    cfg = getattr(config, "notifications", None)
    rows = list(rows) if rows is not None else collect_mod.history_rows()
    labels = (
        dict(labels_by_issue)
        if labels_by_issue is not None
        else collect_mod.issue_label_index()
    )
    return build_baselines(
        rows,
        labels_by_issue=labels,
        min_samples=int(getattr(cfg, "min_samples", 5) or 5),
        percentile_q=float(getattr(cfg, "percentile", 90.0) or 90.0),
        ceilings=_ceilings(cfg),
        silence_fraction=float(getattr(cfg, "silence_fraction", 0.5) or 0.5),
    )


def deliver(
    events: Sequence[NotifyEvent],
    transport: Transport,
    state: NotifierState,
    *,
    now: float,
) -> tuple[list[NotifyEvent], list[tuple[NotifyEvent, str]]]:
    """Push *events*, recording only the ones that actually landed.

    A failed send is **not** ledgered.  The condition is almost certainly
    still true next tick, so the event re-derives and re-sends on its own —
    which means an ntfy server that was down for an hour costs a delayed
    notification, not a lost one.  Recording a failed send as delivered
    would silently swallow exactly the event this feature exists to carry.
    """
    delivered: list[NotifyEvent] = []
    failed: list[tuple[NotifyEvent, str]] = []
    for event in events:
        result = safe_send(transport, to_message(event))
        if result.ok:
            delivered.append(event)
        else:
            failed.append((event, result.error or "unknown transport failure"))
    if delivered:
        record_delivered(state, delivered, now=now)
    return delivered, failed


def _tick(
    config: Any,
    *,
    now: float,
    transport: Transport | None,
    state: NotifierState | None,
    agent_status: Any = None,
    fleet_health: Mapping[str, Any] | None = None,
    snapshot: Any = None,
    baselines: Mapping[Stratum, Baseline] | None = None,
    persist: bool = True,
) -> TickResult:
    cfg = getattr(config, "notifications", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        return TickResult(enabled=False)

    state = state if state is not None else load_state()
    transport = transport if transport is not None else build_transport(cfg)
    window = getattr(cfg, "quiet_hours", None)

    if snapshot is None:
        snapshot = collect_mod.collect(
            config,
            now=now,
            notifier_state=state,
            agent_status=agent_status,
            fleet_health=fleet_health,
        )
    if baselines is None:
        baselines = compute_baselines(config)

    raised = evaluate(
        snapshot,
        baselines,
        stall_grace_secs=float(getattr(cfg, "stall_grace_mins", 20.0) or 20.0) * 60.0,
    )
    fresh = select_deliverable(raised, state.ledger)

    result = TickResult(enabled=True, quiet=is_quiet(window, now), raised=list(fresh))

    send_now, hold = partition(fresh, window, now)
    if hold:
        state.deferred.extend(hold)
        # Ledger held events at hold time, not only at delivery time — see
        # `record_held`'s docstring.  Without this a persisting condition
        # (halted drive, parked gate, stalled worker) looks "fresh" to
        # `select_deliverable` on every tick for the rest of the quiet-hours
        # window and duplicate-floods `state.deferred` (#1632 fix
        # iteration 1).
        record_held(state, hold, now=now)
        result.deferred = list(hold)

    delivered, failed = deliver(send_now, transport, state, now=now)
    result.delivered = delivered
    result.failed = failed

    # The 08:00 flush.  One digest, everything the night held, nothing
    # discarded.  The held events were already ledgered when they were
    # raised, so a failed digest send does not resurrect them individually.
    if digest_due(state.deferred, window, now):
        digest = build_digest(state.deferred, now=now)
        sent = safe_send(transport, to_message(digest))
        if sent.ok:
            result.digest = digest
            state.deferred = []
            state.overflow = 0
        else:
            result.failed.append((digest, sent.error or "digest send failed"))

    if persist:
        save_state(state, now=now)
    return result


def tick(
    config: Any,
    *,
    now: float | None = None,
    transport: Transport | None = None,
    state: NotifierState | None = None,
    agent_status: Any = None,
    fleet_health: Mapping[str, Any] | None = None,
    snapshot: Any = None,
    baselines: Mapping[Stratum, Baseline] | None = None,
    persist: bool = True,
) -> TickResult:
    """Run one notifier tick.  **Never raises.**

    Callers on the daemon's hot path (`coord serve`'s ``_tick_loop``) rely
    on that unconditionally: an advisory channel that can throw is an
    advisory channel that can take reconciliation, dispatch or the merge
    drain down with it.
    """
    now = time.time() if now is None else float(now)
    try:
        return _tick(
            config,
            now=now,
            transport=transport,
            state=state,
            agent_status=agent_status,
            fleet_health=fleet_health,
            snapshot=snapshot,
            baselines=baselines,
            persist=persist,
        )
    except Exception as exc:  # noqa: BLE001 — see docstring; this is the boundary
        log.warning("notifier: tick failed (%s: %s)", type(exc).__name__, exc, exc_info=True)
        return TickResult(enabled=True, error=f"{type(exc).__name__}: {exc}")
