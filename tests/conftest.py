"""Shared pytest fixtures."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _non_terminal_work(monkeypatch):
    """#522: default ALL work to NON-terminal so any test that dispatches a
    review/fix never shells out to ``gh`` through the chokepoint guard
    (``dispatch_review`` / the auto-loop).  Tests exercising the guard re-patch
    ``coord.github_ops.work_is_terminal`` (or ``issue_is_closed`` /
    ``pr_is_merged``) to opt in.  ``test_github_ops`` tests the real helpers
    via captured references, so this module-attr stub does not affect them.
    """
    monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _no_board_service(monkeypatch, tmp_path):
    """#584/#590: keep board-service resolution UNSET by default so tests never
    pick up the dev machine's real ``~/.coord/client.toml`` or
    ``COORD_SERVICE_URL`` and try to hit a live daemon.  Tests that exercise the
    thin-client path opt in by monkeypatching ``coord.client.resolve_board_service``
    (or ``CLIENT_TOML`` / the env vars) themselves — that runs after this
    autouse fixture, so it wins.
    """
    import coord.client as _cc

    monkeypatch.delenv("COORD_SERVICE_URL", raising=False)
    monkeypatch.delenv("COORD_TOKEN", raising=False)
    monkeypatch.setattr(_cc, "CLIENT_TOML", tmp_path / "absent-client.toml")


@pytest.fixture(autouse=True)
def _no_agent_health_probe(monkeypatch):
    """#904: default the reviewer /health pre-filter to fail-open (``None``)
    so tests that don't pass ``health_checker=`` to ``dispatch_review``
    (nearly all of them) never make a real ``httpx.get(".../health")`` call.
    Previously this fell through to the real ``_fetch_agent_advertised_repos``,
    which resolved fast locally (NXDOMAIN) but made the suite's default
    behavior depend on network/DNS timing rather than being fully hermetic.
    Tests exercising the health-filter itself pass an explicit
    ``health_checker=`` to ``dispatch_review``, which takes priority over this
    default and is unaffected by this stub.
    """
    monkeypatch.setattr(
        "coord.review._fetch_agent_advertised_repos", lambda *a, **k: None
    )


@pytest.fixture(autouse=True)
def _no_worktree_writable_deny_scan(monkeypatch):
    """#1445 review: keep `check_worktree_writable`'s deny-rule scan from
    reading whatever `~/.claude/settings.json` happens to exist on the
    machine running pytest.

    `AgentServer.assign()` and `coord diagnose --orphan-worktrees`
    (`coord/commands/status.py`) both call `check_worktree_writable()` /
    `find_blocking_deny_rule()` with no `settings_files` override in
    production, which resolves to the real `Path.home() / ".claude" /
    "settings.json"`. Left unpatched, every existing `.assign()`-based test
    in `test_agent.py` (none of which pass an explicit override) would
    implicitly depend on that file's contents — exactly the class of bug
    `_no_board_service` and `_no_agent_health_probe` above already exist to
    prevent, and the fleet already has this exact deny-rule shape on
    dellserver (#1445 itself). Tests exercising the scan directly pass their
    own `settings_files=[...]` (or an explicit
    `worktree_writable_settings_files=` to `AgentServer(...)`), which bypasses
    this default entirely and is unaffected.
    """
    monkeypatch.setattr("coord.agent._default_deny_settings_files", lambda: [])


@pytest.fixture(autouse=True)
def _no_real_usage_probe(monkeypatch):
    """#1466: default the Max-plan usage-window probe to a real subprocess
    NEVER running in tests.  ``coord approve``'s pre-check and
    ``coord.drive.preflight`` (via an injected ``usage_prober=``, so it's
    unaffected by this fixture) call ``coord.usage_limits.get_plan_limits``,
    which shells out to ``claude -p "/usage"`` — on a machine that has
    ``claude`` on PATH (every dev/agent box in this fleet does) that is a
    REAL network call, exactly the class of non-hermetic dependency
    ``_no_agent_health_probe`` above already exists to prevent. Stubbed to
    ``status="unknown"`` — the gate's own fail-open behaviour — so the
    default is silent and identical to "no probe available" everywhere
    except the small number of tests exercising the gate directly, which
    monkeypatch ``coord.usage_limits.get_plan_limits`` (or pass their own
    ``usage_prober=``) themselves, taking priority over this default.
    """
    from coord.usage_limits import PlanLimits

    monkeypatch.setattr(
        "coord.usage_limits.get_plan_limits",
        lambda *a, **k: PlanLimits(status="unknown"),
    )


def output_and_stderr(result) -> str:
    """CLI text across click versions: newer click separates stderr; older
    mixes it into .output and raises on .stderr access."""
    try:
        err = result.stderr or ""
    except ValueError:
        err = ""
    return result.output + err


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
