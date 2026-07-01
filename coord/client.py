"""Thin-client access to a remote ``coord serve`` daemon (#584/#594).

When a ``board_service`` is configured, the ``coord`` CLI (and, in spirit, any
Python consumer) reads the board + config from the daemon over Tailscale instead
of opening a local ``~/.coord/coord.db`` / ``coordinator.yml``.  When it is NOT
configured, callers fall back to the existing local-SQLite path — byte-identical
behaviour, no regression.

Bootstrap contract (kubeconfig-style), resolution order **flag > env > file**:

* ``--service`` / ``--token`` flags (passed by the caller),
* ``COORD_SERVICE_URL`` / ``COORD_TOKEN`` environment variables,
* ``~/.coord/client.toml`` with ``board_service = "http://host:7435"`` and an
  optional ``token = "..."``.

The client carries **no** Claude or gh credentials — it only reads the board and
config.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import httpx

from coord._board_mapping import assemble_board, infer_review_state
from coord.models import Board

COORD_DIR = Path.home() / ".coord"
CLIENT_TOML = COORD_DIR / "client.toml"
# Local cache of the daemon-served coordinator.yml so the existing
# coord.config.load() parser can consume it unchanged (config.py has no dict
# round-trip — raw YAML is the lossless contract).
REMOTE_CONFIG_CACHE = COORD_DIR / "coordinator.remote.yml"

_DEFAULT_TIMEOUT = 5.0
# Writes can post a GitHub comment synchronously on the daemon, so give them a
# longer ceiling than read GETs.
_WRITE_TIMEOUT = 30.0


@dataclass(frozen=True)
class ServiceConfig:
    url: str
    token: str | None = None


def resolve_board_service(
    flag_url: str | None = None,
    flag_token: str | None = None,
) -> ServiceConfig | None:
    """Resolve the board service per the bootstrap contract, or ``None`` if unset."""
    url = flag_url or os.environ.get("COORD_SERVICE_URL")
    token = flag_token or os.environ.get("COORD_TOKEN")
    if not url and CLIENT_TOML.exists():
        try:
            data = tomllib.loads(CLIENT_TOML.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        url = data.get("board_service")
        token = token or data.get("token")
    if not url:
        return None
    return ServiceConfig(url=url.rstrip("/"), token=token)


def _headers(svc: ServiceConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {svc.token}"} if svc.token else {}


def fetch_board_payload(svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """GET /board → the raw projection dict (also carries machines/merge_queue/…)."""
    resp = httpx.get(f"{svc.url}/board", headers=_headers(svc), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def board_from_payload(payload: dict) -> Board:
    """Reconstruct a :class:`Board` from a ``/board`` payload.

    Delegates row→Board assembly to ``coord._board_mapping.assemble_board`` —
    the same core ``coord.state._query_board`` uses — so the remote board
    can't drift from a locally-built one (#749: the dual state.py/client.py
    projection is now one shared implementation).
    """
    assignments_raw = payload.get("assignments", [])
    plans = payload.get("plans") or {}
    board = assemble_board(assignments_raw, plans, payload.get("round_number") or 0)
    review_rows = [
        {
            "assignment_id": d.get("assignment_id"),
            "review_of_assignment_id": d.get("review_of_assignment_id"),
            "status": d.get("status"),
        }
        for d in assignments_raw
        if d.get("type") == "review" and d.get("review_of_assignment_id")
    ]
    notified_ids = {n.get("assignment_id") for n in payload.get("notifications", [])}
    infer_review_state(board, review_rows, notified_ids)
    return board


def fetch_remote_board(svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT) -> Board:
    """GET /board and reconstruct a :class:`Board`."""
    return board_from_payload(fetch_board_payload(svc, timeout=timeout))


def serialize_board(board: Board) -> dict:
    """Serialize *board* for ``POST /board`` — the inverse of :func:`board_from_payload`.

    Only ships what ``coord.state.save_board`` actually persists: assignment
    rows (``dataclasses.asdict``, so JSON columns are native lists/dicts —
    tolerated by ``row_to_assignment`` on the way back in) + ``round_number``.
    ``save_board`` is upsert-only (never deletes), so POSTing a client's full
    in-memory board is a safe, non-lossy drop-in for what today runs directly
    against the local DB (#749).
    """
    from dataclasses import asdict  # noqa: PLC0415

    return {
        "assignments": [asdict(a) for a in board.active + board.completed],
        "round_number": board.round_number,
    }


def post_board(svc: ServiceConfig, board: Board, *, timeout: float = _WRITE_TIMEOUT) -> None:
    """POST the full board to the daemon's generic upsert endpoint (#749).

    Backs ``coord.board_service.write_board`` for the commands that still
    read-modify-write the whole board locally (``assign``/``approve``/``stop``/
    ``retry``/``resume``/``bounce``/``done``/``pr``/… and the dashboard/auto_loop
    call sites) — replacing a local ``save_board`` that used to silently no-op
    on a thin client.
    """
    post_record(svc, "/board", serialize_board(board), timeout=timeout)


def post_record(
    svc: ServiceConfig,
    path: str,
    payload: dict,
    *,
    timeout: float = _WRITE_TIMEOUT,
) -> dict:
    """POST a serialized seam record to the daemon and return the JSON outcome.

    Used by the ``coord.issue_store`` seam (#590): when ``board_service`` is
    set, ``post_result`` / ``post_completion`` POST the record here instead of
    writing the local DB, so a thin client's result lands on the one shared DB.
    Raises ``httpx.HTTPError`` on transport/HTTP failure — the seam decides
    whether that is fatal (``post_result``) or best-effort (``post_completion``).
    """
    resp = httpx.post(
        f"{svc.url}{path}", json=payload, headers=_headers(svc), timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()


def fetch_remote_config(svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT) -> Path:
    """GET /config, cache it to ``~/.coord/coordinator.remote.yml``, return the path.

    The caller feeds the returned path to ``coord.config.load()``.
    """
    resp = httpx.get(f"{svc.url}/config", headers=_headers(svc), timeout=timeout)
    resp.raise_for_status()
    COORD_DIR.mkdir(parents=True, exist_ok=True)
    REMOTE_CONFIG_CACHE.write_text(resp.text)
    return REMOTE_CONFIG_CACHE


def fetch_issue_context(
    svc: ServiceConfig,
    repo_name: str,
    issue_number: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict]:
    """GET an issue's raw context entries from the daemon (#603).

    Returns the ``entries`` list (oldest-first); ``[]`` on ANY failure (404 from
    an older daemon, network error, bad JSON).  Fail-soft on purpose: this rides
    the briefing read-path, so a missing/old daemon must degrade to "no context
    block", never crash a dispatch.
    """
    try:
        resp = httpx.get(
            f"{svc.url}/issue-context",
            params={"repo_name": repo_name, "issue_number": issue_number},
            headers=_headers(svc),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 — fail-soft; briefing just omits the block
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []
