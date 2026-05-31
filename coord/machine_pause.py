"""Persistent pause/resume state for machines (#routing-pause).

The pause set is a tiny JSON file at ``~/.coord/paused_machines.json``
holding ``{"paused": [<name>, ...]}``.  Both the Python coordinator
(`coord plan`, `coord assign`, auto_loop, reconcile, review,
refine_chat) and the Rust TUI read it to decide whether a given
machine is a candidate for new work — paused machines stay reachable
and visible but never receive new assignments.

Pause does NOT cancel in-flight assignments; the user can `coord stop`
those separately if needed.  This module only governs the routing
decision for *new* work.

The file is small and human-editable, so the helpers are atomic via
tempfile-rename to avoid partial writes from concurrent commands.
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
    """Read the current set of paused machine names.

    Returns an empty set when the file is missing or malformed —
    failure to read should never block routing, just degrade to
    "nothing is paused".  Callers wanting a strict view can call
    `_load_raw()` directly.
    """
    try:
        data = _load_raw()
    except (OSError, ValueError):
        return set()
    items = data.get("paused")
    if not isinstance(items, list):
        return set()
    return {str(x) for x in items if isinstance(x, str) and x}


def is_paused(name: str) -> bool:
    """Convenience: True when *name* is in the paused set."""
    return name in paused_set()


def pause(name: str) -> bool:
    """Add *name* to the paused set.  Returns True when the set changed
    (i.e. *name* was not already paused)."""
    current = paused_set()
    if name in current:
        return False
    current.add(name)
    _save(current)
    return True


def unpause(name: str) -> bool:
    """Remove *name* from the paused set.  Returns True when the set
    changed (i.e. *name* was actually paused)."""
    current = paused_set()
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
