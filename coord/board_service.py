"""``BoardService`` facade (#749): decide local-vs-daemon ONCE, for both reads
and writes, instead of re-implementing the ``resolve_board_service()`` +
local-fallback dance at every call site.

Before this module, ~30 call sites across ``coord/commands/*.py``,
``coord/dashboard/server.py`` and ``coord/auto_loop.py`` each hand-rolled:

    svc = resolve_board_service()
    board = fetch_remote_board(svc) if svc else (load_board() or build_board())
    ...mutate board...
    save_board(board)  # silently a no-op on a thin client — the #749 bug

``read_board()`` / ``write_board()`` below are the one place that decision is
made. ``write_board()`` POSTs to the daemon's generic ``/board`` upsert
endpoint when ``board_service`` is configured, so a thin client's mutation
actually reaches the shared DB instead of vanishing into an empty local one.

This module also hosts ``route_write()``, which centralizes the
``resolve_board_service()`` + ``coord.client.post_record`` dance duplicated
~13x across ``coord.state``'s per-mutation routing wrappers
(``record_dispatched``, ``record_test_verdict``, ``update_issue_labels``,
…) — so ``coord.state`` depends on this thin facade rather than importing
``coord.client`` directly at over a dozen sites (#749's "reduce state.py's
outward coupling toward a pure DAO").

Whole-command re-routing (``coord merge``, ``coord reconcile-merges``,
``coord diagnose``, ``coord housekeeping`` — where the ENTIRE command runs on
the daemon and streams back its own textual/structured output, rather than a
generic board upsert) is a different shape and stays in its own commands;
``daemon_reroute_target()`` here only DRYs up the repeated
"resolve + check the re-entrancy env guard" preamble those four call sites
share.
"""

from __future__ import annotations

import os

from coord.models import Board


def resolve():  # -> coord.client.ServiceConfig | None
    """Resolve the configured board service, or ``None`` for local/host mode."""
    from coord.client import resolve_board_service  # noqa: PLC0415

    return resolve_board_service()


def is_remote() -> bool:
    """Whether this process is a thin client (``board_service`` configured)."""
    return resolve() is not None


def read_board() -> Board:
    """Return the current board.

    Daemon-configured → GET /board and reconstruct it. Otherwise → the local
    DB (``load_board()``, falling back to ``build_board()`` when the board has
    never been saved). This is the single place that decision is made; every
    call site that used to duplicate
    ``fetch_remote_board(svc) if svc else (load_board() or build_board())``
    should call this instead.
    """
    svc = resolve()
    if svc is not None:
        from coord.client import fetch_remote_board  # noqa: PLC0415

        return fetch_remote_board(svc)
    from coord.state import build_board, load_board  # noqa: PLC0415

    return load_board() or build_board()


def write_board(board: Board) -> None:
    """Persist *board*.

    Daemon-configured → POST the full board to the daemon's ``/board`` upsert
    endpoint (safe: upsert-only, never deletes — see
    ``coord.client.serialize_board``). Otherwise → the local
    ``coord.state.save_board``. Every call site that used to call
    ``save_board(board)`` directly (and silently no-op on a thin client)
    should call this instead.
    """
    svc = resolve()
    if svc is not None:
        from coord.client import post_board  # noqa: PLC0415

        post_board(svc, board)
        return
    from coord.state import save_board  # noqa: PLC0415

    save_board(board)


def route_write(
    svc,
    endpoint: str,
    payload: dict,
    *,
    timeout: float | None = None,
) -> dict | None:
    """POST *payload* to *endpoint* via *svc*, or ``None`` if *svc* is unset.

    Centralizes the ``from coord.client import post_record`` + call dance
    duplicated across ``coord.state``'s ``record_*`` / ``update_*`` routing
    wrappers, so ``coord.state`` no longer needs its own deferred
    ``coord.client`` import at each of those ~13 sites. Callers keep their own
    ``svc = board_service.resolve()`` (they need the value regardless, to
    decide whether to fall through to the local ``_*_local`` path) — this just
    does the "POST it, or tell the caller there's nothing to POST to" part.

    Returns ``None`` when *svc* is ``None`` (caller should run the local
    path); otherwise returns the daemon's JSON response (never ``None``,
    even for an empty ``{}`` body) so callers can distinguish "routed" from
    "not routed" with a plain ``is not None`` check.
    """
    if svc is None:
        return None
    from coord.client import post_record  # noqa: PLC0415

    if timeout is None:
        return post_record(svc, endpoint, payload)
    return post_record(svc, endpoint, payload, timeout=timeout)


def daemon_reroute_target(env_var: str):  # -> coord.client.ServiceConfig | None
    """Resolve the board service for a whole-command daemon re-route, or
    ``None`` if the command should run locally.

    Shared preamble for the four commands (``merge``, ``reconcile-merges``,
    ``diagnose``, ``housekeeping``) that route their ENTIRE execution to the
    daemon rather than doing a generic board upsert: each sets *env_var*
    (e.g. ``COORD_MERGE_ON_DAEMON``) before re-invoking itself on the daemon
    side, so the daemon's own execution doesn't try to re-route back out over
    HTTP. Returns ``None`` (meaning "run locally") both when no board service
    is configured AND when *env_var* is set (i.e. we ARE the daemon running
    the re-routed-to invocation).
    """
    if os.environ.get(env_var):
        return None
    return resolve()
