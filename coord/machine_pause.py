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
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_STATE_FILENAME = "paused_machines.json"


def _state_path() -> Path:
    """Return the absolute path to the pause-state file.

    Lives under ``$HOME/.coord/`` so it sits alongside the rest of the
    runtime state (`assignments.db`, `agent_state.json`, etc.).
    """
    home = Path(os.environ.get("HOME", "/tmp")).expanduser()
    return home / ".coord" / _STATE_FILENAME


def paused_set() -> set[str]:
    """Read the current set of paused machine names (#1563: daemon-aware).

    Routes through the daemon's `/pause` endpoint when a board service is
    configured (thin client); otherwise reads the local file directly. The
    remote fetch fails SOFT (any error → empty set) — see module docstring
    for why reads stay fail-open while explicit `pause()`/`unpause()` calls
    don't.
    """
    svc = _resolve_service()
    if svc is not None:
        from coord.client import fetch_paused_machines  # noqa: PLC0415

        try:
            return fetch_paused_machines(svc)
        except Exception:  # noqa: BLE001 — fail-soft read, see module docstring
            return set()
    return local_paused_set()


def is_paused(name: str) -> bool:
    """Convenience: True when *name* is in the paused set."""
    return name in paused_set()


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


def unpause(name: str) -> bool:
    """Remove *name* from the paused set.  Returns True when the set
    changed (i.e. *name* was actually paused).  See `pause()` for the
    thin-client routing / fail-loudly contract.
    """
    svc = _resolve_service()
    if svc is not None:
        from coord.client import post_pause  # noqa: PLC0415

        result = post_pause(svc, name, "unpause")
        return bool(result.get("changed"))
    return local_unpause(name)


def _resolve_service():  # -> coord.client.ServiceConfig | None
    from coord.board_service import resolve  # noqa: PLC0415

    return resolve()


# ── local-only (#1563: always used by the daemon's own `/pause` endpoint and
# tick loop, and by every caller when no board service is configured) ───────


def local_paused_set() -> set[str]:
    """Read the current set of paused machine names from the local file.

    Returns an empty set when the file is missing or malformed —
    failure to read should never block routing, just degrade to
    "nothing is paused".  Callers wanting a strict view can call
    `_load_raw()` directly.

    Unlike `paused_set()`, this NEVER goes over the network — it is what
    the daemon's `/pause` endpoint handler calls, and what `paused_set()`
    itself falls through to when no board service is configured.
    """
    try:
        data = _load_raw()
    except (OSError, ValueError):
        return set()
    items = data.get("paused")
    if not isinstance(items, list):
        return set()
    return {str(x) for x in items if isinstance(x, str) and x}


def local_pause(name: str) -> bool:
    """Add *name* to the local paused set.  Returns True when the set
    changed (i.e. *name* was not already paused)."""
    current = local_paused_set()
    if name in current:
        return False
    current.add(name)
    _save(current)
    return True


def local_unpause(name: str) -> bool:
    """Remove *name* from the local paused set.  Returns True when the set
    changed (i.e. *name* was actually paused)."""
    current = local_paused_set()
    if name not in current:
        return False
    current.discard(name)
    _save(current)
    return True


# ── internals ────────────────────────────────────────────────────────────────


def _load_raw() -> dict:
    path = _state_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(names: set[str]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"paused": sorted(names)}
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
