"""Milestone gate state machine — S-1 of epic #1440 (#1929).

The skeleton the rest of #1440 hangs off: a durable, board-backed **gate
record** per driven milestone plus the pure transition function that walks it

    Gate A (contract) → work → Gate B (architecture) → Gate C (acceptance)
                      → Gate D (ship) → done

exactly once, resumably.  Where ``drive-issue.sh`` gets its resumability from
re-reading the board on every loop, the daemon gets it from
:func:`coord.state.list_milestone_gates`: the record says which gate a
milestone is in, when it entered, and what it is waiting on, so a daemon that
restarts mid-milestone picks the walk back up without ever re-running a gate
it already cleared.

**Every edge here is a "hold and report".**  That is deliberate and is the
whole point of shipping this first.  Each of #1440's other children replaces
exactly one hold in :func:`evaluate_gate` with a real decision (Gate-A pause,
contract amendment, red acceptance, Gate-B request-changes, Gate-C red).
Until then a gate that cannot advance logs *why* and stays put.  There is no
silent fall-through anywhere in this module — that is the failure mode that
produced the 240-minute advisory spin in the per-issue driver, and the one
thing this file exists to make impossible.

Layering
--------

- :func:`evaluate_gate` is **pure**: gate name + :class:`GateProbes` in, one
  :class:`GateStep` out.  No board, no GitHub, no clock.  Every edge in the
  machine is decided here and nowhere else.
- :func:`probe_milestone` is the I/O half — it turns a fetched
  :class:`~coord.milestone_dispatch.MilestoneContext` plus a board snapshot
  into a :class:`GateProbes`.  Injectable probe callables keep tests off
  ``gh``, mirroring ``coord.milestone_dispatch``'s ``GateAFileExists``.
- :func:`plan_sequence` walks the whole remaining sequence from a record's
  current gate for ``--dry-run``, so the operator sees every gate and what
  would happen at each — not just the next step.
- :func:`coord.serve_app._milestone_gate_tick` is the driver that persists
  the result.  ``work`` is the one gate with a side effect, and it delegates
  to the existing drain (``coord.milestone_dispatch.plan_dispatch`` /
  ``dispatch_entry``) rather than inventing a second dispatch path.

Relationship to ``milestone.auto_dispatch`` (#1929's explicit question)
----------------------------------------------------------------------

They are **mutually exclusive per milestone, gate driving wins.**

``milestone.auto_dispatch`` gates the *legacy standalone drain*
(``_milestone_drain_tick``): a milestone registered by ``coord milestone
dispatch`` whose frontier the daemon re-drains with no gate walk around it.
A milestone with a gate record is driven by ``_milestone_gate_tick`` instead,
which owns the drain as its ``work`` state — so ``_milestone_drain_tick``
**skips** any ``(repo_name, tracking_issue)`` that has a gate record.  Without
that exclusion a milestone sitting at Gate A (contract missing, or a future
sibling's Gate-A pause) could still have its frontier dispatched by the
independently-gated drain path, which is precisely the "two gates disagreeing
about whether work may start" bug.

Gate driving is therefore its **own** opt-in, per milestone, via ``coord
milestone drive`` — not a global config flag.  ``_milestone_gate_tick`` is
consequently *not* gated on ``milestone.auto_dispatch``: an operator who
explicitly asked for one milestone to be gate-driven has already given the
approval that flag exists to represent, and leaving the tick behind it would
mean a ``drive`` that silently does nothing.

Exactly one overseer (#1930, epic #1440 S-2)
---------------------------------------------

#1440's acceptance says the "exactly one overseer" decision must be
structural, not documentary — #1870 is the counter-example this file exists
to not repeat: a second entry point observed stale state and raced the first,
producing a duplicate drive. Applied to a gate-controlled milestone, the
**daemon's gate tick is the sole owner of its ``work`` drain** — every other
entry point that could dispatch the same frontier must refuse or delegate,
never guess-and-race:

- ``coord milestone drive`` **delegates**. It writes a ``GateRecord`` (cold
  start) or re-persists the existing one (resume) but never calls
  :func:`apply_step` and never dispatches — only ``_milestone_gate_tick``
  does either. Running it twice, from two operators or two machines, is
  therefore inert past the first call: same key, same record, no side
  effect. There is nothing here for a second caller to race.
- ``coord milestone dispatch`` (the manual, non-gate CLI) **refuses**. Before
  #1930 it had no idea a gate record existed and would happily dispatch the
  same ready frontier the gate tick's ``work`` state was about to (or had
  just) dispatched — the exact "two gates disagreeing about whether work may
  start" shape #1870 already taught this codebase to fear, just reached from
  the operator's keyboard instead of a second host's timer. See
  ``coord.commands.milestone.milestone_dispatch_cmd``'s gate-record check,
  which runs before the GitHub fetch and before ``--dry-run``/``--next``, so
  the refusal is unconditional and cheap.
- ``_milestone_drain_tick`` (the legacy ``milestone.auto_dispatch`` path)
  **delegates** by skipping any milestone with a gate record — see above.
- A second daemon process ticking the same board is out of scope here: this
  module has no leader-election of its own and assumes the fleet convention
  documented in ``docs/DRIVE_QUEUE.md`` (one daemon owns tick duty) holds for
  the milestone-gate tick too. ``_save_milestone_gate_local``'s whole-record
  upsert is not a compare-and-swap, so two daemons ticking concurrently could
  still interleave writes — that would need a real distributed lock, which is
  a different, larger change than this issue's scope (client vs. daemon
  entry points). Flagged here rather than left for the next reader to
  discover the hard way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from coord.config import Config
    from coord.milestone_dispatch import MilestoneContext
    from coord.models import Board, Repo

__all__ = [
    "GATE_A",
    "WORK",
    "GATE_B",
    "GATE_C",
    "GATE_D",
    "DONE",
    "GATE_SEQUENCE",
    "GATE_LABELS",
    "TERMINAL_GATES",
    "ADVANCE",
    "HOLD",
    "DISPATCH",
    "TERMINAL",
    "GateRecord",
    "GateStep",
    "GateProbes",
    "evaluate_gate",
    "probe_milestone",
    "plan_sequence",
    "apply_step",
    "format_plan",
]


# ── The gates ────────────────────────────────────────────────────────────────
#
# Names are the persisted wire values (they land in board_meta JSON), so
# renaming one is a migration, not a refactor.  ``work`` is deliberately a
# peer *state* in this sequence and not a peer *machine*: the drain is the
# middle of this walk, entered only once Gate A has cleared and exited only
# once every work-order node is terminal.

GATE_A = "gate_a"
WORK = "work"
GATE_B = "gate_b"
GATE_C = "gate_c"
GATE_D = "gate_d"
DONE = "done"

GATE_SEQUENCE: tuple[str, ...] = (GATE_A, WORK, GATE_B, GATE_C, GATE_D, DONE)

GATE_LABELS: dict[str, str] = {
    GATE_A: "Gate A — contract",
    WORK: "work — drain the ready frontier",
    GATE_B: "Gate B — architecture review",
    GATE_C: "Gate C — full acceptance suite",
    GATE_D: "Gate D — ship",
    DONE: "done",
}

#: Gates from which the machine never leaves; reaching one deregisters the
#: milestone from gate driving (the gate-record analogue of
#: ``deregister_milestone_drain``).
TERMINAL_GATES: frozenset[str] = frozenset({DONE})

# Step actions.
ADVANCE = "advance"
HOLD = "hold"
DISPATCH = "dispatch"
TERMINAL = "terminal"


def next_gate(gate: str) -> str | None:
    """The gate that follows *gate* in :data:`GATE_SEQUENCE`, or ``None``.

    ``None`` both for a terminal gate and for an unknown one — callers treat
    "nowhere to go" identically in either case.
    """
    try:
        idx = GATE_SEQUENCE.index(gate)
    except ValueError:
        return None
    if idx + 1 >= len(GATE_SEQUENCE):
        return None
    return GATE_SEQUENCE[idx + 1]


# ── The durable record ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class GateRecord:
    """One driven milestone's persisted position in the gate walk.

    Stored as JSON under the ``milestone_gates`` ``board_meta`` key (see
    :func:`coord.state.list_milestone_gates`), one entry per
    ``(repo_name, tracking_issue)``.  Everything the daemon needs to resume
    without re-deriving history:

    ``gate``
        Which gate the milestone is in *right now*.  A restart reads this and
        resumes there — it never restarts the walk at :data:`GATE_A`, which
        is what "never re-runs a completed gate" means concretely.
    ``entered_at``
        Wall-clock seconds when the milestone entered ``gate``.  Stamped only
        on an actual transition, so it survives arbitrarily many no-op ticks
        and answers "how long has this been stuck".
    ``waiting_on``
        The current hold reason, verbatim from :func:`evaluate_gate`.  Empty
        while a gate is advancing.  This is the field that makes a hold
        *reported* rather than silent.
    ``cleared``
        Every gate this milestone has already left, in order.  The audit
        trail for "did we run Gate B twice"; also what a future sibling reads
        to decide whether a bounce means re-entering a cleared gate.
    """

    repo_name: str
    tracking_issue: int
    gate: str = GATE_A
    entered_at: float = 0.0
    updated_at: float = 0.0
    waiting_on: str = ""
    milestone_number: int | None = None
    cleared: tuple[str, ...] = field(default_factory=tuple)
    #: Bumped when the persisted shape changes incompatibly.  A record whose
    #: schema this build doesn't understand is treated as absent (cold start)
    #: rather than mis-parsed — see :meth:`from_dict`.
    schema: int = 1

    @property
    def key(self) -> tuple[str, int]:
        return (self.repo_name, self.tracking_issue)

    @property
    def label(self) -> str:
        return f"{self.repo_name}#{self.tracking_issue}"

    @property
    def is_terminal(self) -> bool:
        return self.gate in TERMINAL_GATES

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "repo_name": self.repo_name,
            "tracking_issue": self.tracking_issue,
            "gate": self.gate,
            "entered_at": self.entered_at,
            "updated_at": self.updated_at,
            "waiting_on": self.waiting_on,
            "milestone_number": self.milestone_number,
            "cleared": list(self.cleared),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "GateRecord | None":
        """Rebuild a record from persisted JSON, or ``None`` if unusable.

        Tolerant on purpose, in the same spirit as
        ``coord.state._load_milestone_drains_raw``: one corrupt entry must
        degrade to "this milestone isn't gate-driven" rather than take down
        the whole tick for every other milestone.  An unknown ``gate`` value
        is rejected too — silently coercing it to Gate A would re-run
        already-cleared gates, the exact thing this record exists to prevent.
        """
        if not isinstance(raw, dict):
            return None
        try:
            if int(raw.get("schema", 1)) != 1:
                return None
            repo_name = raw["repo_name"]
            tracking_issue = int(raw["tracking_issue"])
        except (KeyError, TypeError, ValueError):
            return None
        if not isinstance(repo_name, str) or not repo_name:
            return None

        gate = raw.get("gate", GATE_A)
        if gate not in GATE_SEQUENCE:
            return None

        milestone_number = raw.get("milestone_number")
        if milestone_number is not None:
            try:
                milestone_number = int(milestone_number)
            except (TypeError, ValueError):
                milestone_number = None

        cleared_raw = raw.get("cleared") or []
        cleared = tuple(
            g for g in cleared_raw if isinstance(g, str) and g in GATE_SEQUENCE
        ) if isinstance(cleared_raw, list) else ()

        def _f(key: str) -> float:
            try:
                return float(raw.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        waiting_on = raw.get("waiting_on") or ""
        return cls(
            repo_name=repo_name,
            tracking_issue=tracking_issue,
            gate=gate,
            entered_at=_f("entered_at"),
            updated_at=_f("updated_at"),
            waiting_on=waiting_on if isinstance(waiting_on, str) else "",
            milestone_number=milestone_number,
            cleared=cleared,
        )


# ── The transition function ──────────────────────────────────────────────────


@dataclass(frozen=True)
class GateStep:
    """What the machine decided for one gate.

    ``action`` is one of :data:`ADVANCE`, :data:`HOLD`, :data:`DISPATCH`
    (``work`` only — drain the frontier and stay put) or :data:`TERMINAL`.
    ``reason`` is always populated, including on an advance, so a log line or
    a ``--dry-run`` row never has to say just "held" with no explanation.
    """

    gate: str
    action: str
    reason: str
    to_gate: str | None = None
    #: True when this step is a *projection* rather than a live decision —
    #: set by :func:`plan_sequence` for gates downstream of the first hold,
    #: whose real inputs cannot exist yet.
    projected: bool = False

    @property
    def advances(self) -> bool:
        return self.action == ADVANCE


@dataclass(frozen=True)
class GateProbes:
    """The live inputs every gate edge is decided from.

    Split out from :func:`evaluate_gate` so the machine itself is pure and a
    test can drive any edge by constructing this directly — no GitHub, no
    board, no ``gh``.
    """

    #: ``coord.milestone_dispatch.gate_a_status`` — a human-readable block
    #: reason, or ``None`` when the contract exists (or the repo is outside
    #: the oracle-loop model entirely and skips Gate A).
    gate_a_blocked: str | None = None
    #: ``coord.milestone_dispatch.is_milestone_complete`` — every work-order
    #: node terminal.
    work_complete: bool = False
    #: How many work-order nodes are still not terminal (display only).
    work_remaining: int = 0
    #: ``coord.gate_b.latest_gate_b_verdict`` — ``"approve"``,
    #: ``"request-changes"``, or ``None`` when no Gate B review has landed.
    gate_b_verdict: str | None = None
    #: Tri-state on purpose.  ``None`` means *no durable Gate C result
    #: exists* — which is today's reality: ``coord milestone gate-c`` is a
    #: read-only operator run that records nothing on the board.  Making it
    #: durable is a #1440 sibling; until then Gate C always holds on ``None``
    #: rather than guessing.
    gate_c_green: bool | None = None
    #: Whether the milestone has shipped.  Gate D never ships anything from
    #: here (``coord milestone ship`` is an explicit operator action); this
    #: only *observes* that it happened so the walk can terminate.  The
    #: observable signal is the tracking issue's own GitHub state — ``ship``
    #: closes it as the last step of a successful run, so this is true iff
    #: that closure (by ``ship`` or otherwise) has landed.  See
    #: :func:`probe_milestone`.
    shipped: bool = False
    #: Whether the work order is empty — a tracking issue with no ``## Work
    #: order`` block can't be driven, and saying so beats holding forever at
    #: ``work`` with an empty frontier.
    work_order_empty: bool = False


def evaluate_gate(gate: str, probes: GateProbes) -> GateStep:
    """Decide what happens at *gate*, given *probes*.  Pure.

    The single place any gate edge is decided.  Every branch below either
    advances or produces an explicit, human-readable hold — there is no
    ``else: pass`` and no path that returns "nothing to say".  When a #1440
    sibling lands a real decision for an edge, it replaces that edge's hold
    here and the rest of the walk is unchanged.
    """
    if gate == GATE_A:
        if probes.work_order_empty:
            return GateStep(
                gate, HOLD,
                "tracking issue has no `## Work order` block — nothing to drive "
                "(write one with `coord milestone write-order`)",
            )
        if probes.gate_a_blocked:
            # SIBLING (#1440, Gate-A pause): today a missing contract is a
            # plain wait-and-retry.  The Gate-A child decides whether to
            # pause the milestone, dispatch the mock author, or escalate.
            return GateStep(gate, HOLD, probes.gate_a_blocked)
        return GateStep(
            gate, ADVANCE,
            "contract present on the default branch — work may dispatch",
            to_gate=WORK,
        )

    if gate == WORK:
        if probes.work_complete:
            return GateStep(
                gate, ADVANCE,
                "every work-order node is terminal", to_gate=GATE_B,
            )
        # The one gate with a side effect.  Staying here IS the drain.
        return GateStep(
            gate, DISPATCH,
            f"{probes.work_remaining} work-order node(s) not terminal — "
            "draining the ready frontier",
        )

    if gate == GATE_B:
        verdict = probes.gate_b_verdict
        if verdict == "approve":
            return GateStep(
                gate, ADVANCE, "Gate B approved", to_gate=GATE_C,
            )
        if verdict == "request-changes":
            # SIBLING (#1440, Gate-B request-changes): bouncing the milestone
            # back to `work` with a rework cohort is that child's decision.
            return GateStep(
                gate, HOLD,
                "Gate B returned request-changes — bounce handling is a "
                "separate #1440 child; holding rather than shipping",
            )
        if verdict:
            return GateStep(
                gate, HOLD,
                f"Gate B verdict {verdict!r} is not an approval — holding",
            )
        return GateStep(
            gate, HOLD,
            "no Gate B verdict yet — run `coord milestone gate-b <repo> "
            "<tracking_issue>`",
        )

    if gate == GATE_C:
        if probes.gate_c_green is True:
            return GateStep(
                gate, ADVANCE,
                "full acceptance suite recorded green", to_gate=GATE_D,
            )
        if probes.gate_c_green is False:
            # SIBLING (#1440, Gate-C red): deciding whether red means bounce,
            # amend the contract, or file a follow-up is that child's job.
            return GateStep(
                gate, HOLD,
                "full acceptance suite is red — red-acceptance handling is a "
                "separate #1440 child; holding",
            )
        return GateStep(
            gate, HOLD,
            "no durable Gate C result exists — `coord milestone gate-c` is "
            "read-only today and records nothing on the board; run it "
            "manually (making it durable is a separate #1440 child)",
        )

    if gate == GATE_D:
        if probes.shipped:
            return GateStep(gate, ADVANCE, "milestone shipped", to_gate=DONE)
        return GateStep(
            gate, HOLD,
            "ship is an explicit operator action — run `coord milestone ship "
            "<repo> <tracking_issue>`, which merges feature/ms-NN into develop "
            "*and then closes the tracking issue on success*; this gate "
            "watches for that closure (not the ship command running) to "
            "detect completion. If the tracking issue was already closed by "
            "some other means, that satisfies this gate too — but a bare "
            "`ship` run without a closed issue never will",
        )

    if gate == DONE:
        return GateStep(
            gate, TERMINAL, "milestone walk complete — deregistering",
        )

    # Unreachable via GateRecord.from_dict (which rejects unknown gates), but
    # an explicit refusal beats falling off the end of the function.
    return GateStep(gate, HOLD, f"unknown gate {gate!r} — holding")


def apply_step(record: GateRecord, step: GateStep, *, now: float | None = None) -> GateRecord:
    """Fold *step* into *record*, returning the record to persist.

    An advance stamps a fresh ``entered_at`` and appends the gate just left
    to ``cleared`` — that append is what "never re-runs a completed gate"
    is written down as.  A hold only refreshes ``waiting_on``/``updated_at``,
    so a milestone stuck for an hour still reports the timestamp it actually
    entered the gate.
    """
    stamp = time.time() if now is None else now
    if step.advances and step.to_gate:
        cleared = record.cleared
        if record.gate not in cleared:
            cleared = (*cleared, record.gate)
        return replace(
            record,
            gate=step.to_gate,
            entered_at=stamp,
            updated_at=stamp,
            waiting_on="",
            cleared=cleared,
        )
    return replace(
        record,
        updated_at=stamp,
        waiting_on="" if step.action in (ADVANCE, DISPATCH) else step.reason,
        entered_at=record.entered_at or stamp,
    )


# ── The I/O half ─────────────────────────────────────────────────────────────

#: ``(repo_cfg, config, milestone_number) -> block reason | None``.  Injected
#: so tests never touch ``gh`` — mirrors ``milestone_dispatch.GateAFileExists``.
GateAProbe = Callable[["Repo", "Config", int], "str | None"]
#: ``(board, repo_name, tracking_issue, milestone_number) -> verdict | None``.
GateBProbe = Callable[["Board", str, int, int], "str | None"]


def probe_milestone(
    ctx: "MilestoneContext",
    board: "Board",
    config: "Config",
    repo_cfg: "Repo",
    *,
    gate_a_probe: GateAProbe | None = None,
    gate_b_probe: GateBProbe | None = None,
) -> GateProbes:
    """Gather every live input :func:`evaluate_gate` needs, in one place.

    Reuses the *existing* gate readers verbatim
    (:func:`coord.milestone_dispatch.gate_a_status`,
    :func:`coord.gate_b.latest_gate_b_verdict`) rather than reimplementing
    them, so the gate walk and the manual CLI commands can never disagree
    about whether a gate is satisfied.

    ``shipped`` is read off the tracking issue's own state: closing the epic
    is the observable end of the walk, and it costs nothing extra — the
    tracking issue is already fetched by
    :func:`~coord.milestone_dispatch.fetch_milestone_context`.  ``coord
    milestone ship`` (``coord/commands/milestone.py``) closes the tracking
    issue itself once the merge succeeds, specifically so this probe has
    something to observe — Gate D does not hold forever waiting on a manual
    close that nothing in the pipeline actually performs.

    This always computes *every* probe, regardless of which gate the
    milestone is currently sitting in — a milestone parked at Gate C still
    pays for a Gate-A contract-file fetch every tick.  That is deliberate,
    not an oversight: :func:`plan_sequence` evaluates every gate from the
    current one through :data:`DONE` against this *same* probe set to build
    the ``--dry-run`` projection, so the downstream probes have to exist
    even when the live gate doesn't need them yet.  Making this lazy would
    mean threading "which gates does the caller actually need" through two
    layers to save one GitHub call per tick — a real but minor cost, not a
    correctness issue.
    """
    from coord.gate_b import latest_gate_b_verdict  # noqa: PLC0415
    from coord.milestone_dispatch import gate_a_status, is_milestone_complete  # noqa: PLC0415

    a_probe = gate_a_probe or (
        lambda r, c, m: gate_a_status(r, c, m)
    )
    b_probe = gate_b_probe or (
        lambda b, rn, ti, mn: latest_gate_b_verdict(b, rn, ti, mn)
    )

    nodes = list(ctx.work_order.nodes)
    remaining = [n for n in nodes if n.issue_number not in ctx.terminal_issues]

    return GateProbes(
        gate_a_blocked=a_probe(repo_cfg, config, ctx.milestone_number),
        work_complete=bool(nodes) and is_milestone_complete(ctx),
        work_remaining=len(remaining),
        gate_b_verdict=b_probe(
            board, repo_cfg.name, ctx.tracking_issue, ctx.milestone_number
        ),
        # Tri-state None — see GateProbes.gate_c_green.  No durable Gate C
        # record exists on the board today, so this is never True/False yet.
        gate_c_green=None,
        shipped=str(ctx.tracking_issue_state or "").upper() == "CLOSED",
        work_order_empty=not nodes,
    )


# ── Dry-run: the whole remaining sequence ────────────────────────────────────


def plan_sequence(record: GateRecord, probes: GateProbes) -> list[GateStep]:
    """The full remaining walk from ``record.gate``, one step per gate.

    Every gate from the current one through :data:`DONE` appears — not just
    the next one — which is what makes ``--dry-run`` reviewable.  Gates after
    the first hold are still evaluated against the *current* probes and
    flagged ``projected=True``: their real inputs cannot exist yet (there is
    no Gate B verdict while work is still draining), so the row shows what
    they would say today, honestly labelled, rather than being omitted.
    """
    try:
        start = GATE_SEQUENCE.index(record.gate)
    except ValueError:
        return [evaluate_gate(record.gate, probes)]

    steps: list[GateStep] = []
    stalled = False
    for gate in GATE_SEQUENCE[start:]:
        step = evaluate_gate(gate, probes)
        steps.append(replace(step, projected=stalled))
        if not step.advances:
            # The walk stops advancing here; everything past this point is a
            # projection, but keep emitting it so the operator sees the shape
            # of the whole milestone.
            stalled = True
    return steps


def format_plan(
    record: GateRecord,
    steps: list[GateStep],
    *,
    to_dispatch: list = (),
    skipped: list = (),
    waiting: list = (),
    deferred: list = (),
    footer: bool = True,
) -> list[str]:
    """Render a ``--dry-run`` report: the gate walk plus the work frontier.

    ``to_dispatch``/``skipped``/``waiting``/``deferred`` come straight off a
    :class:`~coord.milestone_dispatch.MilestonePlan` (the same frontier
    ``coord milestone dispatch --dry-run`` prints), so the operator sees both
    halves of #1440's fourth acceptance bullet — every gate, *and* what would
    dispatch — in one output.  Returns lines rather than echoing so both the
    CLI and a test can consume it.

    ``deferred`` (#2542) is populated only when the caller resolved
    ``oracle_loop=True`` — entries the frontier considers ready but that the
    `work` gate is deliberately holding back this tick so no two
    same-milestone entries start authoring a JIT acceptance slice
    concurrently. Rendered separately from ``skipped`` (no idle machine) so
    the report never implies "we couldn't find a machine" for something
    that's actually being serialized on purpose.

    ``footer`` controls whether the trailing blank line + "(dry run — ...)"
    line is included.  A non-dry-run confirmation (``coord milestone drive``
    without ``--dry-run``) wants the same report *without* that footer, since
    the record was in fact written — pass ``footer=False`` there rather than
    slicing the returned list, which silently breaks if this function's own
    footer shape ever changes.
    """
    lines: list[str] = [
        f"Gate walk for {record.label}"
        + (
            f" (milestone #{record.milestone_number})"
            if record.milestone_number is not None
            else ""
        ),
        f"  current gate: {GATE_LABELS.get(record.gate, record.gate)}",
    ]
    if record.cleared:
        lines.append(
            "  already cleared: "
            + ", ".join(GATE_LABELS.get(g, g) for g in record.cleared)
        )
    lines.append("")
    lines.append("Planned sequence:")

    marks = {ADVANCE: "->", HOLD: "||", DISPATCH: "**", TERMINAL: "..."}
    for step in steps:
        mark = marks.get(step.action, "  ")
        suffix = "   [projected]" if step.projected else ""
        lines.append(
            f"  {mark} {GATE_LABELS.get(step.gate, step.gate)}: "
            f"{step.action.upper()} — {step.reason}{suffix}"
        )

    lines.append("")
    lines.append("Work-order frontier (what the `work` gate would dispatch):")
    if to_dispatch:
        for pick in to_dispatch:
            group = getattr(pick.entry, "group", None)
            grp = f"  (group {group})" if group else ""
            lines.append(
                f"  would dispatch #{pick.entry.issue_number} -> "
                f"{pick.machine.name}{grp}"
            )
    else:
        lines.append("  (nothing ready to dispatch right now)")

    if skipped:
        lines.append("  Ready but no idle machine:")
        for s in skipped:
            lines.append(f"    #{s.entry.issue_number}: {s.reason}")
    if waiting:
        lines.append("  Waiting on declared-order dependencies:")
        for b in waiting:
            lines.append(f"    #{b.issue_number}: {b.reason}")
    if deferred:
        lines.append("  Deferred (oracle-loop: one entry per tick):")
        for d in deferred:
            lines.append(f"    #{d.entry.issue_number}: {d.reason}")

    if footer:
        lines.append("")
        lines.append("(dry run — nothing dispatched, no gate record written)")
    return lines
