"""``coord drive-queue`` — the queue CLI and the tick processor (#1754, DQ-2).

The thin I/O shell around :mod:`coord.drive_queue`.  Everything that *decides*
lives there (``plan_tick``); everything that *touches the world* lives here:
the flock, the board fetch, the DQ-1 state accessors, the ``coord drive
--tmux`` subprocess, the escalation write.  Same split, for the same reason, as
``coord/drive.py`` (pure ``decide``) and ``coord/commands/drive.py`` (thin
Click wrapper).

WHY A SEPARATE COMMAND GROUP.  ``drive`` itself spends its argument positions
on ``REPO ISSUE``, which is why its ``--tmux`` companions are flat
(``drive-sessions``/``drive-attach``/``drive-stop``).  The queue has a real
verb set of its own (add/list/remove/move/status/tick), so it gets a group —
``coord drive-queue <verb>`` — rather than six more hyphenated top-level
commands.

TWO POSTURES WORTH KEEPING WHEN EDITING THIS FILE:

* **Fail closed.**  An unreadable board aborts the tick without launching
  anything.  A transient GitHub/daemon error must never read as "nothing is
  running" — that reads as free capacity and stacks drives on live work.
* **Launch out of process.**  ``coord drive --tmux`` is a subprocess, never an
  inline ``Driver.run()``.  A drive runs 60–90 minutes; an inline one under a
  ``Type=oneshot`` timer would hold the unit for hours, and the tick would
  stop being a tick.  ``--tmux`` already waits for a live session writing its
  run log before exiting 0 (#1606), so a non-zero exit here is a genuinely
  failed attempt, not an unknown.
"""

from __future__ import annotations

import json as _json
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

import click

from coord.commands._common import _CONFIG_OPTION
from coord.drive_queue import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_PARALLEL_PER_REPO,
    HOLD_RELEASED,
    QUEUE_ALERT_ISSUE,
    QUEUE_ALERT_REPO,
    QUEUE_ALERT_STAGE,
    RESUME_PROBE_TIMEOUT_SECONDS,
    STATE_BLOCKED,
    STATE_PARKED,
    STATE_RUNNING,
    STATE_WAITING,
    BoardView,
    ProbeResult,
    QueueEntry,
    QueueError,
    TickPlan,
    build_board_view,
    entries_from_rows,
    entry_key,
    fired_holds,
    parse_after_spec,
    parse_key,
    pending_probe_targets,
    plan_tick,
    render_plan,
    validate_enqueue,
)

# Wall-clock ceiling for the `coord drive --tmux` launch subprocess.  The
# launch itself only blocks for #1606's liveness verification (16 × 0.5s) plus
# interpreter startup; this is a backstop against a wedged tmux server, not a
# budget.
_LAUNCH_TIMEOUT_SECONDS = 120.0

# Counts are rendered in pipeline order, not alphabetically, so
# `coord drive-queue status` reads as "1 running · 1 waiting". `parked`
# (#1891) sits between `waiting` and `blocked` — closer to "nothing wrong"
# than to "needs a human", but distinct from both, which is the entire point
# of the state: a held queue must not look like an idle one, see
# `coord.drive_queue.STATE_PARKED`'s docstring.
_STATE_ORDER = (
    STATE_RUNNING, STATE_WAITING, STATE_PARKED, STATE_BLOCKED, "done", "failed",
)


_GROUP_HELP = """The operator-declared `coord drive` work queue (#1750).

`coord drive` drives ONE issue; nothing decided what to drive next, so an
overnight batch was a bash loop that (twice) launched on top of live work.
This is the durable, board-backed replacement: declare the order once, then
let `tick` launch at most one drive per run, first-eligible-wins, never past
the concurrency ceiling.

Run `tick` from a systemd timer (DQ-4) or by hand, on any machine that can
reach the board daemon — the board itself is fleet-global. Liveness of a
RUNNING entry is not: it is always a local `tmux` read, so a tick only ever
confirms a session it launched itself. A tick run on a different machine than
the one that launched an entry reads that entry as UNKNOWN, not dead, and
leaves it alone rather than reaping a healthy drive out from under another
host (#1870).
"""


@click.group("drive-queue", help=_GROUP_HELP)
def drive_queue_group() -> None:
    pass


# ── add ──────────────────────────────────────────────────────────────────────


@drive_queue_group.command("add")
@click.argument("repo")
@click.argument("issue", type=int)
@click.option("--machine", default="", help="Pin the drive to one machine (default: let `coord drive` route it).")
@click.option(
    "--after",
    "after_specs",
    multiple=True,
    default=(),
    help=(
        "Pre-req issues that must land first. `N` or `REPO#N`, comma-separated, "
        "repeatable. Bare numbers resolve against REPO."
    ),
)
@click.option(
    "--position",
    type=int,
    default=None,
    help="Insert at this 0-based slot instead of appending at the tail.",
)
@click.option(
    "--hold-after",
    is_flag=True,
    default=False,
    help=(
        "Deploy gate: when this entry completes, hold the queue — launch "
        "NOTHING until a human deploys and runs `drive-queue resume` (or "
        "`--resume-when` starts passing). `merged` is not `live`."
    ),
)
@click.option(
    "--hold-reason",
    default="",
    help="What the operator must do while the gate is held. Shown in the alert.",
)
@click.option(
    "--resume-when",
    default="",
    help=(
        "Optional shell probe re-run each tick while the gate is held; exit 0 "
        f"auto-releases it. Killed at {RESUME_PROBE_TIMEOUT_SECONDS:.0f}s and "
        "treated as a failure. Requires --hold-after."
    ),
)
@_CONFIG_OPTION
def drive_queue_add(
    repo: str,
    issue: int,
    machine: str,
    after_specs: tuple[str, ...],
    position: int | None,
    hold_after: bool,
    hold_reason: str,
    resume_when: str,
    config_path: Path,
) -> None:
    """Queue REPO ISSUE for `coord drive`, or update it if already queued.

    Validation happens BEFORE the write, the same posture `coord milestone
    write-order` takes for `## Work order`: a self-edge or a dependency cycle
    exits non-zero and leaves the queue exactly as it was.
    """
    from coord.state import enqueue_drive_queue, list_drive_queue  # noqa: PLC0415

    try:
        after = parse_after_spec(after_specs, repo)
        validate_config_repo(config_path, repo)
        validate_hold_flags(hold_after, hold_reason, resume_when)
        validate_enqueue(entries_from_rows(list_drive_queue()), repo, issue, after)
    except QueueError as exc:
        raise click.ClickException(str(exc)) from None

    enqueue_drive_queue(
        repo,
        issue,
        machine=machine or None,
        after=after,
        position=position,
        hold_after=hold_after,
        hold_reason=hold_reason,
        resume_when=resume_when,
    )
    suffix = f" after {', '.join(after)}" if after else ""
    pinned = f" on {machine}" if machine else ""
    gate = ""
    if hold_after:
        gate = " · holds the queue when done"
        if resume_when:
            gate += f" (auto-resume when `{resume_when}` passes)"
    click.echo(f"queued {entry_key(repo, issue)}{pinned}{suffix}{gate}")


def validate_hold_flags(hold_after: bool, hold_reason: str, resume_when: str) -> None:
    """Refuse gate detail without a gate (#1757).

    `--resume-when` / `--hold-reason` on an entry with no `--hold-after` would
    be stored and then never read — a silent no-op on the ONE flag whose whole
    job is to stop the queue.  An operator who mistyped that has no signal at
    all that overnight sequencing will now blow straight through the deploy
    step, so this is a usage error, not a warning.
    """
    if hold_after:
        return
    offenders = [
        flag
        for flag, value in (("--resume-when", resume_when), ("--hold-reason", hold_reason))
        if value
    ]
    if offenders:
        raise QueueError(
            f"{' and '.join(offenders)} require --hold-after "
            "(without it there is no gate to resume or explain)"
        )


def validate_config_repo(config_path: Path, repo: str) -> None:
    """Refuse a repo coordinator.yml has never heard of.

    `coord drive <repo> <issue>` would fail at preflight anyway
    (``DriveStateError: repo ... is not in coordinator.yml``) — catching it at
    ``add`` time turns a mysterious tick-time block into an immediate typo
    report.  Fail-OPEN on a config that won't load at all: a thin client whose
    config cache is momentarily unreadable must still be able to queue work.
    """
    try:
        from coord.commands._common import _load_config  # noqa: PLC0415

        config = _load_config(config_path)
    except Exception:  # noqa: BLE001 — see the fail-open note above
        return
    if config.repo(repo) is None:
        known = ", ".join(sorted(r.name for r in config.repos)) or "(none)"
        raise QueueError(f"repo {repo!r} is not in coordinator.yml (known: {known})")


# ── list ─────────────────────────────────────────────────────────────────────


@drive_queue_group.command("list")
@click.option("--repo", "repo", default=None, help="Restrict to one repo (default: every repo).")
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit the raw rows as JSON.")
@_CONFIG_OPTION
def drive_queue_list(repo: str | None, output_json: bool, config_path: Path) -> None:
    """Show the queue in run order."""
    from coord.state import list_drive_queue  # noqa: PLC0415

    rows = list_drive_queue(repo)
    if output_json:
        click.echo(_json.dumps(rows))
        return
    if not rows:
        click.echo("(drive queue is empty)")
        return
    now = time.time()
    for entry in entries_from_rows(rows):
        bits = [f"{entry.position:>2}  {entry.key:<28} {entry.state}"]
        if entry.machine:
            bits.append(f"machine={entry.machine}")
        if entry.after:
            bits.append(f"after={','.join(entry.after)}")
        if entry.attempts:
            bits.append(f"attempts={entry.attempts}")
        if entry.deferrals:
            bits.append(f"deferrals={entry.deferrals}")
        if entry.hold_after:
            bits.append(f"hold={entry.hold_state or 'armed'}")
        click.echo("  ".join(bits))
        if entry.last_reason:
            click.echo(f"      last{_reason_age_suffix(entry, now)}: {entry.last_reason}")
        for line in _hold_lines(entry):
            click.echo(line)


def _reason_age_suffix(entry: QueueEntry, now: float) -> str:
    """`` (3h ago)`` / `` (42s ago)``, or ``''`` when the age is unknown.

    #2133: a `last_reason` is a snapshot taken the instant the tick (or a
    guard) wrote it — never re-validated — so displaying it bare lets an
    hours-old, no-longer-true observation read as current state (that
    misdirected a live diagnosis on #2104: `checks_failed` was still shown
    ~3 hours after the named checks had gone green, while the actual
    blocker — a later `request-changes` review — was nowhere in the
    output). Stamping the age doesn't make the reason current, but it stops
    it from being silently *mistaken* for current, which is the whole
    defect. Empty for `entry.reason_at is None` — a row predating #2133's
    migration, or an entry whose `last_reason` was never written through
    `update_drive_queue_entry` (e.g. hand-built in a test) — rather than
    guessing an age it doesn't have.
    """
    if entry.reason_at is None:
        return ""
    return f" ({_age_str(max(0.0, now - entry.reason_at))} ago)"


def _age_str(delta_seconds: float) -> str:
    """``42s`` / ``3h`` / ``5d`` — coarse, single-unit, matches #2133's ask."""
    if delta_seconds < 60:
        return f"{int(delta_seconds)}s"
    if delta_seconds < 3600:
        return f"{int(delta_seconds // 60)}m"
    if delta_seconds < 86400:
        return f"{int(delta_seconds // 3600)}h"
    return f"{int(delta_seconds // 86400)}d"


def _hold_lines(entry: QueueEntry) -> list[str]:
    """The gate's rendering for `list` / `status`, or `[]` when there is none.

    Both verbs render through this one function so `list` and `status` can
    never disagree about whether the queue is held — the failure mode that
    makes an operator stop trusting either.
    """
    if not entry.hold_after:
        return []
    lines = [f"      hold-after: {entry.gate_reason}"]
    if entry.resume_when:
        probe = f"      resume-when: {entry.resume_when}"
        if entry.hold_probes:
            probe += f"  (failed {entry.hold_probes}×)"
        lines.append(probe)
    return lines


# ── remove / move ────────────────────────────────────────────────────────────


@drive_queue_group.command("remove")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def drive_queue_remove(repo: str, issue: int, config_path: Path) -> None:
    """Drop REPO ISSUE from the queue (positions are renumbered dense)."""
    from coord.state import dequeue_drive_queue  # noqa: PLC0415

    removed = dequeue_drive_queue(repo, issue)
    if not removed:
        raise click.ClickException(f"{entry_key(repo, issue)} is not in the drive queue")
    click.echo(f"removed {entry_key(repo, issue)} from the drive queue")


@drive_queue_group.command("move")
@click.argument("repo")
@click.argument("issue", type=int)
@click.option("--to", "to_position", type=int, required=True, help="New 0-based position (clamped into range).")
@_CONFIG_OPTION
def drive_queue_move(repo: str, issue: int, to_position: int, config_path: Path) -> None:
    """Move REPO ISSUE to a new position in the queue."""
    from coord.state import move_drive_queue_entry  # noqa: PLC0415

    moved = move_drive_queue_entry(repo, issue, to_position)
    if not moved:
        raise click.ClickException(f"{entry_key(repo, issue)} is not in the drive queue")
    click.echo(f"moved {entry_key(repo, issue)} to position {to_position}")


# ── status ───────────────────────────────────────────────────────────────────


def _counts(rows: list[Mapping[str, Any]]) -> dict[str, int]:
    """State histogram for a queue read."""
    counts: dict[str, int] = {}
    for entry in entries_from_rows(rows):
        counts[entry.state] = counts.get(entry.state, 0) + 1
    return counts


def _queue_alert() -> dict | None:
    """The current queue-level alert record, if a tick raised one.

    Read back through the same synthetic escalation key the tick writes — see
    ``QUEUE_ALERT_REPO`` in coord/drive_queue.py for why that seam and not a
    synthetic ``drive_queue`` row.
    """
    from coord.state import get_drive_escalation  # noqa: PLC0415

    return get_drive_escalation(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)


@drive_queue_group.command("status")
@click.option("--json", "output_json", is_flag=True, default=False, help="Emit counts + alert as JSON.")
@_CONFIG_OPTION
def drive_queue_status(output_json: bool, config_path: Path) -> None:
    """Queue counts by state, plus the current queue-level alert."""
    from coord.state import list_drive_queue  # noqa: PLC0415

    rows = list_drive_queue()
    counts = _counts(rows)
    alert = _queue_alert()
    held = fired_holds(entries_from_rows(rows))

    if output_json:
        click.echo(
            _json.dumps(
                {
                    "total": len(rows),
                    "counts": counts,
                    "alert": alert,
                    # #1757: typed, so a client (or a test) reads the gate
                    # without parsing the rendered sentence back out.
                    "held": [
                        {
                            "key": e.key,
                            "reason": e.gate_reason,
                            "resume_when": e.resume_when,
                            "probes": e.hold_probes,
                        }
                        for e in held
                    ],
                }
            )
        )
        return

    if not rows:
        click.echo("drive queue: empty")
    else:
        ordered = [s for s in _STATE_ORDER if counts.get(s)]
        ordered += sorted(s for s in counts if s not in _STATE_ORDER)
        click.echo(
            "drive queue: " + " · ".join(f"{counts[s]} {s}" for s in ordered)
        )
    # The gate goes ABOVE the alert: "HELD" is the state, the alert is the
    # note about it, and an operator scanning the first line must not have to
    # read three more to learn the queue has stopped.
    for entry in held:
        click.echo(f"HELD — {entry.gate_reason}")
        for line in _hold_lines(entry):
            click.echo(line)
        click.echo("      release with: coord drive-queue resume")
    if alert is not None:
        click.echo(f"alert: {alert.get('reason') or ''}")
        for detail in (alert.get("gate_readings") or "").split(" | "):
            if detail:
                click.echo(f"  {detail}")
    else:
        click.echo("alert: (none)")


# ── resume (#1757) ───────────────────────────────────────────────────────────


@drive_queue_group.command("resume")
@click.argument("repo", required=False)
@click.argument("issue", type=int, required=False)
@_CONFIG_OPTION
def drive_queue_resume(repo: str | None, issue: int | None, config_path: Path) -> None:
    """Release a fired deploy gate so the next tick can launch again.

    With no arguments this releases every held gate — in practice there is at
    most one, because a held queue launches nothing and therefore cannot reach
    a second one. Pass REPO ISSUE to name a specific entry.

    The entry itself is NOT removed or re-run: the release is what unblocks
    the queue, not the held entry leaving it, so `list` keeps its run history.
    """
    from coord.state import list_drive_queue, update_drive_queue_entry  # noqa: PLC0415

    held = fired_holds(entries_from_rows(list_drive_queue()))
    if repo is not None:
        if issue is None:
            raise click.ClickException("give both REPO and ISSUE, or neither")
        wanted = entry_key(repo, issue)
        held = [e for e in held if e.key == wanted]
        if not held:
            raise click.ClickException(
                f"{wanted} has no fired deploy gate to release "
                "(see `coord drive-queue status`)"
            )
    if not held:
        # Exit non-zero: "resume" on a queue that was never held is an
        # operator misreading the board, and a silent success would confirm
        # the misreading.
        raise click.ClickException("no deploy gate is currently held")

    for entry in held:
        update_drive_queue_entry(
            entry.repo, entry.issue, hold_state=HOLD_RELEASED, hold_probes=0
        )
        click.echo(f"released the deploy gate on {entry.key}")
    _clear_queue_alert()
    click.echo("the next tick will launch the next eligible entry")


def _clear_queue_alert() -> None:
    """Drop the queue-level HELD alert once its gate is released.

    Best-effort: the next tick overwrites (or re-raises) this record anyway,
    but leaving a stale "QUEUE HELD" sitting in `status` between the release
    and the next timer fire is exactly the kind of contradiction that trains
    an operator to stop reading alerts.
    """
    try:
        from coord.state import dismiss_drive_escalation  # noqa: PLC0415

        dismiss_drive_escalation(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    except Exception:  # noqa: BLE001 — cosmetic; never fail a release on it
        pass


# ── board-read retry (#2159) ─────────────────────────────────────────────────
#
# A transient SQLite "database is locked" / SQLITE_BUSY on the board read is
# not evidence the board is broken — it is evidence something else (the
# daemon's own tick loop, `coord notify`, the web dashboard, another machine's
# drive-queue tick) was mid-write on the same DB at the exact instant this
# tick asked to read it. The read is idempotent — nothing has been decided
# yet — so retrying it a few times over a couple of seconds is strictly safer
# than the alternative: aborting the whole tick and leaving
# `coord-drive-queue.service` sitting `failed` over a one-off, millisecond-
# wide race (2026-08-12, dellserver, one tick in a 14-hour window).
#
# `coord.db._open` already sets `PRAGMA busy_timeout=5000` on the connection
# the local (daemon-host) board read goes through, so SQLite itself already
# retries internally for up to 5s before ever raising — this loop is the
# belt-and-braces layer above that: it also covers a thin client's board read
# (an HTTP round trip to the daemon, which cannot be taught a SQLite pragma)
# and the case where 5s of internal retry still wasn't enough. Any OTHER read
# failure — the daemon is genuinely unreachable, a malformed payload, a real
# non-transient error — does not match `_is_db_locked_error` and is re-raised
# on the FIRST attempt: retrying those would only delay the fail-closed abort
# this module's docstring requires.
_BOARD_READ_RETRY_ATTEMPTS = 3
_BOARD_READ_RETRY_BACKOFF_SECONDS = (0.5, 1.0)  # 2 sleeps across 3 attempts, ~2s total


def _is_db_locked_error(exc: BaseException) -> bool:
    """``True`` for the transient "database is locked" / SQLITE_BUSY class.

    Matched on the exception's message rather than restricted to
    ``sqlite3.OperationalError``: a thin client's board read goes through
    ``httpx`` against the daemon's ``GET /board``, so a locked DB on the
    daemon's end can surface here wrapped in whatever exception carries the
    daemon's error detail, not necessarily a local ``sqlite3`` exception.
    Every other read failure lacks this text and is unaffected.
    """
    return "database is locked" in str(exc).lower() or "sqlite_busy" in str(exc).lower()


def _fetch_board_view_with_retry() -> BoardView:
    """:func:`_fetch_board_view`, with a bounded retry for lock contention.

    Up to :data:`_BOARD_READ_RETRY_ATTEMPTS` reads, backing off
    :data:`_BOARD_READ_RETRY_BACKOFF_SECONDS` between them — but ONLY while
    the failure matches :func:`_is_db_locked_error`; anything else is
    re-raised immediately so the caller's existing fail-closed abort is
    unchanged. Logs once, at the tick level, when a retry actually recovered
    the read, so the contention stays visible rather than silently smoothed
    over — never once when the very first attempt already succeeded.
    """
    last_exc: Exception | None = None
    for attempt in range(_BOARD_READ_RETRY_ATTEMPTS):
        try:
            board = _fetch_board_view()
        except Exception as exc:  # noqa: BLE001 — re-raised below when not retryable
            if not _is_db_locked_error(exc) or attempt == _BOARD_READ_RETRY_ATTEMPTS - 1:
                raise
            last_exc = exc
            time.sleep(
                _BOARD_READ_RETRY_BACKOFF_SECONDS[
                    min(attempt, len(_BOARD_READ_RETRY_BACKOFF_SECONDS) - 1)
                ]
            )
            continue
        if last_exc is not None:
            click.echo(
                f"board read hit lock contention and recovered after {attempt} "
                f"retry(ies) (last error: {last_exc})"
            )
        return board
    raise AssertionError("unreachable — the loop above always returns or raises")


# ── tick ─────────────────────────────────────────────────────────────────────


def _local_issue_rows() -> list[dict]:
    """``issues`` rows straight from the local DB (daemon-host path only).

    ``BoardFetcher`` builds the standalone payload with
    ``coord.client.serialize_board``, which ships assignment rows and
    ``round_number`` and nothing else — no ``issues`` key at all.  The daemon's
    own ``GET /board`` (``coord.dao.board_projection``) DOES carry one, so a
    thin client already sees issue open/closed state and the daemon host would
    not.  Without this top-up a pre-req that is simply open-and-undispatched
    would look "unknown to the board" on the exact machine a systemd timer runs
    the tick on, and get blocked instead of deferred.

    Fail-soft: an unreadable/absent table degrades to ``[]``, which puts the
    daemon host back on the assignment-only signals rather than aborting.
    """
    from coord.db import get_connection  # noqa: PLC0415

    try:
        rows = get_connection().execute(
            "SELECT repo_name, number, state FROM issues"
        ).fetchall()
    except Exception:  # noqa: BLE001 — see the fail-soft note above
        return []
    return [dict(r) for r in rows]


def _local_merge_queue_rows() -> list[dict]:
    """``merge_queue`` rows straight from the local DB (daemon-host path only).

    Same gap as :func:`_local_issue_rows`, one table over: the standalone
    ``coord.client.serialize_board`` payload ships assignment rows and
    ``round_number`` only — no ``merge_queue`` (and no ``merge_plan``, which
    is computed, not stored) — so on the daemon host itself (no
    ``board_service`` configured, the tick reads the local DB directly)
    :func:`build_board_view`'s ``merge_ci_pending`` fact (#1891) would never
    see a checks-still-pending entry at all. ``merge_plan`` is deliberately
    NOT backfilled here — it needs a live ``config``/``ci_store`` to compute,
    and ``build_board_view`` already falls back to this raw table's ``error``
    column when the plan section is absent, exactly the fallback
    ``drive_state._merge_entry`` uses for the same gap.

    Fail-soft: an unreadable table degrades to ``[]``, same posture as
    :func:`_local_issue_rows`.
    """
    from coord.db import get_connection  # noqa: PLC0415

    try:
        rows = get_connection().execute(
            "SELECT repo_name, issue_number, error FROM merge_queue"
        ).fetchall()
    except Exception:  # noqa: BLE001 — see the fail-soft note above
        return []
    return [dict(r) for r in rows]


def _local_host_id() -> str:
    """This machine's identity for #1870's launch-host / reconcile matching.

    Same normalisation every other host-locality check in this codebase uses
    (``coord/commands/sessions.py``, ``coord/commands/_common.py``,
    ``coord.interactive._get_local_short_hostname``): the short hostname,
    lowercased, domain suffix dropped — so a machine addressed as
    ``dellserver`` in one config and ``dellserver.local`` by DNS still
    compares equal to itself.
    """
    return socket.gethostname().split(".")[0].lower()


def _fetch_cordons() -> dict[str, str]:
    """``{machine: "cordoned: draining for v0.5.31"}`` (#2101).

    Daemon-aware (`coord.machine_pause.cordons()` routes to `GET /pause` on a
    thin client) and fail-SOFT: an unreadable cordon store degrades to "no
    cordons", the same posture `paused_set()` takes on every dispatch decision
    in this codebase. That is the right trade here even though the board fetch
    above fails CLOSED: a missed cordon costs one drive launched into a host
    about to roll (which `coord agent update` then refuses, leaving the entry
    to retry), whereas failing the whole tick closed on a cordon read would
    stop the queue on a network blip — the outage this issue exists to end,
    reintroduced by its own fix.
    """
    from coord.machine_pause import cordons as fetch_cordons  # noqa: PLC0415

    try:
        return {name: record.describe() for name, record in fetch_cordons().items()}
    except Exception as exc:  # noqa: BLE001 — see docstring
        click.echo(f"warning: could not read release cordons ({exc}) — "
                   "treating the fleet as uncordoned", err=True)
        return {}


def _fetch_board_view() -> BoardView:
    """Board + live drive sessions, typed.

    Raises whatever the fetch raised — the caller turns that into a fail-closed
    abort.  ``list_drive_sessions()`` is deliberately NOT allowed to fail the
    tick: it returns ``[]`` when tmux is unavailable, and the board's
    ``active_work`` signal still holds the capacity line in that case.
    """
    from coord.board_service import resolve as resolve_board_service  # noqa: PLC0415
    from coord.drive import list_drive_sessions  # noqa: PLC0415
    from coord.drive_state import BoardFetcher  # noqa: PLC0415

    payload = BoardFetcher().fetch()
    if not isinstance(payload, dict):
        raise ValueError(f"board payload is not an object: {type(payload).__name__}")
    # Standalone shape top-up, gated per-key rather than on "issues" alone
    # (#2040: BoardFetcher's own standalone path now supplies "issues" —
    # see coord.drive_state.BoardFetcher._fetch_local — so a single combined
    # gate on that key would silently stop topping up "merge_queue" too,
    # regressing #1891's merge_ci_pending signal on the daemon host). Each
    # top-up is independently gated on board_service being unset so a thin
    # client never reads its own local DB — both keys are always present in
    # the daemon's HTTP projection, even when empty.
    if resolve_board_service() is None:
        top_up: dict = {}
        if "issues" not in payload:
            top_up["issues"] = _local_issue_rows()
        if "merge_queue" not in payload:
            # #1891: same top-up, one table over — see _local_merge_queue_rows.
            top_up["merge_queue"] = _local_merge_queue_rows()
        if top_up:
            payload = {**payload, **top_up}
    return build_board_view(payload, list_drive_sessions())


def _fetch_exit_reasons(
    entries: list,
) -> tuple[dict[str, str], dict[str, bool], dict[str, bool]]:
    """The drive's own ``drive_exited`` summary — and whether it was
    PERMANENT (a pre-dispatch refusal, or a dead end) — for every ``running``
    entry THIS launch, keyed by entry key (#1845/#1844/#2019).

    ``coord.drive.Driver.run`` already writes the true reason a run stopped —
    a deliberate refusal narrated in full, not just an exit code — to the
    audit trail before it returns. Nothing downstream used to read it, so
    `_reconcile_running`'s "no session, no active work, nothing landed" death
    classifier (which also matches a clean, deliberate exit) always
    overwrote it with a synthesised "drive session died" reason. This is the
    one DB read the shell does to close that gap; `plan_tick`/
    `_reconcile_running` stay pure and just consume the result as data (like
    *probes*).

    Returns ``(reasons, refused, dead_end)``. *reasons* is the summary text,
    same as before #1844. *refused* is ``True`` for a key whose recorded
    ``details.exit_code`` equals ``coord.drive.EXIT_DISPATCH_REFUSED`` — a
    DETERMINISTIC pre-dispatch guard refusal (#1138's oracle-readiness gate,
    #1314's epic-target gate, or any other check `coord assign`/`coord
    approve-plan`/`coord fix` raises a plain ``ValueError`` for) rather than
    a transient crash. `_reconcile_running` uses this second mapping to skip
    straight to `blocked` without spending an attempt — see its ``refused``
    branch.

    *dead_end* (#2019) is the same signal for ``coord.drive.EXIT_DEAD_END``:
    the drive's dead-end predicate found the row terminal and unactionable
    and exited on the first poll rather than counting ``no state change``
    against an event that cannot happen. Same disposition as *refused*
    (`blocked`, no attempt spent), separate mapping so the blocked entry's
    ``last_reason`` names the real cause instead of claiming a pre-dispatch
    guard refused something.

    Scoped with ``since=entry.launched_at`` so a stale reason (or refusal)
    from a PRIOR attempt on the same (repo, issue) — the entry's key doesn't
    change across a retry — is never replayed as if it explained the run
    that just ended. An entry with no `launched_at` (a row from before this
    launch was stamped) is skipped; the caller's fallback wording covers it.

    Fail-soft per entry: an unreadable audit table degrades to "no reason
    known for this entry", never aborts the tick — same posture as
    :func:`_local_issue_rows`.
    """
    from coord.audit import query_audit_log  # noqa: PLC0415
    from coord.drive import EXIT_DEAD_END, EXIT_DISPATCH_REFUSED  # noqa: PLC0415

    reasons: dict[str, str] = {}
    refused: dict[str, bool] = {}
    dead_end: dict[str, bool] = {}
    for e in entries:
        if e.state != STATE_RUNNING or e.launched_at is None:
            continue
        try:
            result = query_audit_log(
                event_type="drive_exited",
                repo=e.repo,
                issue=e.issue,
                since=e.launched_at,
                limit=1,
            )
        except Exception:  # noqa: BLE001 — see the fail-soft note above
            continue
        rows = result.get("entries") or []
        if not rows:
            continue
        row = rows[0]
        summary = row.get("summary")
        if summary:
            reasons[e.key] = str(summary)
        details = row.get("details") or {}
        if details.get("exit_code") == EXIT_DISPATCH_REFUSED:
            refused[e.key] = True
        elif details.get("exit_code") == EXIT_DEAD_END:
            dead_end[e.key] = True
    return reasons, refused, dead_end


def _fetch_gate_a_pending(entries: list) -> dict[str, bool]:
    """``{entry_key: still_waiting_on_a_human}`` for every entry parked on a
    missing Gate-A sign-off (#2063).

    The ``(repo_name, milestone_number)`` pair comes out of the park
    reason's own marker (:func:`coord.gate_a.parse_park_marker`), so this is
    a pure board read — no ``gh`` call, and no work at all on a tick where
    nothing is parked that way.

    Fails **closed**: an entry whose verdict can't be read stays parked. The
    cost of being wrong in that direction is a parked row an operator can
    see and release; the cost of being wrong the other way is dispatching
    work against a contract nobody approved, which is the whole issue.
    """
    from coord.gate_a import approval_fingerprint, parse_park_marker  # noqa: PLC0415

    pending: dict[str, bool] = {}
    #: ``None`` marks "the board could not be read for this milestone".
    cache: dict[tuple[str, int], str | None] = {}
    for e in entries:
        if e.state != STATE_PARKED:
            continue
        parsed = parse_park_marker(getattr(e, "last_reason", "") or "")
        if parsed is None:
            continue
        repo_name, milestone_number, parked_fingerprint = parsed
        key = (repo_name, milestone_number)
        if key not in cache:
            try:
                from coord.state import get_gate_a_approval  # noqa: PLC0415

                raw = get_gate_a_approval(
                    repo_name=repo_name, milestone_number=milestone_number
                )
                cache[key] = approval_fingerprint(raw)
            except Exception:  # noqa: BLE001 — fail closed, stay parked
                cache[key] = None
        if cache[key] is None:
            pending[e.key] = True
            continue
        # Resume when the stored verdict has CHANGED since the park — not
        # merely when one exists. A `--changes` verdict refuses too, so
        # "exists" would resume, relaunch, refuse and re-park every tick
        # forever; "changed" bounds it at one relaunch per operator action.
        # The guard itself re-derives the real answer (including the
        # contract-SHA freshness check) on that relaunch, so this predicate
        # deliberately does not duplicate it.
        pending[e.key] = cache[key] == parked_fingerprint
    return pending


def _launch_argv(entry: QueueEntry, config_path: Path | None) -> list[str]:
    """The ``coord drive --tmux`` argv for *entry*.

    #1809: this is the argv the tick actually spawns as a subprocess (below,
    in the caller). When ``coord_argv()``'s PATH-less fallback was silently
    broken (no ``__main__`` guard on ``coord/cli.py``), that subprocess
    exited 0 having imported the module and run nothing — BEFORE ever
    reaching ``launch_drive_in_tmux``'s #1606 alive/log-growth verification.
    From the tick's side that is indistinguishable from a real launch that
    passed verification: both are "subprocess exited 0". That fully explains
    a launch reported as a success banner while its tmux session had already
    died — no separate bug in this module's returncode handling or in
    ``launch_drive_in_tmux``'s growth check (both were re-verified against
    the #1809 investigation and are correct: a non-zero exit is never
    counted as running — see ``test_a_failed_launch_is_a_consumed_attempt_
    not_a_running_entry`` — and the growth check does register an
    absent-before-launch log file that then gets written to, per
    ``test_session_dies_immediately_raises_instead_of_reporting_success``).
    Fixing the ``__main__`` guard closes this path too, since it is the same
    fallback the driver's own ``coord assign`` calls go through.
    """
    from coord.drive import coord_argv  # noqa: PLC0415

    argv = coord_argv() + ["drive", entry.repo, str(entry.issue), "--tmux"]
    if entry.machine:
        argv += ["--machine", entry.machine]
    if config_path:
        argv += ["--config", str(config_path)]
    return argv


def _run_resume_probe(entry: QueueEntry) -> ProbeResult:
    """Run one entry's ``--resume-when`` probe with a hard timeout.

    TRUST BOUNDARY — READ THIS BEFORE TOUCHING THE FUNCTION.
    ``resume_when`` is a SHELL command, executed by the tick, as the tick's
    user, on the daemon host.  That is deliberate, and it is acceptable for
    exactly one reason: the string is **operator-authored and
    operator-scoped**, the same trust level as the ``ExecStart=`` line of the
    systemd timer unit that invokes this tick in the first place.  It is:

    * NOT sent to a worker and never executed on a worker machine;
    * NOT derived from an issue body, a PR, a review comment, a plan, or any
      other model output;
    * NOT reachable by anything an agent writes — ``coord drive-queue add`` is
      the only writer of this column, and DQ-1's update whitelist
      (``coord.state._DRIVE_QUEUE_UPDATABLE``) deliberately excludes it, so
      not even the tick can rewrite its own probe.

    If any of those three ever stops being true, this is remote code execution
    on the daemon host and the feature must be redesigned — not patched.

    Fails CLOSED: a non-zero exit, a timeout, or a command that could not be
    spawned at all all keep the gate held.  A gate that releases because its
    probe crashed is a gate that never existed.
    """
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415

    try:
        proc = subprocess.Popen(  # noqa: S602 — operator-authored; see the trust note
            entry.resume_when,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # Its own process GROUP, so a timeout can kill the whole tree.
            # `sh -c 'a | b'` leaves children that outlive the shell; killing
            # only the shell would leave a wedged probe holding the pipe and
            # the tick blocked in communicate() — a tick that stops ticking is
            # indistinguishable from a queue with nothing to do (#1616).
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return ProbeResult(entry.key, False, f"could not run the probe: {exc}")

    try:
        out, _ = proc.communicate(timeout=RESUME_PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.communicate(timeout=2.0)
        except (subprocess.SubprocessError, OSError):
            pass
        return ProbeResult(
            entry.key,
            False,
            f"timed out after {RESUME_PROBE_TIMEOUT_SECONDS:.0f}s (killed)",
        )

    tail = (out or "").strip().splitlines()
    detail = f"exit {proc.returncode}"
    if proc.returncode != 0 and tail:
        detail += f": {tail[-1][:160]}"
    return ProbeResult(entry.key, proc.returncode == 0, detail)


def _apply_writes(plan: TickPlan) -> None:
    from coord.state import update_drive_queue_entry  # noqa: PLC0415

    for key, updates in plan.writes():
        parsed = parse_key(key)
        if parsed is None:
            continue
        update_drive_queue_entry(parsed[0], parsed[1], **updates)


def _escalate(repo: str, issue: int, *, reason: str, gates: str, command: str) -> None:
    from coord.state import record_drive_escalation  # noqa: PLC0415

    record_drive_escalation(
        repo,
        issue,
        stage=QUEUE_ALERT_STAGE,
        reason=reason,
        gate_readings=gates,
        proposed_command=command,
    )


def _requeue_command(entry: QueueEntry | None, key: str) -> str:
    """The one-key fix for a blocked entry: drop it and re-add it clean.

    There is deliberately no ``coord drive-queue reset`` — DQ-1's update
    whitelist keeps run state out of the operator's write surface, so
    remove+add IS the reset (a fresh row is ``waiting`` with ``attempts=0``
    and no ``after``).  Re-adding without the bad ``--after`` is also the fix
    for an unsatisfiable pre-req.
    """
    parsed = parse_key(key)
    if parsed is None:
        return "coord drive-queue list"
    repo, issue = parsed
    tail = f" --machine {entry.machine}" if entry is not None and entry.machine else ""
    return (
        f"coord drive-queue remove {repo} {issue} && "
        f"coord drive-queue add {repo} {issue}{tail}"
    )


@drive_queue_group.command("tick")
@click.option(
    "--max-parallel",
    type=int,
    default=1,
    show_default=True,
    help=(
        "Concurrency ceiling. Capacity is counted from BOARD state, not from a "
        "session count, so a drive whose observer hit its deadline (#1660) "
        "still occupies a slot."
    ),
)
@click.option(
    "--max-parallel-per-repo",
    type=int,
    default=DEFAULT_MAX_PARALLEL_PER_REPO,
    show_default=True,
    help=(
        "Per-repo concurrency ceiling, applied after --max-parallel (#1972). "
        "An entry whose repo is already at it DEFERS — position unchanged, no "
        "attempt spent — so the walk lands on the first entry from a repo with "
        "headroom: per-repo serialisation, cross-repo parallelism. 0 disables "
        "it, restoring one global counter."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the resolved plan and mutate nothing.",
)
@click.option(
    "--reconcile-only",
    is_flag=True,
    default=False,
    help=(
        "Update the queue's view of reality and launch nothing (#2110). "
        "Every `running` entry is still checked against the board "
        "(done/blocked/parked/retry, exactly as a normal tick would), but no "
        "new `coord drive` is ever started this run — equivalent to "
        "`--max-parallel 0`. This is the missing primitive for the "
        "stop-the-timer-to-roll-the-fleet sequence: with the timer stopped, "
        "nothing reconciles a finished drive's `running` row, and that stale "
        "row alone can pin `coord release propagate` indefinitely. Run this "
        "once (by hand, timer still stopped) to drain it before propagating, "
        "then restart the timer."
    ),
)
@_CONFIG_OPTION
def drive_queue_tick(
    max_parallel: int,
    max_parallel_per_repo: int,
    dry_run: bool,
    reconcile_only: bool,
    config_path: Path,
) -> None:
    """Drain one step of the queue: reconcile, then launch at most one drive.

    Safe to run on any interval and from any machine that can reach the board
    daemon. A tick already in progress makes this a quiet no-op (exit 0) — a
    slow tick must never stack, and two ticks seconds apart are safe: a drive
    launched inside the startup grace window reconciles as `starting`
    (occupying a slot, attempts untouched) rather than as a death (#1794).
    Two ticks on DIFFERENT machines are also safe: liveness is always a local
    `tmux` read, so a tick reconciles only the entries it itself launched —
    one launched elsewhere reads as `unknown`, occupying its slot but never
    retried or relaunched, rather than being declared dead out from under the
    host actually running it (#1870).

    Capacity has two ceilings (#1972): the global `--max-parallel`, then
    `--max-parallel-per-repo` (default 1). Entries whose repo is already at the
    per-repo ceiling defer, so a tick with a claude-coordinator drive running
    skips the 38 queued claude-coordinator entries behind it and launches the
    quadraui one — per-repo serialisation, cross-repo parallelism. `--dry-run`
    prints the per-repo breakdown so "why didn't item 2 go?" is answerable from
    the output alone.

    `--max-parallel 0` (or `--reconcile-only`, the readable spelling of the
    same thing — #2110) reconciles every `running` entry against the board and
    then stops: no capacity walk, no deferrals, no queue-level alert, no
    launch. Reconciliation (`plan_tick` step 1/1b: a finished entry moves to
    `done`, a permanently-refused one to `blocked`, a CI-pending one parks) is
    unconditional and runs regardless of capacity, which is what makes this
    safe to run with the periodic timer stopped — see
    `docs/AGENT_OPERATIONS.md`'s propagation section for why that combination
    used to deadlock.
    """
    from coord.filelock import FileLock, LockBusy, drive_queue_lock_path  # noqa: PLC0415
    from coord.state import list_drive_queue, update_drive_queue_entry  # noqa: PLC0415

    if max_parallel < 0:
        raise click.ClickException(
            "--max-parallel must be at least 0 (0 = reconcile-only, launch "
            "nothing this run — see --reconcile-only)"
        )
    if max_parallel_per_repo < 0:
        raise click.ClickException(
            "--max-parallel-per-repo must be 0 (no per-repo ceiling) or more"
        )

    # #2110: `--reconcile-only` and `--max-parallel 0` are the same request —
    # one flag is a mnemonic for the other rather than a second code path, so
    # there is exactly one way this behaves, not two that could drift apart.
    reconcile_only = reconcile_only or max_parallel == 0
    effective_capacity = 0 if reconcile_only else max_parallel

    lock = FileLock(drive_queue_lock_path())
    try:
        lock.acquire(timeout=0.0)
    except LockBusy:
        # Quiet by design: this is the normal outcome when a timer fires while
        # the previous tick is still verifying a launch.  Noise here would
        # train the operator to ignore the log.
        click.echo("another drive-queue tick is running — skipping")
        return
    except OSError as exc:
        raise click.ClickException(f"could not take the drive-queue lock: {exc}") from None

    try:
        # FAIL CLOSED. An unreadable board is not "nothing is running"; it is
        # "we do not know what is running", and launching on that assumption is
        # how a sequential batch becomes concurrent on the fleet.
        try:
            board = _fetch_board_view_with_retry()
        except Exception as exc:  # noqa: BLE001 — every fetch failure is fatal here
            raise click.ClickException(
                f"could not read the board — aborting without launching anything: {exc}"
            ) from None

        entries = entries_from_rows(list_drive_queue())

        # #1757: run each held gate's `--resume-when` BEFORE deciding
        # anything, and hand the results to `plan_tick` as data so the
        # decision half stays pure.  Deliberately skipped under `--dry-run`:
        # the probe is an arbitrary operator-authored shell command and
        # `--dry-run` promises to touch nothing.  The consequence — a dry run
        # reports the gate as still held even if the deploy just landed — is
        # stated in the output rather than left for the operator to discover.
        probes: dict[str, ProbeResult] = {}
        pending = pending_probe_targets(entries)
        if pending and dry_run:
            click.echo(
                f"(--dry-run: not running {len(pending)} --resume-when probe(s); "
                "a held gate below may already be releasable)"
            )
        elif not dry_run:
            for target in pending:
                probes[target.key] = _run_resume_probe(target)

        # #1845/#1844: the drive's own `drive_exited` summary — and, when it
        # was a PERMANENT pre-dispatch guard refusal, that fact too — for
        # each `running` entry, when one was recorded for THIS launch. Read
        # here (the shell) and handed to `plan_tick` as data, same as
        # `probes`, so a "no session, no active work, nothing landed"
        # reconcile can report the drive's real reason instead of a
        # synthesised "drive session died" for an exit that was actually
        # deliberate, and — when it was a deterministic refusal — block
        # immediately instead of spending an attempt on a guaranteed-to-fail
        # retry. #2019 adds a second permanent cause with the same handling:
        # a drive that exited on its own dead-end predicate.
        exit_reasons, exit_refused, exit_dead_end = _fetch_exit_reasons(entries)

        # #2063: for every entry parked on a missing Gate-A human sign-off,
        # re-read the recorded verdict so `plan_tick` can un-park it the tick
        # after the operator approves. Board-only (the (repo, milestone) pair
        # is embedded in the park reason's marker), so this costs nothing per
        # tick when no entry is parked that way — which is the common case.
        gate_a_pending = _fetch_gate_a_pending(entries)

        # #2101: release cordons. THIS is the hole the issue names — the
        # queue's launcher had zero pause awareness (`coord/drive.py` checks
        # pause only when routing a *worker*), so a cordoned host kept getting
        # drive sessions launched on it and the fleet could never drain into
        # a rollable state.
        cordons = _fetch_cordons()

        # #1794: the clock is the shell's to read, never `coord.drive_queue`'s.
        # It powers the startup grace window on both sides of the tick — a
        # drive launched seconds ago is `starting`, not dead, and cannot be
        # relaunched — so a tick that fires immediately after another (which
        # `docs/DRIVE_QUEUE.md` §2's install sequence reliably produces) sees
        # a running entry rather than a phantom death.
        #
        # #1870: same posture for the machine's own identity. Liveness is a
        # LOCAL tmux read; without `local_host` a tick on host B would read a
        # healthy drive launched on host A as dead the instant it fell out of
        # #1794's grace window, and reap it.
        plan = plan_tick(
            entries,
            board,
            effective_capacity,
            max_parallel_per_repo=max_parallel_per_repo,
            probes=probes,
            now=time.time(),
            local_host=_local_host_id(),
            exit_reasons=exit_reasons,
            exit_refused=exit_refused,
            exit_dead_end=exit_dead_end,
            gate_a_pending=gate_a_pending,
            cordons=cordons,
        )

        if reconcile_only:
            # #2110: capacity 0 already makes `plan_tick` return before its
            # capacity walk (no deferrals, no queue-level alert, no launch —
            # see its docstring's step 3), so `plan.launch` is guaranteed
            # `None` below without any extra branching here. This line exists
            # purely so the log reads as an intentional reconcile-only run
            # rather than a queue that mysteriously stopped launching.
            click.echo("(--reconcile-only: updating queue state, launching nothing)")

        for line in render_plan(plan, dry_run=dry_run):
            click.echo(line)
        if dry_run:
            return

        _apply_writes(plan)

        by_key = {e.key: e for e in entries}
        for item in plan.blocked:
            parsed = parse_key(item.key)
            if parsed is None:
                continue
            entry = by_key.get(item.key)
            _escalate(
                parsed[0],
                parsed[1],
                reason=item.reason,
                gates=(
                    f"queue_state=blocked | position="
                    f"{entry.position if entry else '?'} | after="
                    f"{','.join(entry.after) if entry and entry.after else '(none)'}"
                ),
                command=_requeue_command(entry, item.key),
            )

        if plan.alert is not None:
            _escalate(
                QUEUE_ALERT_REPO,
                QUEUE_ALERT_ISSUE,
                reason=plan.alert.reason,
                gates=" | ".join(plan.alert.details),
                command=plan.alert.command,
            )
        elif any(h.outcome == "released" for h in plan.holds):
            # A probe just auto-released the gate.  Drop the "QUEUE HELD"
            # record in the same tick, or `status` keeps shouting HELD while
            # the queue is demonstrably running again — the contradiction that
            # teaches an operator to stop reading alerts.
            _clear_queue_alert()

        target = plan.launch
        if target is None:
            return

        argv = _launch_argv(target, config_path)
        try:
            result = subprocess.run(  # noqa: S603 — argv built from coord_argv + typed row
                argv,
                capture_output=True,
                text=True,
                timeout=_LAUNCH_TIMEOUT_SECONDS,
            )
            returncode = result.returncode
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            message = detail[-1] if detail else ""
        except (subprocess.SubprocessError, OSError) as exc:
            returncode, message = 1, str(exc)

        if returncode == 0:
            from coord.drive import drive_session_name  # noqa: PLC0415

            session = drive_session_name(target.repo, target.issue)
            update_drive_queue_entry(
                target.repo,
                target.issue,
                state=STATE_RUNNING,
                session_name=session,
                launched_at=time.time(),
                last_reason="",
                # #1870: stamp THIS host as the launcher so a later tick —
                # possibly on a different machine — knows whose tmux to trust.
                launch_host=_local_host_id(),
            )
            click.echo(f"launched {target.key} in tmux session {session!r}")
            return

        # #1606: `--tmux` only exits 0 once the session is live and writing its
        # run log, so a non-zero exit means nothing is running — record a
        # consumed attempt, never a running entry.
        attempts = target.attempts + 1
        reason = (
            f"launch failed (exit {returncode}): {message}"
            if message
            else f"launch failed (exit {returncode})"
        )
        if attempts < DEFAULT_MAX_ATTEMPTS:
            update_drive_queue_entry(
                target.repo,
                target.issue,
                state=STATE_WAITING,
                attempts=attempts,
                last_reason=reason,
            )
        else:
            update_drive_queue_entry(
                target.repo,
                target.issue,
                state=STATE_BLOCKED,
                attempts=attempts,
                last_reason=reason,
            )
            _escalate(
                target.repo,
                target.issue,
                reason=reason,
                gates=f"queue_state=blocked | attempts={attempts}",
                command=_requeue_command(target, target.key),
            )
        raise click.ClickException(reason)
    finally:
        lock.release()
