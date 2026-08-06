"""Persistent pause/resume state for machines (#routing-pause, #1563).

The pause set is a tiny JSON file at ``~/.coord/paused_machines.json``
holding ``{"paused": [<name>, ...]}``.  Both the Python coordinator
(`coord plan`, `coord assign`, auto_loop, reconcile, review,
refine_chat) and the Rust TUI read it to decide whether a given
machine is a candidate for new work — paused machines stay reachable
and visible but never receive new assignments.

Pause does NOT cancel in-flight assignments; the user can `coord stop`
those separately if needed.  This module only governs the routing
decision for *new* work.

#1563: pause is FLEET state, not host state.  The autonomous dispatcher
(`coord serve`'s `_tick_loop` → `reconcile()` / `dispatch_pending_reviews()`
/ `auto_loop`) runs *inside the daemon*, which has no `board_service`
configured for itself — so it always reads the local JSON file below,
same as before.  What used to be broken is a *thin client*: `coord pause`
run on an operator's laptop wrote to the laptop's own copy of this file,
which the daemon never saw.  The public `paused_set()` / `pause()` /
`unpause()` below now check `coord.board_service.resolve()` first: when a
board service IS configured (thin client), they route over HTTP to the
daemon's `/pause` endpoint — which itself calls the same local-only
helpers here, so both the daemon's own tick loop and every thin client
end up reading/writing the *one* copy of this file that actually governs
dispatch.  When no board service is configured (solo/local use, or the
daemon's own in-process calls), behaviour is unchanged: same file, same
atomic tempfile-rename writes.

`pause()`/`unpause()` (explicit user actions) fail LOUDLY on a thin
client — an HTTP/transport error propagates rather than reporting
success (#1563: "there is no configuration in which a thin-client pause
fails loudly. It always reports success and always fails open").
`paused_set()` (the read side, consulted on every dispatch decision)
stays fail-soft on a remote fetch error, consistent with this module's
existing local-read behaviour and every other daemon read-through helper
in `coord.client` (`fetch_issue_context`, `fetch_drive_escalations`, …):
a transient network blip degrades to "nothing is paused" rather than
wedging the dispatcher, matching the pre-existing contract documented
below for a malformed/missing local file.

#1862: per-machine quiet hours.  `Machine.quiet_hours` (parsed by
`coord.config`) declares a recurring daily window during which a machine
should receive no NEW dispatch — the same routing-pause semantics as an
explicit `coord pause`, computed instead of stored.  This module is the
single place that union happens: `paused_set()`/`local_paused_set()`
accept an optional `machines` sequence and, when given one, fold in
`{m.name for m in machines if m.quiet_hours.covers(now)}` — every one of
the eight call sites listed in #1862 already has its `Config` in local
scope, so they pass `config.machines` and inherit quiet hours with no
second routing check anywhere else.  `machines=None` (the default)
degrades to "explicit pauses only", i.e. unchanged pre-#1862 behaviour —
this is what keeps every deployment with no `quiet_hours:` block, and
every caller not yet threading `machines` through, byte-identical to
before.

`coord unpause` during an active quiet window would otherwise be a lie
(#1563's failure class: reports success, changes nothing, the machine is
paused again on the very next read).  `local_unpause_effective()` picks
the "explicit override" resolution named in #1862: unpausing a
quiet-covered, not-explicitly-paused machine records an override that
suppresses quiet hours until the CURRENT window's end (persisted
alongside the explicit-pause list below, under `quiet_overrides`) and
says so — never silently re-paused, never silently accepted as a no-op.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from coord.models import Machine

_STATE_FILENAME = "paused_machines.json"


def _state_path() -> Path:
    """Return the absolute path to the pause-state file.

    Lives under ``$HOME/.coord/`` so it sits alongside the rest of the
    runtime state (`assignments.db`, `agent_state.json`, etc.).
    """
    home = Path(os.environ.get("HOME", "/tmp")).expanduser()
    return home / ".coord" / _STATE_FILENAME


def paused_set(
    machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> set[str]:
    """Read the current set of paused machine names (#1563: daemon-aware).

    Routes through the daemon's `/pause` endpoint when a board service is
    configured (thin client); otherwise reads the local file directly. The
    remote fetch fails SOFT (any error → empty set) — see module docstring
    for why reads stay fail-open while explicit `pause()`/`unpause()` calls
    don't.

    On a thin client *machines*/*now* are ignored — the daemon's own
    `/pause` endpoint already folds its quiet-hours-covered machines into
    the response (see `coord.serve_app.get_pause`), so the thin client's
    view is correct without knowing about quiet hours itself. Pass
    *machines* (almost always `config.machines`, already in scope at every
    call site) to fold quiet hours into the LOCAL computation — the daemon's
    own in-process tick-loop calls (no board service configured for
    itself), and any solo/local use with no daemon at all.
    """
    svc = _resolve_service()
    if svc is not None:
        from coord.client import fetch_paused_machines  # noqa: PLC0415

        try:
            return fetch_paused_machines(svc)
        except Exception:  # noqa: BLE001 — fail-soft read, see module docstring
            return set()
    return local_paused_set(machines, now=now)


def is_paused(
    name: str, machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> bool:
    """Convenience: True when *name* is in the paused set."""
    return name in paused_set(machines, now=now)


def pause(name: str) -> bool:
    """Add *name* to the paused set.  Returns True when the set changed
    (i.e. *name* was not already paused).

    #1563: on a thin client this POSTs to the daemon's `/pause` endpoint
    and raises (`httpx.HTTPError`) if that can't be confirmed — a pause
    that silently fails to reach the daemon is the exact failure mode this
    module exists to close. Callers that must never raise (e.g. an
    interactive command that wants to print a clean error) should catch
    around this call.
    """
    svc = _resolve_service()
    if svc is not None:
        from coord.client import post_pause  # noqa: PLC0415

        result = post_pause(svc, name, "pause")
        return bool(result.get("changed"))
    return local_pause(name)


@dataclass(frozen=True)
class UnpauseOutcome:
    """Result of an #1862-aware unpause — see `local_unpause_effective()`.

    `kind` is one of:
      * ``"resumed"``        — an explicit `coord pause` was lifted.
      * ``"quiet_override"`` — the machine wasn't explicitly paused but WAS
        inside its quiet-hours window; an override now suppresses that
        window until it would have ended anyway (`quiet_until`/`tz`).
      * ``"not_paused"``     — genuinely not paused by any mechanism; a
        true no-op, distinguishable from the two "did something" cases
        above so a caller never reports success for nothing happening.
    """

    changed: bool
    kind: str
    quiet_until: str | None = None  # local "HH:MM" the override lasts until
    tz: str | None = None


def unpause(
    name: str, machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> UnpauseOutcome:
    """Resolve `coord unpause <name>` (#1862-aware).  See `pause()` for the
    thin-client routing / fail-loudly contract, and `UnpauseOutcome` for
    what `.kind` means.

    #1862: unpausing a machine that is inside its quiet-hours window but
    was never explicitly paused must not silently no-op (the machine would
    be paused again on the very next read — #1563's failure class) and
    must not silently pretend to have lifted a pause that was never set.
    Instead it records an explicit override for the remainder of that
    window — see `local_unpause_effective()`.
    """
    svc = _resolve_service()
    if svc is not None:
        from coord.client import post_pause  # noqa: PLC0415

        result = post_pause(svc, name, "unpause")
        changed = bool(result.get("changed"))
        return UnpauseOutcome(
            changed=changed,
            kind=result.get("kind") or ("resumed" if changed else "not_paused"),
            quiet_until=result.get("quiet_until"),
            tz=result.get("tz"),
        )
    return local_unpause_effective(name, machines, now=now)


def _resolve_service():  # -> coord.client.ServiceConfig | None
    from coord.board_service import resolve  # noqa: PLC0415

    return resolve()


# ── local-only (#1563: always used by the daemon's own `/pause` endpoint and
# tick loop, and by every caller when no board service is configured) ───────


def local_paused_set(
    machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> set[str]:
    """The local, effective paused-machine set: explicit pauses UNION any
    machine currently inside its quiet-hours window (#1862), minus any
    machine with an active `coord unpause` override for that window.

    Returns just the explicit set when *machines* is omitted — this is what
    keeps every pre-#1862 caller (and `local_pause`/`local_unpause` below,
    which use this internally to test EXPLICIT membership) byte-identical.
    Malformed/missing local file degrades to "nothing explicitly paused" —
    failure to read should never block routing.

    Unlike `paused_set()`, this NEVER goes over the network — it is what
    the daemon's `/pause` endpoint handler calls (passing `config.machines`
    so quiet hours apply), and what `paused_set()` itself falls through to
    when no board service is configured.
    """
    explicit = _explicit_paused_set()
    if not machines:
        return explicit
    covered = _quiet_covered_names(machines, now=now)
    if not covered:
        return explicit
    return explicit | covered


def local_pause(name: str) -> bool:
    """Add *name* to the local EXPLICIT paused set.  Returns True when the
    set changed (i.e. *name* was not already explicitly paused).

    Deliberately orthogonal to quiet hours: an explicit pause is tracked
    and reported independently of whatever a machine's `quiet_hours` window
    happens to be doing at the same moment.
    """
    current = _explicit_paused_set()
    if name in current:
        return False
    current.add(name)
    _save_state(paused=current)
    return True


def local_unpause(name: str) -> bool:
    """Remove *name* from the local EXPLICIT paused set.  Returns True when
    the set changed (i.e. *name* was actually explicitly paused).

    This is the pre-#1862 primitive: it only ever looks at explicit pauses,
    so it correctly reports "not paused" (False) for a machine that is
    merely inside its quiet-hours window — `local_unpause_effective()`
    below is what gives THAT case a truthful, non-lying answer.
    """
    current = _explicit_paused_set()
    if name not in current:
        return False
    current.discard(name)
    _save_state(paused=current)
    return True


def local_unpause_effective(
    name: str, machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> UnpauseOutcome:
    """#1862: the truthful `coord unpause` — see `UnpauseOutcome` for the
    three possible outcomes and the module docstring for why a bare
    `local_unpause()` (explicit-only) would otherwise let `coord unpause`
    report success and change nothing for a quiet-covered machine.
    """
    if local_unpause(name):
        return UnpauseOutcome(changed=True, kind="resumed")

    now = now if now is not None else datetime.now(timezone.utc)
    machine = next((m for m in (machines or ()) if m.name == name), None)
    qh = machine.quiet_hours if machine is not None else None
    if qh is not None and qh.covers(now):
        until_utc = qh.window_end_instant(now)
        _set_quiet_override(name, until_utc)
        until_local = until_utc.astimezone(ZoneInfo(qh.tz))
        return UnpauseOutcome(
            changed=True,
            kind="quiet_override",
            quiet_until=until_local.strftime("%H:%M"),
            tz=qh.tz,
        )
    return UnpauseOutcome(changed=False, kind="not_paused")


# ── #1862: quiet-hours computation ──────────────────────────────────────────


def quiet_paused_names(
    machines: Sequence["Machine"] | None = None, *, now: datetime | None = None,
) -> set[str]:
    """Public: names of machines currently paused SPECIFICALLY because a
    `quiet_hours` window covers *now* (never overridden — an active
    `coord unpause` override excludes a machine from this set, same as it
    excludes it from `local_paused_set()`'s union).

    Always a subset of `local_paused_set(machines, now=now)`. Review finding
    on #1862's original PR: `coord status`'s `describe_pause_state()`
    distinguished a quiet-paused machine from a hand-paused one, but the
    daemon's `/pause` endpoint and the TUI sidebar badge did not — this is
    the choke point both now call so a machine "asleep until 08:00" reads
    differently from one someone explicitly paused, everywhere pause state
    is displayed, not just `coord status`.
    """
    if not machines:
        return set()
    return _quiet_covered_names(machines, now=now)


def _quiet_covered_names(
    machines: Sequence["Machine"], *, now: datetime | None = None,
) -> set[str]:
    """Names of machines whose `quiet_hours` window covers *now*, excluding
    any with an active `coord unpause` override for that window."""
    now = now if now is not None else datetime.now(timezone.utc)
    overridden = _active_quiet_override_names(now)
    return {
        m.name
        for m in machines
        if m.quiet_hours is not None
        and m.name not in overridden
        and m.quiet_hours.covers(now)
    }


def _active_quiet_override_names(now: datetime | None = None) -> set[str]:
    now = now if now is not None else datetime.now(timezone.utc)
    active: set[str] = set()
    for name, raw_until in _quiet_overrides().items():
        try:
            until = datetime.fromisoformat(raw_until)
        except ValueError:
            continue
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if now < until:
            active.add(name)
    return active


def _set_quiet_override(name: str, until_utc: datetime) -> None:
    overrides = _quiet_overrides()
    overrides[name] = until_utc.astimezone(timezone.utc).isoformat()
    _save_state(quiet_overrides=overrides)


# ── internals ────────────────────────────────────────────────────────────────


def _load_raw() -> dict:
    path = _state_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _explicit_paused_set() -> set[str]:
    try:
        data = _load_raw()
    except (OSError, ValueError):
        return set()
    items = data.get("paused")
    if not isinstance(items, list):
        return set()
    return {str(x) for x in items if isinstance(x, str) and x}


def _quiet_overrides() -> dict[str, str]:
    try:
        data = _load_raw()
    except (OSError, ValueError):
        return {}
    items = data.get("quiet_overrides")
    if not isinstance(items, dict):
        return {}
    return {str(k): str(v) for k, v in items.items() if isinstance(k, str) and isinstance(v, str)}


def _save_state(
    *, paused: set[str] | None = None, quiet_overrides: dict[str, str] | None = None,
) -> None:
    """Read-modify-write the local state file, preserving whichever half
    the caller doesn't pass (explicit pauses vs quiet-hours overrides are
    independent axes — a write to one must never clobber the other)."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new_paused = sorted(paused) if paused is not None else sorted(_explicit_paused_set())
    new_overrides = quiet_overrides if quiet_overrides is not None else _quiet_overrides()
    payload = {"paused": new_paused, "quiet_overrides": dict(sorted(new_overrides.items()))}
    # Atomic write: tempfile in the same dir then rename so a crashed
    # writer can never leave a partially-written file in place.
    fd, tmp = tempfile.mkstemp(prefix=".paused_machines.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── #1862: pause-state display (`coord status`, and any future consumer
# that wants to distinguish a hand pause from a quiet-hours one) ────────────


@dataclass(frozen=True)
class PauseState:
    """Why a machine is (or isn't, but would be if not overridden) paused,
    for display purposes only — never consulted for routing.

    `kind` is one of:
      * ``"hand"``             — an explicit `coord pause`.
      * ``"quiet"``            — inside its quiet-hours window right now.
      * ``"quiet_overridden"`` — inside its quiet-hours window, but a
        `coord unpause` override is currently suppressing it (so it is NOT
        in the effective paused set, and IS dispatchable).
    """

    kind: str
    detail: str


def describe_pause_state(
    machine: "Machine", paused: set[str], *, now: datetime | None = None,
) -> PauseState | None:
    """Derive *machine*'s pause state purely from *paused* (the already-
    fetched effective set from `paused_set()`) plus the machine's own
    locally-known `quiet_hours` config — no extra network round trip.

    Returns ``None`` when the machine isn't paused by any mechanism and
    isn't in an overridden quiet window either.
    """
    covered = machine.quiet_hours is not None and machine.quiet_hours.covers(now)
    in_paused = machine.name in paused
    if in_paused and covered:
        qh = machine.quiet_hours
        assert qh is not None  # covered implies quiet_hours is set
        return PauseState(kind="quiet", detail=f"until {qh.end.strftime('%H:%M')} ({qh.tz})")
    if in_paused:
        return PauseState(kind="hand", detail="")
    if covered:
        return PauseState(kind="quiet_overridden", detail="override active — dispatchable")
    return None
