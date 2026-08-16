"""Shared mtime-guarded ``coordinator.yml`` reload (#1081, lifted in #2299).

Every long-lived coord process (``coord serve``, ``coord agent``) parses
``coordinator.yml`` exactly once, at startup, and then runs on that in-memory
snapshot forever. That is fine until an operator edits the file: from then on
the *file* and the *process* disagree, and nothing on the operator's side says
so — ``coord config``/``coord status``/``coord assign --dry-run`` all read the
file and cheerfully agree with the edit while the daemon quietly keeps acting
on the pre-edit snapshot.

:func:`reload_config_if_stale` is the one mechanism that closes that gap. It
was written for the board daemon in #1081 (``coord/serve_app.py``) and lifted
here verbatim in #2299 so the agent could reuse it rather than grow a second,
subtly-different copy. The behaviour is unchanged from #1081; only the logger
name and the human-facing prefix are parameterised, so each caller's journal
lines still read as that daemon's.

The contract, in one line: **a bad hand-edit must never take a running daemon
down, and must never be retried in a loop.**
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from coord.config import Config

__all__ = ["reload_config_if_stale"]


def reload_config_if_stale(
    current: "Config",
    last_mtime: float | None,
    *,
    log_name: str = "coord.serve",
    label: str = "coord serve",
) -> tuple["Config", float | None]:
    """Re-parse *current*'s backing ``coordinator.yml`` if it changed on disk (#1081).

    A daemon's in-memory ``Config`` is otherwise fixed at process startup, so
    a hand-edit to ``coordinator.yml`` on the daemon host silently diverges
    from the file until a restart — even though ``GET /config`` (which serves
    the raw bytes fresh every request) shows the new content immediately. This
    closes that gap for the daemon's *own* decisions by tracking the file's
    mtime and swapping in a freshly-parsed ``Config`` whenever a caller
    notices it moved.

    Returns ``(config, mtime)`` — either *current* unchanged (no backing path,
    a ``stat()`` failure, or no on-disk change since *last_mtime*) or a
    freshly-loaded ``Config`` paired with its new mtime. A malformed hand-edit
    (invalid YAML, a validation error, a permissions change, a TOCTOU race
    where the file vanishes between our ``stat()`` and ``load()``'s read, or a
    bad-encoding write caught mid-edit) is logged and swallowed rather than
    raised into a request handler or the tick loop — the daemon keeps serving
    the last-good config. *last_mtime* still advances past a bad edit so it
    isn't re-parsed (and re-logged) on every subsequent call; it will be
    retried once the file changes again (e.g. the edit is fixed).

    *log_name* / *label* only affect the journal lines (``coord.serve`` /
    ``"coord serve"`` for the board daemon, ``coord.agent`` / ``"coord
    agent"`` for the agent) — the reload semantics are identical for every
    caller by construction, which is the entire point of sharing this.
    """
    path = current.path
    if path is None:
        return current, last_mtime
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return current, last_mtime
    if last_mtime is not None and mtime <= last_mtime:
        return current, last_mtime

    from coord.config import load as _load_coordinator_config  # noqa: PLC0415

    log = logging.getLogger(log_name)
    try:
        reloaded = _load_coordinator_config(path)
    except Exception as e:  # noqa: BLE001 — a tick must never crash the daemon
        # Broad on purpose (#1081 review): load() isn't guaranteed to only raise
        # ConfigError — a TOCTOU race (file deleted/replaced between our stat()
        # and load()'s own read), a permissions change (OSError), a bad-encoding
        # write caught mid-edit (UnicodeDecodeError), or a malformed structure
        # tripping an un-validated code path deeper in the parser (AttributeError/
        # TypeError/KeyError) can all surface here. Swallowing them matches every
        # other tick-loop guard in the callers — an uncaught exception from this
        # helper would otherwise either 500 a /board (or /health) request or,
        # worse, permanently kill a bare `asyncio.create_task(...)` task with no
        # supervisor to restart it.
        log.warning(
            "%s: %s changed on disk but failed to reload (%s: %s); "
            "keeping last-good config until the file is fixed",
            label,
            path,
            type(e).__name__,
            e,
        )
        return current, mtime
    log.info("%s: reloaded %s (on-disk change detected)", label, path)
    return reloaded, mtime
