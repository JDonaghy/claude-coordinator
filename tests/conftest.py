"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


VALID_CONFIG = """\
repos:
  - name: api
    github: acme/api
    depends_on: [shared]
  - name: shared
    github: acme/shared

machines:
  - name: laptop
    host: laptop.tailnet
    capabilities: [python]
    repos: [api, shared]
  - name: server
    host: server.tailnet
    capabilities: [python, docker]
    repos: [api]
"""


@pytest.fixture
def valid_config_yaml() -> str:
    return VALID_CONFIG


@pytest.fixture
def valid_config_path(tmp_path: Path, valid_config_yaml: str) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(valid_config_yaml)
    return p


@pytest.fixture(autouse=True)
def coord_db():
    """Isolated in-memory SQLite database, active for every test automatically.

    Overrides the module-level singleton in coord.db so that all state
    functions (save_board, load_board, record_dispatched, etc.) operate on a
    fresh :memory: database rather than the real ``~/.coord/coord.db``.

    autouse=True means no test needs to request this fixture explicitly —
    every test gets a clean DB and can never leak rows into the real database.
    Tests that need the connection object (e.g. to inspect raw rows) can still
    declare ``coord_db`` in their parameter list to receive it.
    """
    from coord import db
    from coord.db import _ensure_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db.override_connection(conn)
    yield conn
    db.close()
