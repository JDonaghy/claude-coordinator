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
calls :func:`plan_tick`, and executes what comes back.

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

# Launch attempts a single entry gets before it is blocked and escalated.  An
# attempt is only consumed when a launched drive DIED without landing the work
# — a deferral (pre-req not satisfied yet) never touches it, and neither does
# an unsatisfiable pre-req.
DEFAULT_MAX_ATTEMPTS = 2

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

    @property
    def key(self) -> str:
        return entry_key(self.repo, self.issue)

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
    """The resolved outcome for one ``running`` entry."""

    key: str
    outcome: str  # alive | held | done | retry | exhausted
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
class QueueAlert:
    """The one queue-level record a tick may raise (see QUEUE_ALERT_REPO)."""

    reason: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class TickPlan:
    """Everything one tick decided, and nothing it has done yet."""

    reconciles: tuple[Reconcile, ...] = ()
    launch: QueueEntry | None = None
    blocked: tuple[Blocked, ...] = ()
    deferrals: tuple[Deferral, ...] = ()
    alert: QueueAlert | None = None
    occupied: int = 0
    capacity: int = 0

    @property
    def free_slots(self) -> int:
        return max(0, self.capacity - self.occupied)

    def writes(self) -> list[tuple[str, Mapping[str, Any]]]:
        """``(key, updates)`` for every row this plan mutates, in apply order.

        The launch is NOT here: its row is written by the shell only after
        ``coord drive --tmux`` has confirmed a live session, so a launch that
        dies immediately is recorded as a failed attempt rather than as a
        running entry (#1606 makes that exit code trustworthy).
        """
        out: list[tuple[str, Mapping[str, Any]]] = []
        for item in (*self.reconciles, *self.blocked, *self.deferrals):
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


def _reconcile_running(
    entry: QueueEntry, board: BoardView, max_attempts: int
) -> tuple[Reconcile, Blocked | None]:
    """Resolve one ``running`` entry against the board.

    The ``held`` branch is rule 1 from this module's docstring and the reason
    capacity is not a session count: ``coord drive`` exits ``EXIT_DEADLINE``
    (3) when the observer's budget runs out, but the worker/test/review it was
    watching keep running on the fleet (#1660).  Such an entry has no tmux
    session and no merge yet — counting it as free is exactly the 2026-08-01
    incident, where five expired drives were each stacked on top of.
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

    attempts = entry.attempts + 1
    if attempts < max_attempts:
        reason = (
            f"drive session died without landing the work "
            f"(attempt {attempts}/{max_attempts}) — requeued at position "
            f"{entry.position}"
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
        f"drive session died without landing the work "
        f"{attempts}/{max_attempts} times — giving up"
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


# ── the tick ─────────────────────────────────────────────────────────────────


def plan_tick(
    entries: Sequence[QueueEntry],
    board: BoardView,
    capacity: int,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> TickPlan:
    """Decide one tick.  Pure; the caller executes the returned plan.

    *capacity* is the CEILING (``--max-parallel``), not the number of free
    slots — how many slots are already occupied is a decision (rule 1 above),
    and decisions live in here, not in the shell.

    The algorithm, from #1754:

    1. Reconcile every ``running`` entry (:func:`_reconcile_running`).
    2. ``free = capacity - occupied``; ``<= 0`` returns with no launch and no
       alert — being at capacity is the queue working, not a problem to
       report.
    3. Walk ``waiting`` by ``position``, FIRST ELIGIBLE WINS: unsatisfiable
       blocks and escalates, unsatisfied defers (position unchanged), the
       first eligible entry is the launch.  Everything after the launch is
       walked in REPORT-ONLY mode (``Deferral.counted=False``, no updates) so
       ``--dry-run`` can explain the rest of the queue.
    4. No launch with at least one waiting entry ⇒ exactly ONE queue-level
       alert.

    An entry reconciled from ``running`` back to ``waiting`` in step 1 IS
    walked in step 3 — its attempt was already consumed, so a drive that died
    early relaunches on the same tick instead of idling a whole interval.
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
        reconcile, block = _reconcile_running(entry, board, max_attempts)
        reconciles.append(reconcile)
        if reconcile.occupies:
            occupied += 1
        new_state = reconcile.updates.get("state")
        if new_state:
            states[entry.key] = str(new_state)
        if block is not None:
            blocked.append(block)
            states[entry.key] = STATE_BLOCKED

    plan_base = {
        "reconciles": tuple(reconciles),
        "occupied": occupied,
        "capacity": capacity,
    }

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

    launch: QueueEntry | None = None
    waiting = [e for e in ordered if states.get(e.key) == STATE_WAITING]
    for entry in waiting:
        if launch is not None:
            # Report-only pass over the tail of the queue.  The launch above
            # already won this tick, so nothing here is mutated (see
            # Deferral.counted) — this exists so `--dry-run` explains the rest
            # of the queue instead of going silent after the first line.
            verdict = _resolve_prereqs(entry, board, states, cycle_keys)
            if not verdict.satisfied:
                deferrals.append(
                    Deferral(entry.key, verdict.reason, counted=False)
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
