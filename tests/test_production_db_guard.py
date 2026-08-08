"""#1960: a test (or a subprocess it spawns) must never be able to open the
live ``~/.coord/coord.db``.

Before this fix, ``coord.db.get_connection()``'s module-level singleton was
only isolated because the autouse ``coord_db`` fixture in
``tests/conftest.py`` overrides it with an in-memory connection before every
test body runs. Anything that reached ``coord.db._open(DB_PATH)`` directly --
a test that closes the override and lets the singleton fall back to the real
path, or a subprocess that re-imports ``coord.db`` fresh (pytest's
``PYTEST_CURRENT_TEST`` env var is inherited by child processes by default)
-- would silently write to the real database. That's exactly how the
``tests/test_retry.py``-shaped ``laptop``/``laptop.tailnet`` row and the
fixture ``assignments`` rows ended up in the production DB on dellserver.

These tests prove the new guard in ``coord.db._open`` fails loudly instead.
"""

from __future__ import annotations

import os

import pytest

from coord import db


def test_opening_the_production_db_path_raises_under_pytest() -> None:
    """Directly opening ``DB_PATH`` (bypassing the ``coord_db`` override, the
    way a stray direct call or a re-imported subprocess would) must refuse
    rather than silently touching ``~/.coord/coord.db``."""
    assert os.environ.get("PYTEST_CURRENT_TEST"), (
        "sanity check: this test only proves the guard fires while pytest's "
        "own env marker is set, which is how the guard detects test context"
    )

    with pytest.raises(db.ProductionDatabaseGuardError) as excinfo:
        db._open(db.DB_PATH)

    message = str(excinfo.value)
    assert str(db.DB_PATH) in message
    assert "1960" in message


def test_get_connection_raises_if_singleton_falls_back_to_production_path() -> None:
    """``get_connection()`` re-opens ``DB_PATH`` whenever the singleton is
    ``None`` -- simulate the fixture override being torn down mid-test (e.g.
    a test that calls ``db.close()`` itself) and confirm the fallback still
    refuses the real path instead of silently reopening it."""
    db.close()  # tears down the coord_db fixture's in-memory override early

    with pytest.raises(db.ProductionDatabaseGuardError):
        db.get_connection()


def test_guard_does_not_block_isolated_paths(tmp_path) -> None:
    """The guard is scoped to the exact production path -- any other path
    (what every test actually uses) must keep working unmodified."""
    conn = db._open(tmp_path / "coord.db")
    try:
        # A real, usable connection with the schema applied.
        conn.execute("SELECT 1 FROM machines")
    finally:
        conn.close()
