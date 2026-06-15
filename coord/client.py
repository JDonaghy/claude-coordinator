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

from coord._board_mapping import infer_review_state, row_to_assignment
from coord.models import Board

COORD_DIR = Path.home() / ".coord"
CLIENT_TOML = COORD_DIR / "client.toml"
# Local cache of the daemon-served coordinator.yml so the existing
# coord.config.load() parser can consume it unchanged (config.py has no dict
# round-trip — raw YAML is the lossless contract).
REMOTE_CONFIG_CACHE = COORD_DIR / "coordinator.remote.yml"

_DEFAULT_TIMEOUT = 5.0


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

    Mirrors ``coord.state._query_board`` + ``build_board`` (incl. the shared
    ``infer_review_state`` core) so the remote board matches a locally-built one.
    """
    assignments_raw = payload.get("assignments", [])
    plans = payload.get("plans") or {}
    active: list = []
    completed: list = []
    for d in assignments_raw:
        a = row_to_assignment(d)
        if a.assignment_id and a.assignment_id in plans:
            a.plan = plans[a.assignment_id]
        (active if a.status in ("running", "pending") else completed).append(a)
    board = Board(
        active=active,
        completed=completed,
        round_number=int(payload.get("round_number") or 0),
    )
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


def fetch_remote_config(svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT) -> Path:
    """GET /config, cache it to ``~/.coord/coordinator.remote.yml``, return the path.

    The caller feeds the returned path to ``coord.config.load()``.
    """
    resp = httpx.get(f"{svc.url}/config", headers=_headers(svc), timeout=timeout)
    resp.raise_for_status()
    COORD_DIR.mkdir(parents=True, exist_ok=True)
    REMOTE_CONFIG_CACHE.write_text(resp.text)
    return REMOTE_CONFIG_CACHE
