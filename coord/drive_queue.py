"""Pure decision half of the ``coord drive`` queue (#1754, DQ-2).

Phase B of #1750.  DQ-1 gave the queue a home (``drive_queue``, one row per
``(repo, issue)``, dense 0-based ``position``, an ``after_json`` pre-req list
nothing interpreted yet).  This module is what interprets it: given the queue
rows, a typed projection of the board, and a concurrency ceiling, it returns a
:class:`TickPlan` — what to reconcile, the ONE entry to launch, what to block,
what merely deferred and why, and the single queue-level alert.

STRUCTURE — the split is copied verbatim from ``coord/drive.py``, for the
reason that module's docstring gives: *"every bug the bash version shipped was
in the decision half, which is why that half is where the tests are."*  Nothing
in this file runs a subprocess, opens a socket, touches the DB, or reads the
clock.  ``coord/commands/drive_queue.py`` is the thin I/O shell that fetches,
calls :func:`plan_tick`, and executes what comes back.  #1794 needs wall-clock
age, so the clock is *passed in* (``plan_tick(..., now=time.time())``) rather
than read here — the rule is "no ambient state", not "no time".

TWO RULES THIS FILE EXISTS TO ENFORCE, both learned the hard way:

1. **Capacity comes from BOARD STATE, not from a session count.**  ``coord
   drive`` returns ``EXIT_DEADLINE`` (3) when the *observer* gives up; the
   worker, test and review keep running on the fleet (#1660).  Such a drive is
   invisible to ``coord drive-sessions`` but is still occupying a machine.  So
   an entry occupies capacity when its tmux session is alive **or** it still
   has a live work-like assignment on the board — see
   :func:`_reconcile_running`.  Getting this wrong reproduces the 2026-08-01
   incident where a sequential batch became concurrent on the fleet.

2. **Typed state, never CLI prose** (#1523 §2).  Everything here reads dicts
   that came off ``GET /board`` and ``coord drive-sessions --json``.  Both bugs
   in the ad-hoc overnight sequencer were prose-parsing and both failed
   *silently*.

DELIBERATELY NOT HERE: auto-demotion.  A deferral increments a counter and
records a reason; it never reorders the queue (see #1750's design note).  The
head of the queue stays the head until an operator moves it.

#1757 (DEPLOY GATES) adds a third rule: **merged is not live.**  An entry may
be marked ``--hold-after``, and when the tick transitions THAT entry to
``done`` the queue stops launching — even with free capacity and a fully
eligible successor — until a human deploys and releases it.  That is not a
niche case; it is the shape of every change here that crosses a deploy lane
(``docs/OPERATING_GOTCHAS.md`` opens with the matrix).  A queue that models
merge but not deploy would confidently sequence work into that trap overnight.
The gate's decision half is :func:`plan_tick`'s hold resolution below; running
the optional ``resume_when`` probe is the shell's job, and its result comes
back in as data (:class:`ProbeResult`) so this file stays pure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from coord.drive_state import TERMINAL_STATUSES, WORK_LIKE

# ── queue states ─────────────────────────────────────────────────────────────
#
# `waiting` and `running` are the live states; `done`/`blocked`/`failed` are
# terminal and stay in the table until an operator removes them, so
# `coord drive-queue list` doubles as a short run history (coord/db.py's
# drive_queue comment states that contract).

STATE_WAITING = "waiting"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_BLOCKED = "blocked"
STATE_FAILED = "failed"

TERMINAL_QUEUE_STATES: frozenset[str] = frozenset(
    {STATE_DONE, STATE_BLOCKED, STATE_FAILED}
)

# ── deploy-gate states (#1757) ───────────────────────────────────────────────
#
# `hold_state` is the gate's LIFECYCLE, orthogonal to the entry's queue
# `state`.  A gate is `armed` from the moment the operator declares it
# (`coord drive-queue add --hold-after`, written by `enqueue_drive_queue`),
# `fired` the tick the entry reaches `done`, and `released` once a human ran
# `coord drive-queue resume` or the entry's `resume_when` probe exited 0.
# `''` means the entry carries no gate at all.
#
# The queue is held for exactly as long as SOME entry sits at `fired` — the
# release, not the entry leaving the queue, is what unblocks the successors.
HOLD_NONE = ""
HOLD_ARMED = "armed"
HOLD_FIRED = "fired"
HOLD_RELEASED = "released"

# Wall-clock ceiling for one `resume_when` run.  The shell enforces it; it
# lives here so the CLI's help text, the alert prose and the test all quote one
# number.  A wedged probe must never wedge the tick (a tick that stops running
# is indistinguishable from a queue with nothing to do — #1616's lesson).
RESUME_PROBE_TIMEOUT_SECONDS = 5.0

# Launch attempts a single entry gets before it is blocked and escalated.  An
# attempt is only consumed when a launched drive DIED without landing the work
# — a deferral (pre-req not satisfied yet) never touches it, and neither does
# an unsatisfiable pre-req.
DEFAULT_MAX_ATTEMPTS = 2

# ── the startup grace window (#1794) ─────────────────────────────────────────
#
# A drive is NOT established the instant `coord drive --tmux` exits 0.  #1606's
# verification proves a tmux session exists and its run log has been written
# to; it does NOT prove the drive has registered anywhere the tick can see it.
# Between the launch and the first dispatch there is a window in which the
# entry has:
#
#   * no live session in `board.live_sessions` — that snapshot is a
#     `tmux list-sessions` reading, and `list_drive_sessions()` returns `[]`
#     for "tmux unavailable" / "no server running" / "the call timed out"
#     exactly as it does for "no sessions", so one bad reading makes EVERY
#     running entry look dead at once;
#   * no `active_work` on the board — the drive has not dispatched yet.
#
# Before #1794 that fell straight through all three non-death branches of
# `_reconcile_running` into `retry`.  On 2026-08-03 a tick 40s after a launch
# declared a healthy drive dead, spent an attempt, and launched a SECOND
# `coord drive` for the same issue.  Left alone that walks the entry to
# `attempts=2/2` and `blocked`, i.e. an unattended queue parks healthy work and
# reports it as failed.  The two ticks were 40s apart because DRIVE_QUEUE.md §2's
# install sequence is `systemctl --user enable --now …timer` immediately
# followed by a verification `systemctl --user start …service` — i.e. the
# documented install reliably produces the back-to-back ticks that trigger it.
#
# So an entry launched within this window is `starting`, not dead: it OCCUPIES
# capacity and is never a retry candidate.  The measured startup on a loaded
# dellserver was ~2 minutes (19:13:09 launch → 19:15:22 `drive loop started`),
# and this is 5 — deliberately >2x that, and still well under the timer's
# 15-minute cadence so a genuinely dead drive is only ever delayed by ONE
# interval before the retry path sees it.
#
# The window is also applied to the LAUNCH decision (see `_startup_cooldown`),
# so no code path in the tick — not a retry, not a hand-edited row — can start
# a second `coord drive` for an issue whose last launch is this recent.
# `coord drive`'s per-issue flock stays the last line of defence; the queue no
# longer relies on it.
DRIVE_STARTUP_GRACE_SECONDS = 300.0

# ── the queue-level alert's synthetic escalation key ─────────────────────────
#
# #1754 asks for "one queue-level record per tick, written through the DQ-1
# seam OR `record_drive_escalation` with a synthetic issue key — pick one and
# state it in the code comment, don't leave both live".
#
# CHOSEN: `record_drive_escalation` under the synthetic key below.  Reasons:
# the alert is exactly the shape `drive_escalations` already stores (stage +
# reason + gate readings + a proposed command), that table's UNIQUE(repo_name,
# issue_number) + ON CONFLICT DO UPDATE gives "exactly one record, replaced
# each tick" for free, and `coord escalate list` / the TUI's escalation
# plumbing pick it up with no new wire type.  The alternative — a synthetic
# `drive_queue` row — would have to be filtered out of `list`, `move`,
# `plan_tick`, and the dense-position renumbering, i.e. a special case in
# every function in this file.  The DQ-1 seam stays strictly "real entries".
#
# The repo name is deliberately not a valid coordinator.yml repo, so this row
# can never collide with a real issue's escalation or match a Pipeline row.
QUEUE_ALERT_REPO = "(drive-queue)"
QUEUE_ALERT_ISSUE = 0
QUEUE_ALERT_STAGE = "drive-queue"


class QueueError(ValueError):
    """A queue mutation was refused before it was written.

    Carries a message naming the offending issue and the violated constraint,
    the same posture ``coord milestone write-order`` takes for ``## Work
    order`` (``coord.milestone_order.WorkOrderError``): validate, then write —
    never write and then discover.
    """


# ── keys ─────────────────────────────────────────────────────────────────────


def entry_key(repo: str, issue: int) -> str:
    """The fully-qualified queue/pre-req key for an issue: ``"repo#N"``.

    This is the on-disk form DQ-1 stores in ``after_json`` — one column
    carries a cross-repo queue with no second column.
    """
    return f"{repo}#{int(issue)}"


def parse_key(key: str) -> tuple[str, int] | None:
    """Inverse of :func:`entry_key`; ``None`` when *key* isn't ``repo#N``.

    Splits on the LAST ``#`` so a repo name containing one still parses, and
    requires the tail to be a bare number.
    """
    repo, sep, num = str(key).rpartition("#")
    if not sep or not repo or not num.isdigit():
        return None
    return repo, int(num)


def parse_after_spec(raw: str | Iterable[str], default_repo: str) -> list[str]:
    """Normalise a ``--after`` spec into fully-qualified ``repo#N`` keys.

    Accepts ``N`` or ``REPO#N``, comma-separated, and (for repeatable Click
    options) an iterable of either.  Bare numbers resolve against
    *default_repo* — the queue is usually single-repo, and typing the repo
    name twice is the kind of friction that gets a flag skipped.

    Raises :class:`QueueError` on anything that isn't one of those two forms,
    rather than silently dropping it (a dropped pre-req launches work early,
    which is the whole failure this feature exists to prevent).
    """
    chunks: list[str] = []
    items: Iterable[str] = [raw] if isinstance(raw, str) else raw
    for item in items:
        chunks.extend(str(item).split(","))

    keys: list[str] = []
    for chunk in chunks:
        text = chunk.strip()
        if not text:
            continue
        if text.isdigit():
            keys.append(entry_key(default_repo, int(text)))
            continue
        parsed = parse_key(text.lstrip("#"))
        if parsed is None:
            raise QueueError(
                f"malformed --after entry {text!r} (expected 'N' or 'REPO#N')"
            )
        keys.append(entry_key(*parsed))
    # De-duplicate, preserving declaration order.
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


# ── the queue row ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueueEntry:
    """One ``drive_queue`` row, typed.

    Built from the dicts DQ-1's ``list_drive_queue()`` returns — identical
    whether they came off the local DB or the daemon's ``/drive-queue``, which
    is what lets the whole tick run unchanged on a thin client.
    """

    repo: str
    issue: int
    position: int = 0
    machine: str = ""
    after: tuple[str, ...] = ()
    state: str = STATE_WAITING
    attempts: int = 0
    deferrals: int = 0
    last_reason: str = ""
    session_name: str = ""
    launched_at: float | None = None
    # #1757 deploy gate.  `hold_after`/`hold_reason`/`resume_when` are
    # operator-declared (written by `enqueue`); `hold_state`/`hold_probes` are
    # the tick's run state.
    hold_after: bool = False
    hold_reason: str = ""
    resume_when: str = ""
    hold_state: str = HOLD_NONE
    hold_probes: int = 0

    @property
    def key(self) -> str:
        return entry_key(self.repo, self.issue)

    @property
    def gate_reason(self) -> str:
        """What to tell the operator when this entry's gate fires.

        Never empty: an operator who used ``--hold-after`` without a reason
        still gets a sentence naming the entry, because an alert that says
        only "HELD" is one the operator has to go and reconstruct.
        """
        return self.hold_reason or f"deploy gate declared on {self.key}"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "QueueEntry":
        """Type one raw queue row.

        ``after_json`` arrives as a real list over the wire (DQ-1 put it in
        ``coord.dao._JSON_COLUMNS``) but as a JSON *string* when a caller
        reads the table directly, so both are accepted; anything unparseable
        degrades to ``()`` rather than blowing up the whole tick.
        """
        raw_after: Any = row.get("after_json")
        if isinstance(raw_after, str):
            try:
                raw_after = json.loads(raw_after)
            except (TypeError, ValueError):
                raw_after = []
        if not isinstance(raw_after, list):
            raw_after = []
        launched_at = row.get("launched_at")
        return cls(
            repo=str(row.get("repo_name") or ""),
            issue=int(row.get("issue_number") or 0),
            position=int(row.get("position") or 0),
            machine=str(row.get("machine") or ""),
            after=tuple(str(a) for a in raw_after),
            state=str(row.get("state") or STATE_WAITING),
            attempts=int(row.get("attempts") or 0),
            deferrals=int(row.get("deferrals") or 0),
            last_reason=str(row.get("last_reason") or ""),
            session_name=str(row.get("session_name") or ""),
            launched_at=None if launched_at is None else float(launched_at),
            # SQLite hands `hold_after` back as 0/1; a JSON client may send a
            # real bool.  `bool(...)` accepts both and, for a row written
            # before #1757's migration ran, an absent key reads as False —
            # i.e. no gate, which is the pre-#1757 behaviour exactly.
            hold_after=bool(row.get("hold_after") or 0),
            hold_reason=str(row.get("hold_reason") or ""),
            resume_when=str(row.get("resume_when") or ""),
            hold_state=str(row.get("hold_state") or HOLD_NONE),
            hold_probes=int(row.get("hold_probes") or 0),
        )


def entries_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[QueueEntry]:
    """Type a whole queue read, in ``position`` order."""
    return sorted(
        (QueueEntry.from_row(r) for r in rows), key=lambda e: (e.position, e.key)
    )


# ── the board projection ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class IssueFacts:
    """Everything the tick needs to know about one issue, and nothing else.

    All four fields come off ``GET /board`` — no ``gh`` call, no CLI prose.
    ``known=False`` means the board has never heard of this issue at all
    (unsynced, or a typo'd number), which is deliberately NOT the same as
    "open": an unknown pre-req is unsatisfiable, an open one merely defers.
    """

    known: bool = False
    issue_state: str = ""  # "open" / "closed" / "" when the board has no row
    merged: bool = False  # a work-like assignment with status == 'merged'
    active_work: bool = False  # a NON-terminal work-like assignment

    @property
    def open(self) -> bool:
        return self.issue_state == "open"

    @property
    def closed(self) -> bool:
        return self.issue_state == "closed"

    @property
    def landed(self) -> bool:
        """The work is done, by either witness.

        Both are checked because #611 leaves merged work with ``branch=None``
        rows the merge projection can miss, and quadraui-style repos can merge
        a PR into ``develop`` while the linked issue stays open — so neither
        signal alone is reliable.
        """
        return self.merged or self.closed


@dataclass(frozen=True)
class BoardView:
    """The whole board reduced to per-issue facts plus live drive sessions."""

    issues: Mapping[str, IssueFacts] = field(default_factory=dict)
    live_sessions: frozenset[str] = frozenset()

    def facts(self, key: str) -> IssueFacts:
        return self.issues.get(key, IssueFacts())


def build_board_view(
    payload: Mapping[str, Any],
    live_sessions: Iterable[Mapping[str, Any] | str] = (),
) -> BoardView:
    """Reduce a ``/board`` payload + ``drive-sessions --json`` to a :class:`BoardView`.

    Pure: *payload* is whatever ``coord.drive_state.BoardFetcher.fetch()``
    returned and *live_sessions* is whatever ``coord.drive.list_drive_sessions()``
    returned (dicts with ``repo``/``issue``), or a plain iterable of
    ``"repo#N"`` keys for tests.
    """
    facts: dict[str, dict[str, Any]] = {}

    def slot(key: str) -> dict[str, Any]:
        return facts.setdefault(
            key,
            {"known": True, "issue_state": "", "merged": False, "active_work": False},
        )

    for row in payload.get("assignments") or []:
        if (row.get("type") or "") not in WORK_LIKE:
            continue
        repo = row.get("repo_name") or ""
        number = row.get("issue_number")
        if not repo or number is None:
            continue
        entry = slot(entry_key(repo, int(number)))
        status = row.get("status") or ""
        if status == "merged":
            entry["merged"] = True
        if status not in TERMINAL_STATUSES:
            entry["active_work"] = True

    for row in payload.get("issues") or []:
        repo = row.get("repo_name") or ""
        number = row.get("number")
        if not repo or number is None:
            continue
        entry = slot(entry_key(repo, int(number)))
        entry["issue_state"] = str(row.get("state") or "").lower()

    sessions: set[str] = set()
    for item in live_sessions:
        if isinstance(item, str):
            sessions.add(item)
            continue
        repo = item.get("repo") or ""
        number = item.get("issue")
        if repo and number is not None:
            sessions.add(entry_key(repo, int(number)))

    return BoardView(
        issues={key: IssueFacts(**value) for key, value in facts.items()},
        live_sessions=frozenset(sessions),
    )


# ── the plan ─────────────────────────────────────────────────────────────────
#
# Every item carries an explicit `updates` mapping of DQ-1-whitelisted columns
# (see `_DRIVE_QUEUE_UPDATABLE` in coord/state.py).  The shell's apply loop is
# therefore a single uniform `update_drive_queue_entry(repo, issue, **updates)`
# per item — it never re-derives a decision, and a plan with no updates is
# provably a no-op, which is what makes `--dry-run` trustworthy.


@dataclass(frozen=True)
class Reconcile:
    """The resolved outcome for one ``running`` entry.

    ``outcome`` is one of:

    * ``alive``     — a live ``coord-drive-*`` tmux session.  Occupies.
    * ``starting``  — launched inside :data:`DRIVE_STARTUP_GRACE_SECONDS` and
      not yet visible anywhere else (#1794).  Occupies; never a death.
    * ``held``      — session gone but work still ACTIVE on the board (the
      #1660 observer-deadline case).  Occupies; never a death.
    * ``done``      — merged, or the issue closed.
    * ``retry``     — genuinely dead: no session, no active work, and past the
      startup grace window.  Costs one attempt.
    * ``exhausted`` — as ``retry``, but out of attempts; pairs with a
      :class:`Blocked`.
    """

    key: str
    outcome: str  # alive | starting | held | done | retry | exhausted
    reason: str
    occupies: bool = False
    updates: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Blocked:
    """An entry to mark ``blocked`` and escalate."""

    key: str
    reason: str
    updates: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Deferral:
    """An entry whose pre-reqs aren't satisfied YET.  Position unchanged.

    ``counted=False`` marks a REPORT-ONLY deferral: an entry the walk reached
    only after a launch had already been chosen, so it never actually
    competed for a free slot.  Its ``updates`` are empty, so it mutates
    nothing — it exists purely so ``--dry-run`` can answer "and why isn't the
    rest of the queue going?" in the same breath.  Counting it would ruin the
    signal ``deferrals`` carries: *how many times this entry was passed over
    while a slot was actually available*.
    """

    key: str
    reason: str
    updates: Mapping[str, Any] = field(default_factory=dict)
    counted: bool = True


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of ONE ``resume_when`` run, handed back in by the shell.

    Exit 0 (``ok=True``) releases the gate; anything else — non-zero, a
    timeout, or a command that could not be spawned at all — keeps it held.
    Fail-CLOSED is the only safe default here: a gate that releases because
    its probe blew up is a gate that did not exist.
    """

    key: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Hold:
    """One entry's deploy gate, as resolved by this tick (#1757).

    ``outcome``:

    * ``fired``    — the entry reached ``done`` on THIS tick and the gate has
      just closed the queue.  ``updates`` arms the run state.
    * ``held``     — the gate was already ``fired`` and is still closed
      (either no probe is declared, or the probe ran and failed).
    * ``released`` — the probe exited 0; the walk continues in this same tick.

    ``blocking`` is the single thing the tick acts on, so a future outcome
    can be added without every caller re-deriving the rule.
    """

    key: str
    outcome: str
    reason: str
    resume_when: str = ""
    probes: int = 0
    probe_detail: str = ""
    updates: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.outcome in ("fired", "held")


@dataclass(frozen=True)
class QueueAlert:
    """The one queue-level record a tick may raise (see QUEUE_ALERT_REPO).

    ``command`` is the proposed fix written into the escalation record. It is
    carried here rather than derived in the shell so the alert's prose and its
    one-key remedy are decided together — a "HELD" alert whose command says
    ``coord drive-queue list`` teaches the operator to ignore the field.
    """

    reason: str
    details: tuple[str, ...] = ()
    command: str = "coord drive-queue list"


@dataclass(frozen=True)
class TickPlan:
    """Everything one tick decided, and nothing it has done yet."""

    reconciles: tuple[Reconcile, ...] = ()
    launch: QueueEntry | None = None
    blocked: tuple[Blocked, ...] = ()
    deferrals: tuple[Deferral, ...] = ()
    holds: tuple[Hold, ...] = ()
    alert: QueueAlert | None = None
    occupied: int = 0
    capacity: int = 0

    @property
    def free_slots(self) -> int:
        return max(0, self.capacity - self.occupied)

    @property
    def held(self) -> Hold | None:
        """The gate holding the queue shut, if any (lowest position wins)."""
        for item in self.holds:
            if item.blocking:
                return item
        return None

    def writes(self) -> list[tuple[str, Mapping[str, Any]]]:
        """``(key, updates)`` for every row this plan mutates, in apply order.

        The launch is NOT here: its row is written by the shell only after
        ``coord drive --tmux`` has confirmed a live session, so a launch that
        dies immediately is recorded as a failed attempt rather than as a
        running entry (#1606 makes that exit code trustworthy).

        Holds come straight after reconciles: the reconcile that moved an
        entry to ``done`` and the hold that fires off it touch the same row,
        and the gate's run state must land after the state that triggered it.
        """
        out: list[tuple[str, Mapping[str, Any]]] = []
        for item in (*self.reconciles, *self.holds, *self.blocked, *self.deferrals):
            if item.updates:
                out.append((item.key, dict(item.updates)))
        return out


# ── cycle detection ──────────────────────────────────────────────────────────


def find_cycle(edges: Mapping[str, Sequence[str]]) -> list[str] | None:
    """Return one cycle in *edges* (``key -> pre-req keys``), or ``None``.

    Same three-colour DFS as ``coord.milestone_order._check_cycles`` — the
    validation posture ``coord milestone write-order`` applies to ``## Work
    order``, applied to the same shape of graph.  Edges pointing outside
    *edges* (a pre-req that isn't itself queued) are ignored: they cannot
    close a loop.
    """
    white, gray, black = 0, 1, 2
    color = {key: white for key in edges}

    def visit(node: str, path: list[str]) -> list[str] | None:
        color[node] = gray
        path.append(node)
        for dep in edges.get(node, ()):  # noqa: SIM118 — Mapping, not dict
            if dep not in color:
                continue
            if color[dep] == gray:
                return path[path.index(dep):] + [dep]
            if color[dep] == white:
                found = visit(dep, path)
                if found is not None:
                    return found
        path.pop()
        color[node] = black
        return None

    for key in edges:
        if color[key] == white:
            found = visit(key, [])
            if found is not None:
                return found
    return None


def validate_enqueue(
    entries: Sequence[QueueEntry],
    repo: str,
    issue: int,
    after: Sequence[str],
) -> None:
    """Refuse an ``add`` that would be malformed, BEFORE anything is written.

    Checks, in the order an operator is most likely to hit them: a self-edge,
    then a cycle across the queue as it would look *after* this write (the
    entry being added replaces its own current edges, because ``enqueue``
    upserts).  Raises :class:`QueueError`; the caller writes nothing.

    A pre-req that isn't queued is NOT an error here — the point of `--after`
    is often "run this after that other thing merges", and that thing may
    never be queued at all.  Whether such an edge is satisfiable is a *tick*
    question (:func:`plan_tick`), answered against the board.
    """
    key = entry_key(repo, issue)
    normalised = [str(a) for a in after]
    if key in normalised:
        raise QueueError(f"{key} cannot depend on itself")

    edges: dict[str, list[str]] = {
        e.key: list(e.after) for e in entries if e.key != key
    }
    edges[key] = normalised
    cycle = find_cycle(edges)
    if cycle is not None:
        raise QueueError("dependency cycle: " + " -> ".join(cycle))


# ── pre-req resolution ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Verdict:
    satisfied: bool
    unsatisfiable: bool = False
    reason: str = ""


def _resolve_prereqs(
    entry: QueueEntry,
    board: BoardView,
    states: Mapping[str, str],
    cycle_keys: Mapping[str, str],
) -> _Verdict:
    """Decide whether *entry* may launch now.

    Three outcomes, and the difference between the last two is the whole
    point: an *unsatisfied* pre-req will plausibly clear on a later tick, so
    the entry defers and keeps its position; an *unsatisfiable* one never
    will, so waiting forever is the silent-stall failure mode this feature
    exists to remove — it blocks and escalates instead.
    """
    if entry.key in cycle_keys:
        return _Verdict(False, True, cycle_keys[entry.key])

    for dep in entry.after:
        facts = board.facts(dep)
        if facts.landed:
            continue
        dep_state = states.get(dep)
        if dep_state is not None:
            if dep_state in (STATE_BLOCKED, STATE_FAILED):
                return _Verdict(
                    False,
                    True,
                    f"pre-req {dep} is queued but {dep_state} — it will never satisfy",
                )
            return _Verdict(
                False, False, f"waiting on {dep} (queued, {dep_state})"
            )
        if facts.open:
            return _Verdict(
                False, False, f"waiting on {dep} (open, not queued)"
            )
        if facts.active_work:
            # No `issues` row (the standalone `serialize_board` payload ships
            # assignments only) but live work-like assignment rows — the issue
            # is demonstrably in flight, so this defers rather than blocking.
            return _Verdict(
                False, False, f"waiting on {dep} (work in flight, not queued)"
            )
        return _Verdict(
            False,
            True,
            f"pre-req {dep} is not queued, not merged and not open on the board "
            f"(unknown issue, or the board has not synced it — try `coord sync`)",
        )
    return _Verdict(True)


# ── reconciliation ───────────────────────────────────────────────────────────


def _startup_age(entry: QueueEntry, now: float | None) -> float | None:
    """Seconds since *entry*'s drive was launched, or ``None`` when unknowable.

    ``None`` — meaning "no startup grace applies" — for three distinct cases,
    all of which must degrade to the pre-#1794 behaviour rather than to an
    entry that can never be retried:

    * the caller passed no clock (``now is None``): a pure-logic caller that
      does not care about the window, e.g. a test pinning pre-req resolution;
    * the row has no ``launched_at``: a row written before DQ-1 shipped the
      column, or one a human flipped to ``running`` by hand;
    * the stamp is in the FUTURE (negative age): a clock that jumped backwards
      must not be able to pin an entry inside the grace window indefinitely.
    """
    if now is None or entry.launched_at is None:
        return None
    age = now - entry.launched_at
    return age if age >= 0.0 else None


def _startup_cooldown(
    entry: QueueEntry, now: float | None, grace_seconds: float
) -> float | None:
    """The entry's age when it is still inside the startup window, else ``None``.

    The age is returned (rather than a bare bool) so every caller can put the
    real number in its reason string — a journal line that says "launched 41s
    ago" is diagnosable; one that says "still starting" is not.
    """
    age = _startup_age(entry, now)
    if age is None or age >= grace_seconds:
        return None
    return age


def _reconcile_running(
    entry: QueueEntry,
    board: BoardView,
    max_attempts: int,
    *,
    now: float | None = None,
    grace_seconds: float = DRIVE_STARTUP_GRACE_SECONDS,
) -> tuple[Reconcile, Blocked | None]:
    """Resolve one ``running`` entry against the board.

    The branch ORDER is the contract; each non-death branch exists because a
    real incident proved the fall-through to ``retry`` was wrong:

    * ``held`` is rule 1 from this module's docstring and the reason capacity
      is not a session count: ``coord drive`` exits ``EXIT_DEADLINE`` (3) when
      the observer's budget runs out, but the worker/test/review it was
      watching keep running on the fleet (#1660).  Such an entry has no tmux
      session and no merge yet — counting it as free is exactly the 2026-08-01
      incident, where five expired drives were each stacked on top of.
    * ``starting`` is #1794: a drive that has been launched but has not yet
      registered a session reading OR put work on the board is not dead, it is
      young.  See :data:`DRIVE_STARTUP_GRACE_SECONDS`.

    ``retry`` is therefore reachable only when the session is absent, no work
    is active, nothing landed, AND the launch is older than the grace window —
    i.e. when death is the only remaining explanation.
    """
    facts = board.facts(entry.key)

    if entry.key in board.live_sessions:
        return (
            Reconcile(entry.key, "alive", "drive session is live", occupies=True),
            None,
        )

    if facts.landed:
        witness = "merged" if facts.merged else "issue closed"
        return (
            Reconcile(
                entry.key,
                "done",
                f"drive finished ({witness})",
                occupies=False,
                updates={
                    "state": STATE_DONE,
                    "last_reason": f"done ({witness})",
                    "session_name": None,
                },
            ),
            None,
        )

    if facts.active_work:
        return (
            Reconcile(
                entry.key,
                "held",
                "drive session is gone but work is still ACTIVE on the board "
                "(observer deadline, #1660) — still occupying a machine",
                occupies=True,
                updates={
                    "last_reason": "session gone, work still active on the board",
                },
            ),
            None,
        )

    age = _startup_cooldown(entry, now, grace_seconds)
    if age is not None:
        # #1794.  Launched, but not yet visible as a session and not yet
        # visible as work.  A tick that fires inside this window sees exactly
        # what a dead drive looks like, so it must not be allowed to conclude
        # anything: the entry keeps its state, keeps its attempts, and keeps
        # its slot.
        reason = (
            f"drive is still starting — launched {age:.0f}s ago, inside the "
            f"{grace_seconds:.0f}s startup grace window (#1794); "
            f"not a death, still occupying a machine"
        )
        return (
            Reconcile(
                entry.key,
                "starting",
                reason,
                occupies=True,
                updates={"last_reason": reason},
            ),
            None,
        )

    # Past the grace window (or with no launch stamp to measure), with no
    # session, no active work and nothing landed: death is the only remaining
    # explanation.  The age is quoted so a journal reader can tell a genuine
    # death from a grace window that was set too short.
    since = _startup_age(entry, now)
    launched = f", launched {since:.0f}s ago" if since is not None else ""

    attempts = entry.attempts + 1
    if attempts < max_attempts:
        reason = (
            f"drive session died without landing the work"
            f"{launched} (attempt {attempts}/{max_attempts}) — requeued at "
            f"position {entry.position}"
        )
        return (
            Reconcile(
                entry.key,
                "retry",
                reason,
                occupies=False,
                updates={
                    "state": STATE_WAITING,
                    "attempts": attempts,
                    "last_reason": reason,
                    "session_name": None,
                },
            ),
            None,
        )

    reason = (
        f"drive session died without landing the work"
        f"{launched} {attempts}/{max_attempts} times — giving up"
    )
    return (
        Reconcile(entry.key, "exhausted", reason, occupies=False),
        Blocked(
            entry.key,
            reason,
            updates={
                "state": STATE_BLOCKED,
                "attempts": attempts,
                "last_reason": reason,
                "session_name": None,
            },
        ),
    )


# ── deploy gates (#1757) ─────────────────────────────────────────────────────


def pending_probe_targets(entries: Sequence[QueueEntry]) -> list[QueueEntry]:
    """Entries whose ``resume_when`` the shell should run BEFORE this tick.

    Only an ALREADY-``fired`` gate is probed: a gate that fires during this
    tick's own reconcile holds unconditionally for one interval, which is the
    issue's rule ("a ``fired`` hold makes each SUBSEQUENT tick run the
    command") and also the honest one — the deploy cannot have happened in the
    microseconds since the merge was observed.

    Pure and position-ordered, so the shell has no decision left to make: it
    runs exactly this list, in this order, and hands the results back to
    :func:`plan_tick`.
    """
    return [
        e
        for e in sorted(entries, key=lambda e: (e.position, e.key))
        if e.hold_state == HOLD_FIRED and e.resume_when
    ]


def fired_holds(entries: Sequence[QueueEntry]) -> list[QueueEntry]:
    """Entries whose gate has fired and is still holding the queue shut.

    What ``coord drive-queue resume`` releases and what ``status`` reports.
    Position-ordered so "the hold" is always the same entry in both.
    """
    return [
        e
        for e in sorted(entries, key=lambda e: (e.position, e.key))
        if e.hold_state == HOLD_FIRED
    ]


def _resolve_holds(
    ordered: Sequence[QueueEntry],
    reconciled_states: Mapping[str, str],
    probes: Mapping[str, ProbeResult],
) -> list[Hold]:
    """Fire / probe / release every gate, in position order.

    *reconciled_states* is each entry's queue state AFTER step 1 of the tick,
    which is what makes "fires on ``done`` only" checkable here: a
    ``--hold-after`` entry that reconciled to ``blocked`` never reaches this
    branch, so it produces the existing escalation and NOT a second alert (the
    issue's explicit rule — two alerts for one condition is how an alert
    channel gets muted).
    """
    holds: list[Hold] = []
    for entry in ordered:
        if not entry.hold_after:
            continue

        # ARMED → FIRED, the tick the entry lands.  Nothing else fires a gate:
        # `blocked`/`failed` already stop the queue through the escalation
        # path, and `waiting`/`running` have not finished anything yet.
        if (
            entry.hold_state == HOLD_ARMED
            and reconciled_states.get(entry.key) == STATE_DONE
        ):
            holds.append(
                Hold(
                    key=entry.key,
                    outcome="fired",
                    reason=entry.gate_reason,
                    resume_when=entry.resume_when,
                    probes=0,
                    updates={"hold_state": HOLD_FIRED, "hold_probes": 0},
                )
            )
            continue

        if entry.hold_state != HOLD_FIRED:
            # `''` (no gate yet armed), `armed` on an entry that has not
            # landed, or `released` — none of which hold anything.
            continue

        probe = probes.get(entry.key)
        if probe is None:
            # No probe declared, or the shell did not run one.  Manual resume
            # only; the count does not move, so a hold that nobody probes
            # never grows a fake attempt number.
            holds.append(
                Hold(
                    key=entry.key,
                    outcome="held",
                    reason=entry.gate_reason,
                    resume_when=entry.resume_when,
                    probes=entry.hold_probes,
                )
            )
            continue

        if probe.ok:
            holds.append(
                Hold(
                    key=entry.key,
                    outcome="released",
                    reason=entry.gate_reason,
                    resume_when=entry.resume_when,
                    probes=entry.hold_probes,
                    probe_detail=probe.detail,
                    updates={"hold_state": HOLD_RELEASED, "hold_probes": 0},
                )
            )
            continue

        attempts = entry.hold_probes + 1
        holds.append(
            Hold(
                key=entry.key,
                outcome="held",
                reason=entry.gate_reason,
                resume_when=entry.resume_when,
                probes=attempts,
                probe_detail=probe.detail,
                updates={"hold_probes": attempts},
            )
        )
    return holds


def _hold_alert(hold: Hold) -> QueueAlert:
    """The one queue-level record a closed gate raises.

    Carries the operator's own ``hold_reason`` verbatim in ``reason`` — that
    string is the entire point of the feature (it is the runbook line for the
    deploy the queue is waiting on), so it must survive into the alert without
    being summarised.
    """
    details = [f"held after {hold.key} — nothing will launch until this is released"]
    if hold.resume_when:
        outcome = (
            f"attempt {hold.probes} failed"
            if hold.probes
            else "not probed yet (fires on the next tick)"
        )
        if hold.probe_detail:
            outcome += f": {hold.probe_detail}"
        details.append(f"resume-when: {hold.resume_when} ({outcome})")
    else:
        details.append("no --resume-when probe: release manually")
    return QueueAlert(
        reason=f"QUEUE HELD — {hold.reason}",
        details=tuple(details),
        command="coord drive-queue resume",
    )


# ── the tick ─────────────────────────────────────────────────────────────────


def plan_tick(
    entries: Sequence[QueueEntry],
    board: BoardView,
    capacity: int,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    probes: Mapping[str, ProbeResult] | None = None,
    now: float | None = None,
    grace_seconds: float = DRIVE_STARTUP_GRACE_SECONDS,
) -> TickPlan:
    """Decide one tick.  Pure; the caller executes the returned plan.

    *capacity* is the CEILING (``--max-parallel``), not the number of free
    slots — how many slots are already occupied is a decision (rule 1 above),
    and decisions live in here, not in the shell.

    *probes* maps an entry key to the :class:`ProbeResult` the shell got from
    running that entry's ``resume_when`` (see :func:`pending_probe_targets`);
    an absent key simply means no probe ran.

    *now* is the shell's ``time.time()``, passed in rather than read here (see
    the module docstring).  It powers #1794's startup grace window on both
    sides of the tick: a recently-launched entry never reconciles to ``retry``
    (:func:`_reconcile_running`), and no entry is relaunched while its last
    launch is still that recent (step 4 below).  ``None`` disables the window
    entirely, which is the pre-#1794 behaviour — the production shell always
    passes a real clock.

    The algorithm, from #1754, plus #1757's step 2:

    1. Reconcile every ``running`` entry (:func:`_reconcile_running`).
    2. Resolve deploy gates (:func:`_resolve_holds`).  ANY gate left closed
       returns immediately with no launch and a HELD alert — before the
       capacity check, and regardless of how eligible the rest of the queue
       is.  That "even with free capacity and an eligible successor" clause is
       the entire feature: the successor is exactly the thing that must not
       run until the deploy lands.
    3. ``free = capacity - occupied``; ``<= 0`` returns with no launch and no
       alert — being at capacity is the queue working, not a problem to
       report.
    4. Walk ``waiting`` by ``position``, FIRST ELIGIBLE WINS: an entry still
       inside its startup grace window defers (#1794), unsatisfiable blocks
       and escalates, unsatisfied defers (position unchanged), the first
       eligible entry is the launch.  Everything after the launch is walked in
       REPORT-ONLY mode (``Deferral.counted=False``, no updates) so
       ``--dry-run`` can explain the rest of the queue.
    5. No launch with at least one waiting entry ⇒ exactly ONE queue-level
       alert.

    An entry reconciled from ``running`` back to ``waiting`` in step 1 IS
    walked in step 4 — its attempt was already consumed, so a drive that died
    early relaunches on the same tick instead of idling a whole interval.  A
    gate RELEASED in step 2 likewise falls straight through into step 4, so a
    probe that starts passing launches in the same tick rather than costing
    the queue a whole interval.

    #1794 puts one bound on that same-tick relaunch, and it is the reason the
    grace window is checked TWICE.  Step 1 can only produce a ``retry`` for an
    entry whose launch is older than *grace_seconds*, so the relaunch is only
    ever of a drive the tick is confident is gone; and step 4 refuses the
    launch outright for anything launched more recently, whatever put it back
    in ``waiting``.  Between them, no single tick can start a second ``coord
    drive`` for an issue whose first one may still be coming up.
    """
    ordered = sorted(entries, key=lambda e: (e.position, e.key))
    states: dict[str, str] = {e.key: e.state for e in ordered}
    by_key = {e.key: e for e in ordered}

    reconciles: list[Reconcile] = []
    blocked: list[Blocked] = []
    deferrals: list[Deferral] = []
    occupied = 0

    for entry in ordered:
        if entry.state != STATE_RUNNING:
            continue
        reconcile, block = _reconcile_running(
            entry, board, max_attempts, now=now, grace_seconds=grace_seconds
        )
        reconciles.append(reconcile)
        if reconcile.occupies:
            occupied += 1
        new_state = reconcile.updates.get("state")
        if new_state:
            states[entry.key] = str(new_state)
        if block is not None:
            blocked.append(block)
            states[entry.key] = STATE_BLOCKED

    # #1757 step 2: deploy gates.  Resolved from the POST-reconcile states, so
    # a `--hold-after` entry that reconciled to `blocked` cannot also fire a
    # gate, and `released` falls through to the walk below in this same tick.
    holds = _resolve_holds(ordered, states, probes or {})

    plan_base = {
        "reconciles": tuple(reconciles),
        "holds": tuple(holds),
        "occupied": occupied,
        "capacity": capacity,
    }

    gate = next((h for h in holds if h.blocking), None)
    if gate is not None:
        # Launch NOTHING.  Not "launch if there is spare capacity", not
        # "launch anything whose pre-reqs don't mention the held entry" — the
        # deploy this gate is waiting on is invisible to the dependency graph,
        # which is exactly why an explicit operator-declared gate exists.
        return TickPlan(
            **plan_base,
            blocked=tuple(blocked),
            deferrals=(),
            alert=_hold_alert(gate),
            launch=None,
        )

    if capacity - occupied <= 0:
        return TickPlan(
            **plan_base, blocked=tuple(blocked), deferrals=(), alert=None, launch=None
        )

    # Cycles are re-checked here, not just at `add` time: `remove` can leave
    # the surviving edges in a shape `add` never validated, and a hand-edited
    # DB row is always possible.  A cycle makes every member unsatisfiable.
    cycle_keys: dict[str, str] = {}
    cycle = find_cycle({e.key: list(e.after) for e in ordered})
    if cycle is not None:
        message = "dependency cycle: " + " -> ".join(cycle)
        for key in cycle:
            cycle_keys[key] = message

    def _cooldown_reason(candidate: QueueEntry) -> str:
        """#1794's launch-side guard: '' unless this entry was just launched.

        A `waiting` row carrying a recent `launched_at` means SOMETHING put a
        drive up for this issue moments ago — a retry decided on stale
        evidence, a launch subprocess whose exit code lied, an operator's hand
        edit.  Whatever it was, starting a second `coord drive` now is the
        failure #1794 exists to prevent, so the entry defers and tries again
        on the next tick, by which point the reconcile branches above have
        real evidence to work with.
        """
        age = _startup_cooldown(candidate, now, grace_seconds)
        if age is None:
            return ""
        return (
            f"launched {age:.0f}s ago — inside the {grace_seconds:.0f}s startup "
            f"grace window, so a second `coord drive` is refused (#1794)"
        )

    launch: QueueEntry | None = None
    waiting = [e for e in ordered if states.get(e.key) == STATE_WAITING]
    for entry in waiting:
        if launch is not None:
            # Report-only pass over the tail of the queue.  The launch above
            # already won this tick, so nothing here is mutated (see
            # Deferral.counted) — this exists so `--dry-run` explains the rest
            # of the queue instead of going silent after the first line.
            cooldown = _cooldown_reason(entry)
            if cooldown:
                deferrals.append(Deferral(entry.key, cooldown, counted=False))
                continue
            verdict = _resolve_prereqs(entry, board, states, cycle_keys)
            if not verdict.satisfied:
                deferrals.append(
                    Deferral(entry.key, verdict.reason, counted=False)
                )
            continue
        cooldown = _cooldown_reason(entry)
        if cooldown:
            deferrals.append(
                Deferral(
                    entry.key,
                    cooldown,
                    updates={
                        "deferrals": entry.deferrals + 1,
                        "last_reason": cooldown,
                    },
                )
            )
            continue
        verdict = _resolve_prereqs(entry, board, states, cycle_keys)
        if verdict.unsatisfiable:
            blocked.append(
                Blocked(
                    entry.key,
                    verdict.reason,
                    # attempts is deliberately NOT incremented: nothing was
                    # ever launched for this entry, so charging it a retry
                    # would be charging it for the operator's typo.
                    updates={
                        "state": STATE_BLOCKED,
                        "last_reason": verdict.reason,
                    },
                )
            )
            states[entry.key] = STATE_BLOCKED
            continue
        if not verdict.satisfied:
            deferrals.append(
                Deferral(
                    entry.key,
                    verdict.reason,
                    updates={
                        "deferrals": entry.deferrals + 1,
                        "last_reason": verdict.reason,
                    },
                )
            )
            continue
        launch = by_key[entry.key]

    alert: QueueAlert | None = None
    if launch is None and waiting:
        details = [f"{item.key}: {item.reason}" for item in deferrals]
        details += [f"{item.key}: BLOCKED — {item.reason}" for item in blocked]
        alert = QueueAlert(
            # "considered N" rather than "N waiting": some of those entries are
            # blocked by the time this line is written, and an alert that
            # contradicts `coord drive-queue status` two lines below it is an
            # alert operators learn to distrust.
            reason=(
                f"nothing eligible to launch: considered {len(waiting)} waiting "
                f"entr{'y' if len(waiting) == 1 else 'ies'}, "
                f"{capacity - occupied} free slot(s)"
            ),
            details=tuple(details),
        )

    return TickPlan(
        **plan_base,
        blocked=tuple(blocked),
        deferrals=tuple(deferrals),
        alert=alert,
        launch=launch,
    )


# ── rendering (pure, so `--dry-run` is testable without a CLI) ───────────────


def render_plan(plan: TickPlan, *, dry_run: bool = False) -> list[str]:
    """The human-readable form of a :class:`TickPlan`, one line per element."""
    prefix = "would " if dry_run else ""
    lines = [
        f"capacity: {plan.occupied}/{plan.capacity} occupied, "
        f"{plan.free_slots} free"
    ]
    for item in plan.reconciles:
        lines.append(f"  reconcile {item.key}: {item.outcome} — {item.reason}")
    # #1757: the gate line goes directly under its reconcile, because "1753
    # done" immediately followed by "and therefore nothing launches" is the
    # sentence an operator reading a timer log needs to read as one thought.
    for item in plan.holds:
        probe = ""
        if item.resume_when:
            probe = f" [resume-when: {item.resume_when}"
            if item.probes:
                probe += f", {item.probes} failed attempt(s)"
            if item.probe_detail:
                probe += f" — {item.probe_detail}"
            probe += "]"
        lines.append(f"  hold {item.key}: {item.outcome} — {item.reason}{probe}")
    for item in plan.blocked:
        lines.append(f"  {prefix}block {item.key}: {item.reason}")
    # Counted deferrals come BEFORE the launch line and report-only ones after,
    # so the output reads in the order the walk actually happened: these lost
    # their turn while a slot was free; that one took it; the rest were never
    # reached.
    for item in plan.deferrals:
        if item.counted:
            lines.append(f"  defer {item.key}: {item.reason}")
    if plan.launch is not None:
        target = plan.launch
        pinned = f" on {target.machine}" if target.machine else ""
        lines.append(f"  {prefix}launch {target.key}{pinned}")
    elif plan.held is not None:
        lines.append(
            f"  no launch — HELD by the deploy gate on {plan.held.key} "
            f"(release with `coord drive-queue resume`)"
        )
    elif plan.capacity and plan.free_slots == 0:
        # Naming the reason matters more here than anywhere else in this
        # render: #1794 was diagnosed entirely from a journal, and "no launch"
        # on its own is indistinguishable from a stalled queue.
        lines.append(
            f"  no launch — at capacity ({plan.occupied}/{plan.capacity} occupied)"
        )
    else:
        lines.append("  no launch")
    for item in plan.deferrals:
        if not item.counted:
            lines.append(
                f"  defer {item.key}: {item.reason} (not reached this tick)"
            )
    if plan.alert is not None:
        lines.append(f"  {prefix}alert: {plan.alert.reason}")
        lines.extend(f"    {detail}" for detail in plan.alert.details)
    return lines
