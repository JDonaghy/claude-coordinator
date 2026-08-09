"""Decision half of merge-triggered propagation (#1835, PKG-7).

PKG-7 closes the release loop: **merging a PR to `main` is the only human
action in a release.** The pipeline it creates is deliberately cut in two,
and the cut is the whole design:

* **Publish** — fully automatic, on merge. `.github/workflows/auto-release.yml`
  picks the next `vX.Y.Z` from the tag history and pushes it; `publish.yml`
  (#1242, PKG-6) turns that one tag into one Release carrying the wheel, the
  `coord-tui` binaries and the bundled webapp. Publishing touches no running
  host, so it is safe at any instant.

* **Propagate** — automatic, but scheduled against *fleet quiescence*, never
  against the clock. This module is that scheduler's judgement.

Why the cut is not optional: ``coord agent update`` restarts the agent, and
a restart kills every in-flight headless worker (``coord/agent_app.py``'s
``/update`` refuses outright when assignments are live, for exactly this
reason). With overnight drive queues (#56/#1750) the fleet is rarely idle,
so "on merge, upgrade the fleet" would routinely destroy work — and the
better the queue works, the more it destroys.

QUIESCENCE IS THE DRIVE QUEUE'S, NOT A SECOND OPINION
-----------------------------------------------------
#1835 is explicit that propagation must reuse the drive queue's existing
gate mechanism rather than invent a rival definition of "the fleet is busy"
— two competing definitions of quiescence is the same class of defect as two
overseers driving one milestone (#1440). So :func:`assess_quiescence` reads
exactly the states :mod:`coord.drive_queue` already publishes, and imports
its constants rather than re-spelling them:

* a queue entry in :data:`~coord.drive_queue.STATE_RUNNING` is in-flight work
  → **busy**;
* an agent with live (``RUNNING``/``PENDING``) assignments is in-flight work
  → **busy** (the board is authoritative here for the same reason
  ``coord.drive_queue`` rule 1 gives: a drive whose *observer* gave up leaves
  the worker running and invisible to session counts);
* a **fired** deploy gate (:data:`~coord.drive_queue.HOLD_FIRED`) is
  **not** busy — it is the opposite. `--hold-after` means "this entry landed
  a change that crosses a deploy lane; stop launching until a human deploys
  and releases it" (#1757). The queue has *deliberately stopped*. That is the
  best propagation window there is, and propagation is precisely the deploy
  the gate is waiting for. So a fired hold is an *invitation*, and a verified
  propagation releases it (see :func:`holds_to_release`) — the gate stops the
  queue for the deploy, propagation performs the deploy, propagation restarts
  the queue. One mechanism, one loop, no second notion of quiescence.

LANE ORDER ANSWERS THE SKEW QUESTION
------------------------------------
#1835 asks whether a fleet mid-roll — hosts at two versions — is safe for
the board protocol, and insists on an explicit answer rather than an
assumption. It is safe **in one direction only**, and the direction is
already a documented failure: a *caller* on a new version calling a *daemon*
that predates the endpoint it wants gets a 405. New callers must therefore
never appear before the daemon can serve them.

:func:`plan_lanes` encodes that as a total order rather than leaving it to
whoever wrote the loop:

1. the **daemon host**'s Python lane first — it must lead, always;
2. every **other machine**'s Python lane;
3. each host's **systemd unit** lane (#1831 — `deploy/**` ships in the wheel
   as ``coord/deploy/``, so this lane can only roll *after* that host's venv
   swapped);
4. the **coord-tui** binaries last — a pure client of the board API, so it is
   the one lane that is safe at any skew.

Propagation is therefore explicitly **not** all-or-nothing; it is ordered so
that every intermediate state is one the protocol already tolerates
(old caller → new daemon), and never the one it does not.

PURITY
------
Nothing in this module runs a subprocess, opens a socket, touches the DB or
reads the clock — same split ``coord/drive_queue.py`` documents, for the same
reason: every bug worth catching lives in the decision half. The clock is
passed in. ``coord/commands/release.py`` is the I/O shell that gathers the
facts, calls in here, executes the plan and appends the journal.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from coord.drive_queue import HOLD_FIRED, STATE_RUNNING, entry_key

# ── lane kinds ───────────────────────────────────────────────────────────────
#
# One string per *kind* of thing that has to move for the fleet to reach a
# version. These are the lanes #1835's precondition list names, and the same
# lanes `coord release verify` (#1834) grades afterwards. A release that
# propagates only `python` would have shipped #1543's `--dist` flag and none
# of its behaviour (that change was three unit files and a shell script).
LANE_PYTHON = "python"
LANE_UNITS = "units"
LANE_TUI = "tui"

#: Every lane this module knows how to roll, in no particular order — the
#: *order* is :func:`plan_lanes`'s output, not this tuple.
ALL_LANES: tuple[str, ...] = (LANE_PYTHON, LANE_UNITS, LANE_TUI)

# ── propagation outcomes ─────────────────────────────────────────────────────
#
# The status recorded in the journal for one propagation attempt. #1835:
# "a silent success is indistinguishable from a silent no-op, which is
# precisely how 2026-08-04 stayed invisible" — so *every* attempt appends a
# record, including the boring "deferred, fleet busy" ones. A timer that
# fired forty times and deferred forty times must look different from a
# timer that never fired at all.
STATUS_DEFERRED = "deferred"
STATUS_UP_TO_DATE = "up-to-date"
STATUS_ROLLED = "rolled"
STATUS_VERIFIED = "verified"
STATUS_ROLLED_BACK = "rolled-back"
STATUS_FAILED = "failed"

#: Statuses that mean "this attempt changed nothing on any host". Used by the
#: renderer to keep a long quiet night readable.
NO_OP_STATUSES: frozenset[str] = frozenset({STATUS_DEFERRED, STATUS_UP_TO_DATE})

#: Board assignment statuses that count as live work. Mirrors the set
#: ``coord/agent_app.py``'s ``/update`` refuses on, deliberately: propagation
#: must not schedule an update the agent would then refuse.
LIVE_ASSIGNMENT_STATUSES: frozenset[str] = frozenset({"RUNNING", "PENDING"})


# ── busy signals ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Busy:
    """One concrete reason the fleet is not quiescent right now.

    Named down to the subject so a deferral is actionable prose ("dellserver
    has 2 live assignments") rather than the useless "fleet busy" — a
    deferral nobody can explain is a deferral nobody can distinguish from a
    wedged timer.
    """

    kind: str
    subject: str
    detail: str = ""

    def describe(self) -> str:
        base = f"{self.kind}: {self.subject}"
        return f"{base} ({self.detail})" if self.detail else base


@dataclass(frozen=True)
class Quiescence:
    """Is there a window right now, and if not, why not."""

    quiescent: bool
    busy: tuple[Busy, ...] = ()
    #: Fired deploy gates (#1757) found while assessing. Not busy signals —
    #: see the module docstring. Carried through so the caller can release
    #: them after a verified roll.
    fired_holds: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        if self.quiescent:
            if self.fired_holds:
                return (
                    "quiescent — and "
                    f"{len(self.fired_holds)} deploy gate(s) are waiting on "
                    "exactly this deploy"
                )
            return "quiescent — nothing in flight"
        return "; ".join(b.describe() for b in self.busy) or "busy"

    def to_dict(self) -> dict:
        return {
            "quiescent": self.quiescent,
            "reason": self.reason,
            "busy": [asdict(b) for b in self.busy],
            "fired_holds": list(self.fired_holds),
        }


def _queue_key(entry: Mapping[str, Any]) -> str:
    """``repo#issue`` for a ``drive_queue`` row, however it reached us.

    ``/board`` publishes the sqlite columns verbatim (``repo_name`` /
    ``issue_number``); an already-rendered row may carry ``key``. Both are
    accepted so this never silently degrades to ``"?"`` — a busy signal
    nobody can name is a busy signal nobody can act on, and ``coord
    drive-queue resume`` needs the real key to release the gate.
    """
    key = entry.get("key")
    if key:
        return str(key)
    repo = entry.get("repo_name") or entry.get("repo")
    issue = entry.get("issue_number") or entry.get("issue")
    if repo and issue is not None:
        try:
            return entry_key(str(repo), int(issue))
        except (TypeError, ValueError):
            return f"{repo}#{issue}"
    return "?"


def assess_quiescence(
    *,
    queue_entries: Iterable[Mapping[str, Any]] = (),
    assignments: Iterable[Mapping[str, Any]] = (),
    extra_busy: Iterable[Busy] = (),
) -> Quiescence:
    """Is the fleet idle enough to restart every agent on it?

    *queue_entries* are ``drive_queue`` rows as they come off the board /
    ``coord drive-queue list --json``; *assignments* are board assignment
    rows. Both are read as plain mappings — #1523 §2's "typed state, never
    CLI prose", the rule both bugs in the ad-hoc overnight sequencer broke.

    ``extra_busy`` is the seam for host-local signals the board cannot see
    (an interactive tmux session, a machine paused by an operator); the
    shell passes them in rather than this module growing a way to look.
    """
    busy: list[Busy] = []
    fired: list[str] = []

    for entry in queue_entries:
        state = str(entry.get("state") or "")
        key = _queue_key(entry)
        if state == STATE_RUNNING:
            busy.append(
                Busy(
                    kind="drive-queue entry running",
                    subject=key,
                    detail="restarting agents now would kill it mid-flight",
                )
            )
        # A *fired* gate is the opposite of busy — the queue has stopped
        # itself waiting for precisely this deploy. Recorded, never counted.
        if str(entry.get("hold_state") or "") == HOLD_FIRED:
            fired.append(key)

    for row in assignments:
        status = str(row.get("status") or "").upper()
        if status not in LIVE_ASSIGNMENT_STATUSES:
            continue
        machine = str(row.get("machine_name") or row.get("machine") or "?")
        subject = str(
            row.get("issue_number")
            or row.get("issue")
            or row.get("assignment_id")
            or "?"
        )
        busy.append(
            Busy(
                kind=f"live {status} assignment",
                subject=f"{machine}:{subject}",
                detail="`coord agent update` would refuse this host anyway",
            )
        )

    busy.extend(extra_busy)
    return Quiescence(
        quiescent=not busy, busy=tuple(busy), fired_holds=tuple(dict.fromkeys(fired))
    )


def holds_to_release(quiescence: Quiescence, *, verified: bool) -> tuple[str, ...]:
    """Which deploy gates a finished propagation should release (#1757).

    Only after a **verified** roll. Releasing a gate on an unverified — or
    rolled-back — propagation would restart the overnight queue into exactly
    the "merged is not live" trap the gate exists to prevent, which is the
    single most expensive recurring failure in this fleet.
    """
    return quiescence.fired_holds if verified else ()


# ── the roll plan ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LaneRoll:
    """One unit of propagation work: roll *lane* on *host* to a version."""

    order: int
    lane: str
    host: str
    #: Why this step sits where it does in the order. Rendered in `--dry-run`
    #: and journalled, because the ordering is a protocol-safety argument and
    #: an argument nobody can read is an argument nobody can check.
    rationale: str = ""

    @property
    def label(self) -> str:
        return f"{self.lane}@{self.host}"


def plan_lanes(
    *,
    daemon_host: str | None,
    hosts: Sequence[str],
    lanes: Iterable[str] = ALL_LANES,
    skip_hosts: Iterable[str] = (),
) -> list[LaneRoll]:
    """The total order in which lanes may roll. See the module docstring.

    The invariant this function exists to hold: **the daemon never lags a
    caller.** A host running a newer ``coord`` than the daemon it talks to
    reproduces the documented 405 (caller wants an endpoint the daemon does
    not serve yet); the reverse — a newer daemon serving older callers — is
    the skew the board protocol is built to tolerate, since that is the
    steady state between every release and every fleet update anyway.

    *skip_hosts* drops hosts already on the target version, so a re-run after
    a partial failure resumes rather than restarting.
    """
    wanted = [lane for lane in ALL_LANES if lane in set(lanes)]
    skip = set(skip_hosts)
    ordered_hosts: list[str] = []
    if daemon_host and daemon_host in hosts:
        ordered_hosts.append(daemon_host)
    ordered_hosts.extend(h for h in hosts if h != daemon_host)

    rolls: list[LaneRoll] = []
    order = 0

    if LANE_PYTHON in wanted:
        for host in ordered_hosts:
            if host in skip:
                continue
            first = host == daemon_host
            order += 1
            rolls.append(
                LaneRoll(
                    order=order,
                    lane=LANE_PYTHON,
                    host=host,
                    rationale=(
                        "daemon host leads: a caller must never reach an "
                        "endpoint its daemon predates (the documented 405)"
                        if first
                        else "callers follow the daemon, never lead it"
                    ),
                )
            )

    if LANE_UNITS in wanted:
        for host in ordered_hosts:
            if host in skip:
                continue
            order += 1
            rolls.append(
                LaneRoll(
                    order=order,
                    lane=LANE_UNITS,
                    host=host,
                    rationale=(
                        "#1831: the units ship inside the wheel as "
                        "coord/deploy/, so this host's venv must have "
                        "swapped first"
                    ),
                )
            )

    if LANE_TUI in wanted:
        for host in ordered_hosts:
            if host in skip:
                continue
            order += 1
            rolls.append(
                LaneRoll(
                    order=order,
                    lane=LANE_TUI,
                    host=host,
                    rationale=(
                        "coord-tui is a pure board-API client — safe at any "
                        "skew, so it goes last and can never block the fleet"
                    ),
                )
            )

    return rolls


def normalize_version(raw: str | None) -> str | None:
    """``v0.4.111`` / ``0.4.111`` -> ``0.4.111``; empty -> ``None``."""
    if not raw:
        return None
    return str(raw).strip().lstrip("vV") or None


def hosts_already_current(
    lane_versions: Mapping[str, Iterable[str | None]], target: str | None
) -> list[str]:
    """Hosts whose every *known* lane already reports *target*.

    A host with an unreadable lane is deliberately **not** current: #1834's
    rule is that ``version=None`` means "no data", which is emphatically not
    "agrees with everyone else". Skipping such a host would let the lane
    nobody can see be the one that stays behind — the 2026-08-04 shape.
    """
    want = normalize_version(target)
    if not want:
        return []
    current: list[str] = []
    for host, versions in lane_versions.items():
        seen = list(versions)
        if not seen or any(normalize_version(v) != want for v in seen):
            continue
        current.append(host)
    return sorted(current)


# ── the journal ──────────────────────────────────────────────────────────────
#
# #1835's fourth acceptance criterion: "the whole sequence is observable
# after the fact: what was published, when each lane rolled, what
# verification said." An append-only JSONL file, one object per attempt, on
# whichever host runs the propagation timer. Deliberately not a DB table:
# this record must survive a half-installed venv and be readable with `tail`
# while the very upgrade it describes is in flight — which is exactly when
# `coord` itself may not import.


#: Filename under the coord state root (``~/.coord`` on Linux — see
#: :func:`coord.platform_paths.default_coord_dir`).
JOURNAL_NAME = "release_propagation.jsonl"

#: Records kept when the journal is trimmed. Small enough to `cat`, long
#: enough to cover a week of a 15-minute timer's deferrals.
JOURNAL_MAX_RECORDS = 2000


@dataclass
class PropagationRecord:
    """One propagation attempt, start to finish, as journalled."""

    started_at: float
    target_version: str | None = None
    status: str = STATUS_DEFERRED
    quiescence: dict = field(default_factory=dict)
    #: ``[{"lane":..., "host":..., "ok":..., "detail":...}, ...]``, in the
    #: order they actually ran.
    lanes: list[dict] = field(default_factory=list)
    #: What `coord release verify` said, as its own JSON report.
    verification: dict | None = None
    rolled_back: list[str] = field(default_factory=list)
    released_holds: list[str] = field(default_factory=list)
    finished_at: float | None = None
    error: str | None = None
    dry_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_VERIFIED, STATUS_DEFERRED, STATUS_UP_TO_DATE)


def journal_path(state_dir: Path) -> Path:
    return Path(state_dir) / JOURNAL_NAME


def append_record(state_dir: Path, record: PropagationRecord) -> Path:
    """Append *record* as one JSON line. Best effort by contract.

    A propagation must never fail *because* it could not write its own
    diary, but a silently-unwritten diary is the 2026-08-04 shape, so the
    caller is told (by the raised error propagating out of here only for
    genuinely unexpected types) — see the shell, which reports a write
    failure as a warning line and still exits on the real outcome.
    """
    path = journal_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    return path


def read_records(state_dir: Path, *, limit: int | None = None) -> list[dict]:
    """Most-recent-last records from the journal; unparseable lines skipped.

    A torn final line (the process died mid-append) must not make the whole
    history unreadable — the history is most valuable in exactly that case.
    """
    path = journal_path(state_dir)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    if limit is not None and limit >= 0:
        out = out[-limit:]
    return out


def trim_journal(state_dir: Path, *, keep: int = JOURNAL_MAX_RECORDS) -> int:
    """Truncate the journal to its last *keep* records. Returns records kept."""
    records = read_records(state_dir)
    if len(records) <= keep:
        return len(records)
    kept = records[-keep:]
    path = journal_path(state_dir)
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in kept), encoding="utf-8"
    )
    return len(kept)


# ── rendering ────────────────────────────────────────────────────────────────


def _stamp(ts: float | None) -> str:
    if not ts:
        return "?"
    import datetime as _dt  # noqa: PLC0415 — leaf import, keeps the module light

    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


_STATUS_MARK = {
    STATUS_VERIFIED: "✓",
    STATUS_UP_TO_DATE: "=",
    STATUS_DEFERRED: "·",
    STATUS_ROLLED: "~",
    STATUS_ROLLED_BACK: "↩",
    STATUS_FAILED: "✗",
}


def render_record(record: PropagationRecord | Mapping[str, Any]) -> list[str]:
    """Human-readable lines for one attempt."""
    data = record.to_dict() if isinstance(record, PropagationRecord) else dict(record)
    status = str(data.get("status") or "?")
    mark = _STATUS_MARK.get(status, "?")
    version = data.get("target_version") or "?"
    prefix = "[dry-run] " if data.get("dry_run") else ""
    lines = [
        f"{mark} {prefix}{_stamp(data.get('started_at'))}  v{version}  {status}"
    ]

    quiescence = data.get("quiescence") or {}
    if quiescence.get("reason"):
        lines.append(f"    window: {quiescence['reason']}")

    for lane in data.get("lanes") or []:
        ok = lane.get("ok")
        lane_mark = "✓" if ok else ("·" if ok is None else "✗")
        detail = lane.get("detail") or ""
        lines.append(
            f"    {lane_mark} {lane.get('lane')}@{lane.get('host')}"
            + (f" — {detail}" if detail else "")
        )

    verification = data.get("verification")
    if verification:
        sev = verification.get("severity", "?")
        findings = verification.get("findings") or []
        lines.append(
            f"    verify: {sev} ({len(findings)} finding(s))"
        )
        for finding in findings[:5]:
            lines.append(
                f"      - [{finding.get('severity')}] {finding.get('host')} "
                f"{finding.get('lane')}: {finding.get('summary')}"
            )
    if data.get("rolled_back"):
        lines.append(f"    rolled back: {', '.join(data['rolled_back'])}")
    if data.get("released_holds"):
        lines.append(f"    released deploy gates: {', '.join(data['released_holds'])}")
    if data.get("error"):
        lines.append(f"    error: {data['error']}")
    return lines


def render_history(records: Sequence[Mapping[str, Any]], *, verbose: bool = False) -> str:
    """The `coord release history` body.

    Without *verbose*, consecutive no-op attempts (deferred / already
    up-to-date) collapse to one summary line — a 15-minute timer produces
    ~96 of those a day and a history nobody can skim is a history nobody
    reads. The count is always printed: #1835's "a silent success is
    indistinguishable from a silent no-op" cuts both ways, so the no-ops are
    summarised, never dropped.
    """
    if not records:
        return (
            "no propagation attempts recorded yet — if the timer is supposed "
            "to be running, that is itself the finding (see `systemctl --user "
            "status coord-release-propagate.timer`)"
        )
    lines: list[str] = []
    run: list[Mapping[str, Any]] = []

    def _flush() -> None:
        if not run:
            return
        first, last = run[0], run[-1]
        if len(run) == 1:
            lines.extend(render_record(first))
        else:
            lines.append(
                f"· {_stamp(first.get('started_at'))} .. "
                f"{_stamp(last.get('started_at'))}  "
                f"{len(run)} no-op attempt(s) "
                f"(last: {last.get('status')} — "
                f"{(last.get('quiescence') or {}).get('reason', '?')})"
            )
        run.clear()

    for record in records:
        if not verbose and str(record.get("status")) in NO_OP_STATUSES:
            run.append(record)
            continue
        _flush()
        lines.extend(render_record(record))
    _flush()
    return "\n".join(lines)
