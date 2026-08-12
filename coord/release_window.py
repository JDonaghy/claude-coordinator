"""Decision half of the nightly daemon-host release window (#2112).

`coord release propagate` (#1835/#2067) rolls each host at ITS OWN quiescent
window — except the daemon host, which the daemon-first lane order (#1835's
LANE ORDER, the documented 405) forces to gate the *whole run*: no lane may
roll ahead of an unrolled daemon. dellserver is both the daemon host and a
work machine, and every unpinned drive-queue entry charges it via
``launch_host`` (see :func:`coord.release_propagate.busy_host_for_entry`)
regardless of where the worker actually lands — so almost any drive anywhere
keeps the daemon "busy" and defers the entire fleet. Measured 2026-08-10: the
fleet sat eleven releases behind for a day with elitebook idle and rollable
throughout.

#2101 (release cordons) answers this for every OTHER host: cordon it,
drain it, roll it the moment it's free. It cannot answer it for the daemon
host itself — cordoning stops NEW work from routing there, but the daemon
host is what runs the drive-queue tick that launches drives fleet-wide in
the first place, so cordoning it does not stop new drives from being queued
against it. The only way to guarantee the daemon host reaches quiescence is
to stop the thing that launches work onto it. Hence this module: a nightly
window that stops `coord-drive-queue.timer`, waits (bounded) for whatever is
already running to finish, rolls, and restarts the timer — always, whether
or not the roll happened.

GATED ON #2110, HARD PREREQUISITE
----------------------------------
Steps "stop the timer, wait, roll" are exactly the sequence that deadlocked
on 2026-08-10: the reconciler that moves a finished drive from `running` to
`done` lives *inside* `coord drive-queue tick`, so stopping the timer stops
reconciliation too, and the last drive's row stays `running` forever — the
daemon host reads as busy permanently and this window would defer forever,
every night, unattended, and exit 0. #2110 made `coord drive-queue tick
--reconcile-only` (equivalently `--max-parallel 0`) safe to call on its own,
which is exactly what the drain loop below needs: reconcile without
launching anything new.

THE THREE TRAPS THIS MODULE IS SHAPED AROUND
----------------------------------------------
1. **Never `--force`.** That flag kills in-flight headless workers — the
   whole reason propagation is quiescence-scheduled — and an unattended
   nightly job must never carry it. If the drain does not finish, this
   module says so and declines; it does not reach for the escape hatch.
2. **The drain is bounded.** :data:`DEFAULT_DRAIN_WAIT_SECONDS` is the
   deadline after which the caller must restart the timer and report
   failure rather than leave the queue stopped into the working day — the
   mirror image of "an expired deadline stops the observer, not the work."
   Renamed from ``DEFAULT_DRAIN_DEADLINE_SECONDS`` (#2136): this module's
   3600s bounded *wait* for its own drain loop and
   :mod:`coord.release_cordon`'s 5400s cordon-escalation deadline are
   different concepts that used to share one name across two release
   modules — a readability trap that reads as one constant drifting in
   value, when they were never the same constant.
3. **A skipped night must be loud.** Every non-happy status here
   (:data:`STATUS_DRAIN_TIMEOUT`, :data:`STATUS_PROPAGATE_DEFERRED`,
   :data:`STATUS_PROPAGATE_FAILED`, :data:`STATUS_ERROR`) is something the
   I/O shell (`coord/commands/release.py`'s `nightly-window` command) is
   expected to escalate through `coord.state.record_drive_escalation` — the
   same channel #2101's drain-deadline escalation and #2082 exist to make
   this class of silence impossible.

Architecture mirrors `coord/release_propagate.py` and `coord/release_cordon.py`
on purpose: this module is pure decision-making over already-fetched facts
(a version string, a deadline, elapsed time) plus the journal format, so it
is unit-testable with no fleet, no systemd and no board. Everything that
needs a live host — stopping/starting the timer, running the reconcile-only
tick, invoking `coord release propagate`, writing the escalation — lives in
the command's I/O shell, next to `release_propagate`'s own for the same
reason (see that module's docstring's "what lives here" split).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

#: The systemd --user timer this window stops for the duration of the
#: drain. Overridable (`--queue-timer`) purely for tests and for an
#: unusual install that renamed the unit; production has exactly one name.
DEFAULT_QUEUE_TIMER = "coord-drive-queue.timer"

#: Bounded wait (trap 2) for in-flight drives to finish before the window
#: gives up, restarts the queue and reports failure. An hour leaves ample
#: room inside the 22:00-08:00 quiet-hours window even started as late as
#: 03:00 — the queue is never stopped anywhere near the working day.
#:
#: Formerly named ``DEFAULT_DRAIN_DEADLINE_SECONDS`` — the same name
#: :mod:`coord.release_cordon` uses for a different value (5400.0, its
#: cordon-escalation deadline). Renamed (#2136) so the two stop reading as
#: one constant that drifted between modules.
DEFAULT_DRAIN_WAIT_SECONDS = 3600.0

#: How often the drain loop reconciles the queue and re-checks quiescence.
#: `coord-drive-queue.timer` itself fires every 3 minutes in production —
#: polling much faster than that buys nothing (nothing on the board can
#: change faster) and just spends `coord drive-queue tick --reconcile-only`
#: subprocesses for no reason.
DEFAULT_POLL_INTERVAL_SECONDS = 30.0


# ── status vocabulary ────────────────────────────────────────────────────
#
# A small closed set, mirroring release_propagate.py's STATUS_* — so an
# operator who already knows how to read `coord release history` does not
# have to learn a second vocabulary for this journal.

STATUS_UP_TO_DATE = "up-to-date"
STATUS_DRY_RUN = "dry-run"
STATUS_ROLLED = "rolled"
STATUS_DRAIN_TIMEOUT = "drain-timeout"
STATUS_PROPAGATE_DEFERRED = "propagate-deferred"
STATUS_PROPAGATE_FAILED = "propagate-failed"
STATUS_ERROR = "error"

#: Statuses meaning "this window did what it was for, or correctly had
#: nothing to do" — everything else is a night propagation was supposed to
#: happen and did not (trap 3: loud, not silent).
OK_STATUSES = frozenset({STATUS_UP_TO_DATE, STATUS_ROLLED, STATUS_DRY_RUN})

#: The inverse of OK_STATUSES, spelled out for readability at call sites.
LOUD_STATUSES = frozenset(
    {STATUS_DRAIN_TIMEOUT, STATUS_PROPAGATE_DEFERRED, STATUS_PROPAGATE_FAILED, STATUS_ERROR}
)


def needs_roll(daemon_version: str | None, target_version: str | None) -> bool:
    """Is there anything for this window to do?

    #2112 acceptance 3: "with the fleet already current, the job does not
    stop the queue at all." This is the check that has to answer that
    *before* anything touches the queue.

    `daemon_version=None` (no data — an unreachable daemon host, an
    unreadable python lane) reads as NEEDS a roll, never as "current": #1834's
    rule is that no-data is not evidence of agreement, and skipping the
    window on a guess is exactly the silent-no-op shape this issue exists to
    close. Delegates the actual comparison to
    :func:`coord.release_cordon.version_drift` rather than a third
    reimplementation of version arithmetic in this codebase.
    """
    from coord.release_cordon import version_drift  # noqa: PLC0415

    if not target_version:
        return False
    drift = version_drift(daemon_version, target_version)
    return drift is None or drift > 0


@dataclass
class DrainOutcome:
    """What the bounded wait for in-flight drives found (trap 2)."""

    drained: bool
    elapsed_seconds: float
    detail: str = ""


@dataclass
class WindowRecord:
    """One nightly-window attempt, start to finish, as journalled.

    Deliberately shaped like `release_propagate.PropagationRecord`: same
    append-only-JSONL-one-object-per-attempt journal, same reason — this
    record must survive a half-installed venv and be readable with `tail`
    while the very upgrade it describes is in flight.
    """

    started_at: float
    target_version: str | None = None
    daemon_host: str | None = None
    daemon_version: str | None = None
    status: str = STATUS_ERROR
    queue_timer: str = DEFAULT_QUEUE_TIMER
    queue_stopped: bool | None = None
    queue_stop_detail: str = ""
    drained: bool | None = None
    drain_seconds: float | None = None
    drain_detail: str = ""
    queue_restarted: bool | None = None
    queue_restart_detail: str = ""
    propagate_status: str | None = None
    propagate_exit_code: int | None = None
    propagate_output: str = ""
    finished_at: float | None = None
    error: str | None = None
    dry_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES


# ── the journal ──────────────────────────────────────────────────────────

#: Filename under the coord state root (`~/.coord` on Linux — see
#: `coord.platform_paths.default_coord_dir`). Separate from
#: `release_propagate.JOURNAL_NAME`: this record carries fields (queue
#: stop/drain/restart) a plain propagate attempt does not have, and
#: conflating the two would make either journal's shape a lie about the
#: other.
JOURNAL_NAME = "release_window.jsonl"

#: Records kept when the journal is trimmed. One per night, so this is
#: years of history — small enough to `cat`, generous enough that "when did
#: this last actually roll something" never scrolls off.
JOURNAL_MAX_RECORDS = 2000


def journal_path(state_dir: Path) -> Path:
    return Path(state_dir) / JOURNAL_NAME


def append_record(state_dir: Path, record: WindowRecord) -> Path:
    """Append *record* as one JSON line. Best effort by contract.

    A window run must never fail *because* it could not write its own
    diary — but a silently-unwritten diary is the exact 2026-08-04/#2082
    shape, so the caller is told (the shell reports a write failure as a
    warning and still exits on the real outcome, same as
    `release_propagate.append_record`).
    """
    path = journal_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    return path


def read_records(state_dir: Path, *, limit: int | None = None) -> list[dict]:
    """Most-recent-last records from the journal; unparseable lines skipped.

    A torn final line (the process killed mid-append — see #2112 acceptance
    4) must not make the whole history unreadable.
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


# ── rendering ────────────────────────────────────────────────────────────


def _stamp(ts: float | None) -> str:
    if not ts:
        return "?"
    import datetime as _dt  # noqa: PLC0415 — leaf import, keeps the module light

    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


_STATUS_MARK = {
    STATUS_UP_TO_DATE: "=",
    STATUS_DRY_RUN: "·",
    STATUS_ROLLED: "✓",
    STATUS_DRAIN_TIMEOUT: "⏱",
    STATUS_PROPAGATE_DEFERRED: "~",
    STATUS_PROPAGATE_FAILED: "✗",
    STATUS_ERROR: "✗",
}


def render_record(record: WindowRecord | Mapping[str, Any]) -> list[str]:
    """Human-readable lines for one attempt — what `coord release
    window-history` and the command's own stdout print."""
    data = record.to_dict() if isinstance(record, WindowRecord) else dict(record)
    status = str(data.get("status") or "?")
    mark = _STATUS_MARK.get(status, "?")
    version = data.get("target_version") or "?"
    prefix = "[dry-run] " if data.get("dry_run") else ""
    lines = [
        f"{mark} {prefix}{_stamp(data.get('started_at'))}  v{version}  {status}"
    ]
    if data.get("daemon_host"):
        lines.append(
            f"    daemon host: {data['daemon_host']} "
            f"(was v{data.get('daemon_version') or '?'})"
        )
    if data.get("queue_stopped") is not None:
        lines.append(
            f"    {data.get('queue_timer')}: stopped="
            f"{data['queue_stopped']} ({data.get('queue_stop_detail') or '-'})"
        )
    if data.get("drained") is not None:
        secs = data.get("drain_seconds")
        verdict = "clean" if data["drained"] else "TIMED OUT"
        suffix = f" after {secs:.0f}s" if secs is not None else ""
        lines.append(f"    drain: {verdict}{suffix}")
        if data.get("drain_detail"):
            lines.append(f"      {data['drain_detail']}")
    if data.get("propagate_status"):
        lines.append(
            f"    propagate: {data['propagate_status']} "
            f"(exit {data.get('propagate_exit_code')})"
        )
    if data.get("queue_restarted") is not None:
        lines.append(
            f"    {data.get('queue_timer')}: restarted="
            f"{data['queue_restarted']} ({data.get('queue_restart_detail') or '-'})"
        )
    if data.get("error"):
        lines.append(f"    error: {data['error']}")
    return lines
