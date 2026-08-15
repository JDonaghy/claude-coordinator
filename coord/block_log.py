"""Phase 0 stall log (#2235) — every ``blocked``/``parked`` entry, recorded.

**What this is.** #2235's evidence table showed that *five of seven* overnight
stalls named a symptom rather than a cause: the queue said "stale test
verdict" where the truth was a four-branch conflict, said "CI red, 2/2
attempts" for a build that went green 23 minutes later.  That finding is the
load-bearing constraint on everything Phase 1+ might do, and it was
reconstructed by hand, once, from a single morning's triage.  Phase 1's scope
is explicitly gated on **two weeks of this log**.

So this module records, for every entry that reaches ``blocked`` or
``parked``:

* the reason the queue **stated** at the moment it stopped (verbatim), and
* how the entry eventually **left** that state — which is the only honest
  Phase-0 read on its *true* cause — and
* whether a **human had to act** to get it out.

**What the RECORDER is emphatically NOT.**  The writer above does not
diagnose.  It runs no ``gh`` call, re-derives no gate, and consults no live
state that the tick has not already fetched for its own purposes.

**Phase 1 (#2276) fills the column Phase 0 cannot.**  ``true_cause`` is empty
on every ``enter`` record — at that instant nobody knows it — and on ``exit``
it is derived from *the release the queue itself performed*, which is still
the queue talking about itself.  #2235's finding is that the queue is wrong
about this five times out of seven.  So :mod:`coord.queue_diagnose` re-derives
the cause from live state and appends a third record shape here,
:data:`EVENT_DIAGNOSIS`, which :func:`episodes` folds onto the open episode as
its ``true_cause``.  That module owns the *deriving*; this one owns the
*record*, the pairing, and the honesty accounting — how often the derived
cause later agreed with how the episode actually resolved
(:func:`agreement_for`), reported as a rate rather than assumed
(:func:`summarize`).  A diagnostician that is confidently wrong is worse than
one that abstains, which is why ``unknown`` is counted separately from
``disagreed`` and never folded into it.

**Recording must never change a decision.**  Every public entry point here is
best-effort and swallows its own exceptions (:func:`record`), for the same
reason :mod:`coord.audit` does: the tick's merge/dispatch/attempt accounting
is the write that matters, and an observability append that can fail a tick is
strictly worse than no observability.  Nothing in this module is ever consulted
by :func:`coord.drive_queue.plan_tick` — it reads a plan that has already been
decided, and writes to a file no decision path reads.

**Storage.** One JSON object per line, appended, at
:func:`block_log_path`.  Append-only on purpose: an *episode* (entry blocked →
entry released) is two records that the reader pairs up (:func:`episodes`),
never one record mutated in place.  That keeps the writer a single ``O(1)``
append with no read-modify-write race between a tick and an operator command
running concurrently on the same host, and it keeps the raw file honest — a
resolution that contradicts an earlier one is visible rather than overwritten.

The file is per-host, because ticks are per-host and this is instrumentation,
not board state.  Reading two weeks of it off two machines is a ``cat``; a new
board table plus a daemon route is a schema migration and a network dependency
in the tick's write path, which is a poor trade for a temporary measurement.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from coord.drive_queue import (
    STATE_BLOCKED,
    STATE_PARKED,
    QueueEntry,
    TickPlan,
)

_log = logging.getLogger(__name__)

__all__ = [
    "AGREEMENT_ABSTAINED",
    "AGREEMENT_AGREED",
    "AGREEMENT_DISAGREED",
    "AGREEMENT_UNDECIDED",
    "EVENT_DIAGNOSIS",
    "EVENT_ENTER",
    "EVENT_RESOLVE",
    "STALL_STATES",
    "agreement_for",
    "block_log_path",
    "diagnosis_event",
    "enter_event",
    "episodes",
    "operator_resolution_event",
    "plan_events",
    "read_events",
    "record",
    "summarize",
]

# The two states this log exists to measure.  `failed` is deliberately absent:
# nothing in `coord.drive_queue` writes it any more (see `_reconcile_blocked`'s
# note), so a `failed` row is a pre-existing artefact, not a stall this queue
# produced.
STALL_STATES: frozenset[str] = frozenset({STATE_BLOCKED, STATE_PARKED})

EVENT_ENTER = "enter"
EVENT_RESOLVE = "resolve"
#: #2276 Phase 1.  A read-only re-derivation of an OPEN episode's cause, from
#: live state rather than from the queue's own stated reason.  Appended to the
#: same file rather than a new store for the same reason `enter`/`resolve` are
#: two records and not one mutated row: the writer stays an O(1) append with
#: no read-modify-write race, and a later diagnosis that contradicts an earlier
#: one stays visible instead of overwriting it.
EVENT_DIAGNOSIS = "diagnosis"

# Rotate at 4 MiB — comfortably more than two weeks at this fleet's rate
# (single-digit stalls a night, ~400 bytes a record), and small enough that
# `read_events` stays a whole-file parse rather than needing an index.
MAX_LOG_BYTES = 4 * 1024 * 1024


def block_log_path() -> Path:
    """Where the Phase-0 log lives.

    A function, not a module constant, for the same reason
    :func:`coord.filelock.drive_queue_lock_path` is: a constant captured at
    import freezes whatever ``Path.home()`` was at process start, which breaks
    both tests with a relocated ``$HOME`` and the daemon.
    """
    override = os.environ.get("COORD_BLOCK_LOG")
    if override:
        return Path(override)
    return Path.home() / ".coord" / "queue-block-log.jsonl"


# ── classification (derived, never probed) ───────────────────────────────────
#
# The markers below are the ones the queue's own reconcile branches already
# stamp into `last_reason`.  Reusing them — rather than inventing a parallel
# taxonomy, or worse, re-deriving the gate — is the same convention
# `is_permanent_block_reason` and `is_gate_a_refusal_reason` follow, and it is
# what keeps this module a *recorder*: every string it classifies was written
# by the decision it is recording, not by a second opinion about it.

# (marker, true_cause, human_acted) — first match wins, so order is meaningful:
# the Gate-A release (#2063) must be tested before the generic park resume,
# because it is the one auto-release that a HUMAN caused.
_RESUME_CAUSES: tuple[tuple[str, str, bool], ...] = (
    (
        "(#2063)",
        "gate-a-signed — released only because a human recorded the sign-off",
        True,
    ),
    (
        "(#2230)",
        "gate-cleared-after-giveup — the merge gate read clear again after the "
        "queue had already given up; the stated reason was a stale symptom",
        False,
    ),
    (
        "(#2158)",
        "unrefreshable-reading — the park rested on a CI string with no writer "
        "left, and aged out rather than being released by real news",
        False,
    ),
    (
        "(#2182)",
        "ci-reported — a live re-check found the gate READY",
        False,
    ),
    (
        "(#1891)",
        "ci-reported — CI checks reported and the park released itself",
        False,
    ),
)

_LANDED_CAUSE = (
    "already-landed — the work merged or closed while the entry sat in this "
    "state, so the stated reason had outlived the thing it described"
)
_OPERATOR_CAUSE = (
    "operator-intervened — no automatic release fired; a human cleared it by "
    "hand, and what they actually fixed is NOT recorded here"
)
_UNKNOWN_CAUSE = "unclassified — left {state} by a transition this log does not recognise"


def _resume_cause(reason: str) -> tuple[str, bool]:
    """``(true_cause, human_acted)`` for an auto-resume, from its own reason."""
    for marker, cause, human in _RESUME_CAUSES:
        if marker in reason:
            return cause, human
    return (
        "auto-released — the tick resumed the entry without naming a known "
        "release condition",
        False,
    )


# ── event construction (pure) ────────────────────────────────────────────────


def _final_states(plan: TickPlan) -> dict[str, str]:
    """The state each key ENDS this tick in, per ``plan.writes()`` order.

    ``writes()`` is the plan's own apply order (reconciles, holds, blocked,
    deferrals), so last-write-wins here matches exactly what
    ``_apply_writes`` persists.  A key absent from the result is a key whose
    state this tick did not touch — not a key that stayed put by accident.
    """
    out: dict[str, str] = {}
    for key, updates in plan.writes():
        state = updates.get("state")
        if state:
            out[key] = str(state)
    return out


def _final_reasons(plan: TickPlan) -> dict[str, str]:
    """The ``last_reason`` each key ends this tick with, same order/rule."""
    out: dict[str, str] = {}
    for key, updates in plan.writes():
        reason = updates.get("last_reason")
        if reason:
            out[key] = str(reason)
    return out


def _outcomes(plan: TickPlan) -> dict[str, str]:
    """The reconcile outcome per key, for keys a reconcile touched."""
    return {item.key: item.outcome for item in plan.reconciles}


def _entry_fields(entry: QueueEntry | None) -> dict[str, Any]:
    if entry is None:
        return {}
    return {
        "position": entry.position,
        "attempts": entry.attempts,
        "resumes": entry.resumes,
        "machine": entry.machine or "",
    }


def plan_events(
    entries: Sequence[QueueEntry],
    plan: TickPlan,
    *,
    host: str = "",
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Every Phase-0 record one already-decided tick implies.

    Pure: *entries* is the PRE-tick snapshot the shell handed to
    :func:`coord.drive_queue.plan_tick`, *plan* is what that call returned.
    Nothing here re-reads the board, and nothing it returns is fed back into
    any decision — the caller appends the result and moves on.

    Two record shapes come out:

    * ``enter``   — a key whose state this tick moves INTO ``blocked``/
      ``parked`` from something else.  Carries the reason the queue stated,
      verbatim, and ``true_cause: ""`` — at this instant nobody knows it, and
      recording a guess is precisely the failure #2235 documents.
    * ``resolve`` — a key whose state this tick moves OUT of ``blocked``/
      ``parked``.  Carries the classification derived from the release itself
      (:func:`_resume_cause`), which is the strongest honest claim available
      without diagnosing.

    A key that is blocked before AND after (the overwhelmingly common case —
    a blocked entry that stayed blocked, or #2230's ``oscillating`` outcome,
    which rewrites ``last_reason`` but deliberately does not move the row)
    produces nothing.  This log counts transitions, not tick-seconds.
    """
    stamp = time.time() if now is None else now
    by_key = {e.key: e for e in entries}
    states = _final_states(plan)
    reasons = _final_reasons(plan)
    outcomes = _outcomes(plan)
    # A `Blocked` item's own reason is the escalation prose, which is at least
    # as specific as whatever `last_reason` its paired reconcile wrote — and
    # for the launch-failure path it is the ONLY reason there is.
    block_reasons = {item.key: item.reason for item in plan.blocked}

    out: list[dict[str, Any]] = []
    for key, new_state in sorted(states.items()):
        entry = by_key.get(key)
        old_state = entry.state if entry is not None else ""
        was_stalled = old_state in STALL_STATES
        now_stalled = new_state in STALL_STATES
        if was_stalled == now_stalled:
            continue
        reason = block_reasons.get(key) or reasons.get(key, "")
        if now_stalled:
            out.append(
                _enter_record(
                    key,
                    new_state,
                    reason,
                    entry,
                    host=host,
                    now=stamp,
                    outcome=outcomes.get(key, ""),
                )
            )
        else:
            out.append(
                _resolve_record(
                    key,
                    old_state,
                    new_state,
                    reason,
                    entry,
                    host=host,
                    now=stamp,
                    outcome=outcomes.get(key, ""),
                )
            )
    return out


def _enter_record(
    key: str,
    state: str,
    reason: str,
    entry: QueueEntry | None,
    *,
    host: str,
    now: float,
    outcome: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "event": EVENT_ENTER,
        "ts": now,
        "key": key,
        "state": state,
        "from_state": entry.state if entry is not None else "",
        # VERBATIM.  The whole point of the exercise is comparing this against
        # what the resolution later reveals, so it must not be normalised,
        # truncated or "helpfully" rewritten on the way in.
        "stated_reason": reason,
        # Filled by nobody.  A Phase-0 `enter` record cannot know the true
        # cause; the paired `resolve` is where that appears.
        "true_cause": "",
        "human_acted": None,
        "outcome": outcome,
        "host": host,
        "source": "tick",
    }
    record.update(_entry_fields(entry))
    return record


def _resolve_record(
    key: str,
    old_state: str,
    new_state: str,
    reason: str,
    entry: QueueEntry | None,
    *,
    host: str,
    now: float,
    outcome: str,
) -> dict[str, Any]:
    if outcome == "done" or new_state == "done":
        true_cause, human = _LANDED_CAUSE, False
        resolution = "landed"
    elif outcome == "resumed":
        true_cause, human = _resume_cause(reason)
        resolution = "auto_resumed"
    else:
        true_cause, human = _UNKNOWN_CAUSE.format(state=old_state), False
        resolution = outcome or "moved"
    record: dict[str, Any] = {
        "event": EVENT_RESOLVE,
        "ts": now,
        "key": key,
        "state": new_state,
        "from_state": old_state,
        "stated_reason": entry.last_reason if entry is not None else "",
        "resolution": resolution,
        "true_cause": true_cause,
        "human_acted": human,
        "release_reason": reason,
        "outcome": outcome,
        "host": host,
        "source": "tick",
    }
    record.update(_entry_fields(entry))
    return record


def enter_event(
    entry: QueueEntry,
    *,
    state: str,
    reason: str,
    attempts: int | None = None,
    host: str = "",
    now: float | None = None,
    source: str = "tick",
) -> dict[str, Any]:
    """An ``enter`` record for a stall the TICK PLAN does not describe.

    One caller today: the launch-failure branch of ``coord drive-queue tick``,
    where the entry is blocked by the ``coord drive --tmux`` subprocess exiting
    non-zero *after* the plan was applied.  That transition is invisible to
    :func:`plan_events` — it is not in ``plan.writes()`` and never could be —
    and it is #2235's own ``stick-demo#1`` row ("dispatch failed"), so leaving
    it out would omit exactly the category the log was built to count.

    *attempts* overrides the snapshot's own count for the same reason the
    branch itself recomputes it (see the ``base_attempts`` note there): the
    row's persisted value has already moved on.
    """
    record = _enter_record(
        entry.key,
        state,
        reason,
        entry,
        host=host,
        now=time.time() if now is None else now,
        outcome="launch_failed",
    )
    record["source"] = source
    if attempts is not None:
        record["attempts"] = attempts
    return record


def operator_resolution_event(
    entry: QueueEntry,
    *,
    resolution: str,
    host: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """The ``resolve`` record for a stall a HUMAN cleared by hand.

    This is the other half of the metric #2235 asks for, and the half no tick
    can see: an operator running ``coord drive-queue remove`` on a blocked
    entry is the intervention the whole plan is trying to drive to zero.
    ``human_acted`` is unconditionally ``True`` here — the command ran because
    a person typed it.

    ``true_cause`` is deliberately the honest non-answer: what the operator
    actually fixed happened outside this process and is not recoverable from
    here.  A field that says "operator intervened" is useful; one that
    guesses at *why* would poison the very dataset Phase 1 is meant to size
    itself from.
    """
    stamp = time.time() if now is None else now
    record: dict[str, Any] = {
        "event": EVENT_RESOLVE,
        "ts": stamp,
        "key": entry.key,
        "state": "",
        "from_state": entry.state,
        "stated_reason": entry.last_reason,
        "resolution": resolution,
        "true_cause": _OPERATOR_CAUSE,
        "human_acted": True,
        "release_reason": "",
        "outcome": "",
        "host": host,
        "source": "operator",
    }
    record.update(_entry_fields(entry))
    return record


# ── #2276 Phase 1: the diagnosis record, and scoring it ──────────────────────


def diagnosis_event(
    *,
    key: str,
    state: str,
    stated_reason: str,
    true_cause: str,
    cause: str,
    confidence: str,
    evidence: Sequence[str] = (),
    contradicts_stated: bool = False,
    trigger: str = "",
    host: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """A ``diagnosis`` record for one still-open stall.

    Deliberately NOT a ``resolve``: the entry is still stalled, and a record
    that closed the episode would make an observation look like an outcome —
    which is the exact conflation (*a stated reason read as a cause*) this
    whole plan exists to undo.  :func:`episodes` folds it onto the open
    episode instead, so ``coord drive-queue block-log`` shows a populated
    ``true_cause`` where it previously showed ``(unresolved)`` while the
    episode itself stays open.

    ``human_acted`` is absent on purpose rather than ``False``: Phase 1 acts on
    nothing and knows nothing about what a human did, and a ``False`` here
    would be counted by :func:`summarize` as an auto-release that never
    happened.

    Every argument is a plain scalar so this module needs no import from
    :mod:`coord.queue_diagnose` — the recorder must not depend on the thing it
    records.
    """
    return {
        "event": EVENT_DIAGNOSIS,
        "ts": time.time() if now is None else now,
        "key": key,
        "state": state,
        # VERBATIM, same contract as `_enter_record`: the comparison between
        # this and `true_cause` below IS the deliverable.
        "stated_reason": stated_reason,
        "true_cause": true_cause,
        "cause": cause,
        "confidence": confidence,
        "evidence": [str(line) for line in evidence],
        "contradicts_stated": bool(contradicts_stated),
        "trigger": trigger,
        "host": host,
        "source": "diagnostician",
    }


AGREEMENT_AGREED = "agreed"
AGREEMENT_DISAGREED = "disagreed"
#: The diagnosis said ``unknown``.  A first-class verdict with NO penalty:
#: #2276 is explicit that a diagnostician which abstains is better than one
#: that guesses, so this is counted apart from `disagreed` and kept out of the
#: disagreement rate's denominator entirely.
AGREEMENT_ABSTAINED = "abstained"
#: The RESOLUTION is the unknown one.  `operator-intervened` says in as many
#: words that what the human fixed is not recorded, so scoring a diagnosis
#: against it would be scoring it against noise.
AGREEMENT_UNDECIDED = "undecided"

#: ``resolve``-side cause slug -> the diagnosis causes that agree with it.
#: Keyed on the slug before the first " — ", the same split
#: :func:`summarize` buckets on.  A resolution absent from this table is
#: undecidable, not a disagreement — see :data:`AGREEMENT_UNDECIDED`.
_AGREEING_CAUSES: dict[str, frozenset[str]] = {
    "already-landed": frozenset({"pr-merged", "pr-closed"}),
    "gate-cleared-after-giveup": frozenset(
        {"nothing-blocking", "ci-green", "ci-pending"}
    ),
    "ci-reported": frozenset({"nothing-blocking", "ci-green", "ci-pending"}),
    "unrefreshable-reading": frozenset({"nothing-blocking", "ci-green", "no-pr"}),
}


def agreement_for(diagnosed_cause: str, resolution_cause: str) -> str:
    """Did Phase 1's derived cause match how the episode actually ended?

    #2235's stated route to Phase 2 is *"run it read-only and check whether
    its diagnosis matches what the human actually did"*, so this comparison
    is a deliverable, not a follow-up.  It is deliberately conservative in
    both directions: an unrecognised resolution scores ``undecided`` rather
    than being counted as a win, and an ``unknown`` diagnosis scores
    ``abstained`` rather than being counted as a loss.
    """
    diagnosed = str(diagnosed_cause or "").split(" — ")[0]
    resolved = str(resolution_cause or "").split(" — ")[0]
    if not diagnosed:
        return ""
    if diagnosed == "unknown":
        return AGREEMENT_ABSTAINED
    agreeing = _AGREEING_CAUSES.get(resolved)
    if agreeing is None:
        return AGREEMENT_UNDECIDED
    return AGREEMENT_AGREED if diagnosed in agreeing else AGREEMENT_DISAGREED


# ── persistence (best-effort, never raises) ──────────────────────────────────


def record(events: Iterable[Mapping[str, Any]], *, path: Path | None = None) -> int:
    """Append *events* to the log.  Returns how many landed.

    **Never raises.**  Same contract as :func:`coord.audit.record_audit`, for
    the same reason and with more force: the caller is a drive-queue tick
    mid-way through applying real writes, and #2235's own prohibitions make
    the point that a healer which damages the thing it observes is worse than
    the stall.  A full disk, a read-only ``$HOME``, or a schema this build
    does not understand costs the measurement, never the tick.
    """
    target = block_log_path() if path is None else path
    lines = []
    for event in events:
        try:
            lines.append(json.dumps(dict(event), sort_keys=True, separators=(",", ":")))
        except (TypeError, ValueError):  # pragma: no cover — defensive
            _log.debug("block-log: unserialisable event dropped", exc_info=True)
    if not lines:
        return 0
    blob = "\n".join(lines) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target)
        # One `write` of one buffer, in append mode: on POSIX that is the
        # atomic unit, so a tick and an operator command appending at the same
        # instant interleave whole records rather than shredding a line. This
        # is why the format is append-only in the first place.
        with target.open("a", encoding="utf-8") as handle:
            handle.write(blob)
    except OSError:
        _log.debug("block-log: append failed", exc_info=True)
        return 0
    return len(lines)


def _rotate_if_needed(target: Path) -> None:
    try:
        if target.stat().st_size < MAX_LOG_BYTES:
            return
    except OSError:
        return
    # One generation only.  This is a two-week measurement, not an archive;
    # keeping N generations would imply a retention policy nobody has decided.
    try:
        target.replace(target.with_suffix(target.suffix + ".1"))
    except OSError:  # pragma: no cover — defensive
        _log.debug("block-log: rotation failed", exc_info=True)


def read_events(
    *,
    path: Path | None = None,
    since: float | None = None,
) -> list[dict[str, Any]]:
    """Every record in the log, oldest first.  Unparseable lines are skipped.

    Skipping rather than raising is deliberate: a half-written final line
    (power loss mid-append) must not make two weeks of evidence unreadable.
    """
    target = block_log_path() if path is None else path
    out: list[dict[str, Any]] = []
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        if since is not None and float(parsed.get("ts") or 0.0) < since:
            continue
        out.append(parsed)
    out.sort(key=lambda item: float(item.get("ts") or 0.0))
    return out


# ── reading (pairing + summary) ──────────────────────────────────────────────


def episodes(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pair ``enter`` records with the ``resolve`` that closed them.

    One episode is one stall: an entry stopped, then (maybe) started again.
    An ``enter`` with no matching ``resolve`` is still an episode — with
    ``resolved=False`` — because "blocked and still blocked" is the outcome
    the plan cares most about, and dropping it would flatter the numbers.

    A ``resolve`` with no preceding ``enter`` (the log started mid-stall) is
    dropped rather than synthesised: an episode whose stated reason is
    unknown cannot answer the one question this log exists to answer.

    A ``diagnosis`` (#2276 Phase 1) folds onto the open episode: it fills
    ``true_cause`` — the column Phase 0 leaves empty and ``summarize`` buckets
    as ``(unresolved)`` — without closing the episode, because the entry is
    still stalled.  A diagnosis for a key with no open episode is dropped for
    the same reason an orphan ``resolve`` is: there is nothing to attach it
    to, and inventing the episode would fabricate a stated reason to compare
    against.  Once the episode resolves, ``true_cause`` reverts to the
    resolution's own account of what happened and the derived one is kept
    beside it as ``diagnosed_cause``, so ``agreement`` scores two independent
    claims rather than one claim against itself.
    """
    open_by_key: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for event in events:
        key = str(event.get("key") or "")
        if not key:
            continue
        kind = event.get("event")
        if kind == EVENT_ENTER:
            # A second `enter` without a `resolve` means the first episode's
            # release went unrecorded (a tick on another host, say). Close it
            # as unresolved rather than losing it.
            existing = open_by_key.pop(key, None)
            if existing is not None:
                out.append(existing)
            open_by_key[key] = {
                "key": key,
                "state": event.get("state", ""),
                "stated_reason": event.get("stated_reason", ""),
                "entered_at": float(event.get("ts") or 0.0),
                "attempts": event.get("attempts", 0),
                "host": event.get("host", ""),
                "resolved": False,
                "resolution": "",
                "true_cause": "",
                "human_acted": None,
                "resolved_at": None,
                "stalled_seconds": None,
                # #2276 Phase 1.  `diagnoses` is what `needs_diagnosis` spends
                # against the attempt budget, so it has to survive a restart —
                # which it does, being recounted from the log every read
                # rather than held in memory.
                "diagnoses": 0,
                "diagnosed_cause": "",
                "diagnosis_confidence": "",
                "diagnosis_evidence": [],
                "diagnosis_contradicts_stated": False,
                "agreement": "",
            }
        elif kind == EVENT_DIAGNOSIS:
            episode = open_by_key.get(key)
            if episode is None:
                continue
            cause = str(event.get("cause") or "")
            episode.update(
                {
                    "diagnoses": int(episode.get("diagnoses") or 0) + 1,
                    "diagnosed_cause": cause,
                    "diagnosis_confidence": str(event.get("confidence") or ""),
                    "diagnosis_evidence": list(event.get("evidence") or []),
                    "diagnosis_contradicts_stated": bool(
                        event.get("contradicts_stated")
                    ),
                    # The whole point of the column: an OPEN episode now has a
                    # cause where it used to have "".
                    "true_cause": str(event.get("true_cause") or ""),
                }
            )
        elif kind == EVENT_RESOLVE:
            episode = open_by_key.pop(key, None)
            if episode is None:
                continue
            resolved_at = float(event.get("ts") or 0.0)
            resolution_cause = str(event.get("true_cause") or "")
            episode.update(
                {
                    "resolved": True,
                    "resolution": event.get("resolution", ""),
                    "true_cause": resolution_cause,
                    "human_acted": bool(event.get("human_acted")),
                    "resolved_at": resolved_at,
                    "stalled_seconds": max(0.0, resolved_at - episode["entered_at"]),
                    "source": event.get("source", ""),
                    "agreement": agreement_for(
                        str(episode.get("diagnosed_cause") or ""), resolution_cause
                    ),
                }
            )
            out.append(episode)
    out.extend(open_by_key.values())
    out.sort(key=lambda item: float(item.get("entered_at") or 0.0))
    return out


def summarize(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The counts #2235's success metric is defined in terms of.

    ``human_acted`` is the numerator that must trend DOWN.  It is reported
    alongside ``open`` (still stalled) rather than in isolation, because a
    queue that stops needing interventions by leaving everything blocked
    forever is the failure mode, not the goal — the two numbers are only
    meaningful together.

    ``repeat_causes`` is #2235's tripwire, in the weakest form Phase 0 can
    honestly support: the same ``(repo, stated_reason-prefix)`` stalling more
    than once.  The issue's framing — *a repeat is a bug report, not a
    success* — needs that count visible from day one, or the trend line has
    nothing to be checked against.

    ``diagnosis`` is #2276's own success criterion, and it is reported as a
    measurement rather than an assertion.  ``disagreement_rate`` is
    ``disagreed / (agreed + disagreed)`` and is ``None`` — not ``0.0`` — when
    nothing scorable has resolved yet, because a rate of zero out of zero
    reads as a perfect record.  ``abstained`` sits outside that fraction
    entirely: #2276 makes ``unknown`` a first-class verdict with no penalty,
    and putting it in the denominator would penalise it.  ``contradicted``
    counts the episodes where the live evidence positively ruled the queue's
    stated reason out — the five-of-seven number, finally measured instead of
    reconstructed by hand.
    """
    by_state: dict[str, int] = {}
    by_cause: dict[str, int] = {}
    seen_pairs: dict[tuple[str, str], int] = {}
    human = 0
    auto = 0
    still_open = 0
    diagnosed = 0
    contradicted = 0
    agreement_counts: dict[str, int] = {}
    for item in items:
        if item.get("diagnosed_cause"):
            diagnosed += 1
            if item.get("diagnosis_contradicts_stated"):
                contradicted += 1
            verdict = str(item.get("agreement") or "")
            if verdict:
                agreement_counts[verdict] = agreement_counts.get(verdict, 0) + 1
        state = str(item.get("state") or "?")
        by_state[state] = by_state.get(state, 0) + 1
        if not item.get("resolved"):
            still_open += 1
        elif item.get("human_acted"):
            human += 1
        else:
            auto += 1
        cause = str(item.get("true_cause") or "").split(" — ")[0] or "(unresolved)"
        by_cause[cause] = by_cause.get(cause, 0) + 1
        repo = str(item.get("key") or "").split("#")[0]
        stated = " ".join(str(item.get("stated_reason") or "").split())[:60]
        if stated:
            # An episode with NO stated reason cannot repeat one. Counting the
            # empty string would make every reasonless row in a repo collide
            # into a single fat "repeat", which is the opposite of a tripwire.
            pair = (repo, stated)
            seen_pairs[pair] = seen_pairs.get(pair, 0) + 1
    repeats = {
        f"{repo}: {stated}": count
        for (repo, stated), count in sorted(seen_pairs.items())
        if count > 1
    }
    agreed = agreement_counts.get(AGREEMENT_AGREED, 0)
    disagreed = agreement_counts.get(AGREEMENT_DISAGREED, 0)
    scored = agreed + disagreed
    return {
        "episodes": len(items),
        "by_state": by_state,
        "by_cause": by_cause,
        "human_acted": human,
        "auto_released": auto,
        "open": still_open,
        "repeat_causes": repeats,
        "diagnosis": {
            "diagnosed": diagnosed,
            "contradicted_stated_reason": contradicted,
            "agreed": agreed,
            "disagreed": disagreed,
            "abstained": agreement_counts.get(AGREEMENT_ABSTAINED, 0),
            "undecided": agreement_counts.get(AGREEMENT_UNDECIDED, 0),
            "disagreement_rate": (disagreed / scored) if scored else None,
        },
    }
