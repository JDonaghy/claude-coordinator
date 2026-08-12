"""Per-leg spend ceiling (#2131) — the policy layer that kills and escalates a
worker leg whose live cost blows past a configured threshold.

WHY THIS EXISTS.  Measured 2026-08-08 → 08-11 (393 legs, $907.04): the most
expensive single leg cost **$18.74**, twelve legs cost over $10, and those
twelve — 3% of all legs — carried **19% of the entire bill**.  Nothing anywhere
capped what a single leg could spend.  A leg at ~5x a normal work leg is
almost certainly stuck, looping, or mis-scoped, and no human is watching a
headless `claude -p` at 03:00.

WHAT THIS MODULE IS.  Two primitives, deliberately kept out of
:mod:`coord.agent` so they are unit-testable without spawning a worker:

1. :class:`LiveCostMeter` — an **incremental** reader over a worker's
   stream-json transcript that answers "how much has this leg spent so far?"
   in dollars, or ``None`` when it cannot tell.
2. :func:`format_spend_ceiling_reason` / :func:`is_spend_ceiling_reason` — the
   stable, greppable ``failure_reason`` prefix that makes a ceiling kill
   distinguishable from an ordinary crash everywhere downstream (`coord
   status`, the TUI, `coord retry`'s refusal, the escalation record).

FAIL OPEN.  Every unreadable/unparseable/unpriceable case returns ``None``
from :meth:`LiveCostMeter.read`, and the caller lets the leg run.  Killing
real work over a parse failure is strictly worse than the overspend this
guards against.

--------------------------------------------------------------------------
KNOWN LIMIT #1 — HEADLESS LEGS ONLY.
--------------------------------------------------------------------------
The meter reads a **stream-json** transcript.  Interactive legs
(``claude-pty``, the ``--fix-of`` / ``--merge-of`` / chat sessions) are
precisely the logs that are *not* stream-json (#1710), so they emit nothing
this can price and :meth:`read` returns ``None`` for them forever — i.e. an
interactive session is **NOT capped by this ceiling**.  That is the right
trade (headless work legs are where the spend is), but nobody should assume
otherwise.  Extending cost capture to interactive legs is #2129's territory
and is deliberately not attempted here.

--------------------------------------------------------------------------
KNOWN LIMIT #2 — MID-FLIGHT COST IS AN *ESTIMATE*, NOT THE BILL.
--------------------------------------------------------------------------
#2131's premise was that ``cost_so_far`` (``coord/agent.py``'s status path)
already carries live spend.  It does not: :func:`coord.worker_events.
update_summary` only ever sets ``total_cost_usd`` from a ``result`` event, and
a real ``claude -p --output-format stream-json`` transcript contains **exactly
one** ``result`` line, written when the session ends.  So ``cost_so_far`` is
``0.0`` for the entire life of a running leg, and a ceiling keyed on it alone
could never fire before the money was already spent.

So the meter prices the per-turn ``message.usage`` block that every
``assistant`` event *does* carry, against :func:`coord.config._default_pricing`
— the same list-price table :mod:`coord.usage_rollup` already uses to estimate
legs with no captured cost.  Consequences, stated rather than hidden:

* The number is **list price for the reported model**, not the invoice.  It
  ignores per-account discounts and (per #2128) whatever systematic
  miscounting the measurement path still has.  Calibrate the ceiling against
  ``coord usage``'s own numbers: this deliberately uses the identical rate
  table and the identical four token buckets, so the two agree with each
  other even where both differ from the bill.
* **It undercounts, so the ceiling fires late, not early.**  Measured against
  one real opus transcript whose terminal ``result`` reported ``$0.5309``,
  this estimator reads ``$0.3500`` (~34% low).  Most of the gap is the
  1-hour-TTL cache: the transcript's cache-creation tokens were entirely
  ``ephemeral_1h``, which bills ~2x input, while the shared rate table prices
  cache creation at the 5-minute 1.25x.  Pricing the TTL split explicitly
  recovers about a third of the gap ($0.4110) but would make this disagree
  with ``coord usage``, which is the worse trade for a knob an operator has
  to calibrate by eye — so it is deliberately not done here.  Erring low
  means a leg overshoots its ceiling somewhat before dying, never that
  healthy legs get killed early; set the ceiling with that in mind, and
  expect it to tighten when #2128 lands.
* One assistant *message* is emitted as several ``assistant`` *events* (one
  per content block), each repeating the same ``usage``.  They are deduped by
  ``message.id`` — without that the estimate ran ~45% HIGH on the same
  transcript, i.e. in the direction that kills healthy legs.
* A turn whose model is unrecognized (:data:`coord.usage_rollup.UNKNOWN_MODEL`)
  contributes **nothing** — it is never guessed at a tier.  A whole leg of
  unrecognized turns therefore reads ``None`` (fail open), not ``$0``.
* Once the terminal ``result`` event lands, its ``total_cost_usd`` is
  authoritative and replaces the estimate.  By then the leg is over, so this
  only matters for the reason string, not for the kill decision.

THE CEILING IS A SAFETY NET, NOT A SCHEDULER.  It should fire rarely.  If it
fires often, the ceiling is too low or the briefings are bad.
"""

from __future__ import annotations

import json
from pathlib import Path

# A stable, greppable prefix stamped onto `Assignment.failure_reason` whenever
# a leg is killed by the ceiling.  Mirrors
# `coord.worker_events.USAGE_LIMIT_REASON_PREFIX` in shape and purpose: it is
# what lets `coord retry`, the auto-reassign skip, and the escalation record
# tell a ceiling kill apart from an ordinary crash.  NEVER change it without
# updating `is_spend_ceiling_reason`'s callers — a generic FAILED here means
# `coord retry` cheerfully re-spends the money.
SPEND_CEILING_REASON_PREFIX = "spend ceiling — "

#: Fraction of the ceiling at which a warning is written to the worker's own
#: log before anything is killed.  Deliberately a module constant rather than
#: a config knob: it rides the agent deploy lane (docs/AGENT_OPERATIONS.md),
#: and one more wire field is one more way to 400 an agent that predates it.
WARN_FRACTION = 0.8


def format_spend_ceiling_reason(
    cost_usd: float, ceiling_usd: float, assignment_type: str | None = None
) -> str:
    """Render the one-liner stamped onto ``failure_reason`` for a ceiling kill.

    Example: ``"spend ceiling — $12.41 of $8.00 (type=work)"``.
    """
    suffix = f" (type={assignment_type})" if assignment_type else ""
    return (
        f"{SPEND_CEILING_REASON_PREFIX}${cost_usd:.2f} of "
        f"${ceiling_usd:.2f}{suffix}"
    )


def is_spend_ceiling_reason(reason: str | None) -> bool:
    """True iff *reason* is a ``failure_reason`` stamped by the ceiling."""
    return bool(reason) and reason.startswith(SPEND_CEILING_REASON_PREFIX)


class LiveCostMeter:
    """Incremental live-spend reader over one worker's stream-json log.

    Constructed once per reap and polled on the existing wait loop, so each
    call reads only the bytes appended since the last one — a full re-parse
    every couple of seconds over a transcript that can reach tens of MB is
    exactly the kind of cost this feature exists to avoid.

    A trailing partial line (the worker is mid-write) is buffered and
    re-joined on the next read rather than discarded, so no turn is ever
    silently dropped from the total.
    """

    def __init__(self, log_path: str | Path | None, rates: dict | None = None):
        self._path = Path(log_path) if log_path else None
        self._offset = 0
        self._pending = b""
        self._tokens: dict[str, list[int]] = {}
        self._seen_messages: set[str] = set()
        self._authoritative: float | None = None
        self._saw_priceable = False
        self._broken = False
        if rates is None:
            from coord.config import _default_pricing  # noqa: PLC0415

            rates = _default_pricing()
        self._rates = rates
        self._last: float | None = None

    # ── public API ──────────────────────────────────────────────────────

    @property
    def last(self) -> float | None:
        """The most recent value :meth:`read` returned (``None`` if never)."""
        return self._last

    def read(self) -> float | None:
        """Best-known spend for this leg in USD, or ``None`` when unknown.

        ``None`` is the fail-open answer and means "let the leg run": the log
        is missing/unreadable, it is not stream-json (an interactive leg —
        see this module's docstring), or nothing priceable has been emitted
        yet.  It is never a stand-in for ``$0``.
        """
        self._consume()
        if self._authoritative is not None:
            self._last = self._authoritative
            return self._last
        if not self._saw_priceable:
            self._last = None
            return None
        self._last = self._estimate()
        return self._last

    # ── internals ───────────────────────────────────────────────────────

    def _consume(self) -> None:
        """Fold every newly-appended complete line into the running totals."""
        if self._path is None or self._broken:
            return
        try:
            with open(self._path, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
                self._offset = fh.tell()
        except OSError:
            # Fail open, and stay open: a log that vanished or became
            # unreadable must never kill the leg reading it.
            self._broken = True
            return

        if not chunk:
            return
        data = self._pending + chunk
        lines = data.split(b"\n")
        # The last element is whatever followed the final newline — an empty
        # bytestring when the chunk ended cleanly, a partial line otherwise.
        self._pending = lines.pop()
        for line in lines:
            self._fold(line)

    def _fold(self, line: bytes) -> None:
        text = line.strip()
        # The agent writes its own `# argv=…` header and `# reap: …` notes to
        # this same file; they are not events.
        if not text or text.startswith(b"#"):
            return
        try:
            event = json.loads(text)
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict):
            return

        etype = event.get("type")
        if etype == "result":
            cost = event.get("total_cost_usd")
            if cost is None:
                cost = event.get("cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                # The terminal event is the real number; it supersedes every
                # estimate accumulated up to here.
                self._authoritative = float(cost)
            return

        if etype != "assistant":
            return
        message = event.get("message")
        if not isinstance(message, dict):
            return
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return

        # One assistant message is emitted as SEVERAL `assistant` events —
        # one per content block (text, then each tool_use) — and every one of
        # them repeats the SAME `message.usage`. Measured on a real
        # transcript: 41 assistant events for 19 distinct messages, which
        # inflated the estimate by ~45% before this dedupe. Key on
        # `message.id`; a message with no id is counted (better to slightly
        # over-count one turn than to drop it).
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            if message_id in self._seen_messages:
                return
            self._seen_messages.add(message_id)

        from coord.usage_rollup import UNKNOWN_MODEL, normalize_model  # noqa: PLC0415

        canonical = normalize_model(message.get("model") or event.get("model"))
        if canonical == UNKNOWN_MODEL or canonical not in self._rates:
            # Never guessed at a tier, never priced as $0 — an all-unknown
            # leg stays `None` (fail open) rather than reading as free.
            return

        bucket = self._tokens.setdefault(canonical, [0, 0, 0, 0])
        bucket[0] += _int(usage.get("input_tokens"))
        bucket[1] += _int(usage.get("output_tokens"))
        bucket[2] += _int(usage.get("cache_read_input_tokens"))
        bucket[3] += _int(usage.get("cache_creation_input_tokens"))
        self._saw_priceable = True

    def _estimate(self) -> float:
        total = 0.0
        for canonical, (inp, out, cread, ccreate) in self._tokens.items():
            rates = self._rates.get(canonical)
            if rates is None:
                continue
            total += (
                inp * rates.input
                + out * rates.output
                + cread * rates.cache_read
                + ccreate * rates.cache_creation
            ) / 1_000_000.0
        return total


def _int(value: object) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
