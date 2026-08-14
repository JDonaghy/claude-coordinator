"""Black-box tests: drive ``coord health`` end to end for the graph check (#1728).

Unlike ``tests/test_health_cli.py`` (which scripts a *fake* ``graph`` check to
pin the CLI's generic contract) and ``tests/test_health_checks.py`` (which
calls ``graph.probe_graph`` directly), these tests invoke the real
``coord health`` command with the real, registry-discovered ``graph`` check
and assert on the rendered text — the surface an operator actually reads.
Only ``coord.graph_health``'s two entry points are stubbed, exactly as
``coord diagnose --graph`` would see them; nothing here re-derives severity.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from coord.config import HealthConfig
from coord.health.cli import health
from coord.health.models import Checkout, HealthContext


def _run(*args):
    return CliRunner().invoke(health, list(args), catch_exceptions=False)


def _stub(monkeypatch, tmp_path, *, status, hooks=(True, "core.hooksPath=.githooks")):
    """Wire ``coord health`` to one fake checkout and one scripted graph_status."""
    monkeypatch.setattr(
        "coord.graph_health.graph_status", lambda p, default_branch="main": status
    )
    monkeypatch.setattr("coord.graph_health.hooks_path_status", lambda p: hooks)

    thresholds = HealthConfig()

    def _build_context(config=None, **kwargs):
        return HealthContext(
            thresholds=thresholds,
            home=tmp_path,
            coord_dir=tmp_path / ".coord",
            now=1_800_000_000.0,
            checkouts=(Checkout(name="testrepo", path=tmp_path),),
            allow_network=kwargs.get("allow_network", True),
        )

    monkeypatch.setattr("coord.health.cli.build_context", _build_context)
    monkeypatch.setattr("coord.health.cli._load_config_or_none", lambda p: None)


def _status(**kwargs):
    from coord.graph_health import GraphStatus

    st = GraphStatus(repo_path=Path("/repo"))
    for key, value in kwargs.items():
        setattr(st, key, value)
    return st


def test_coord_health_reports_graph_in_sync_as_ok(tmp_path, monkeypatch) -> None:
    _stub(
        monkeypatch,
        tmp_path,
        status=_status(present=True, built_sha="abc12345", head_sha="abc12345",
                        in_sync=True, age_seconds=3600.0),
    )
    result = _run("--check", "graph", "--no-network")
    assert result.exit_code == 0
    assert "graph testrepo" in result.output
    assert "OK" in result.output
    assert "in sync" in result.output
    assert "HEALTH: OK" in result.output


def test_coord_health_reports_graph_stale_as_warn(tmp_path, monkeypatch) -> None:
    _stub(
        monkeypatch,
        tmp_path,
        status=_status(present=True, built_sha="aaaa1111", head_sha="bbbb2222",
                        in_sync=False, age_seconds=30 * 3600.0),
    )
    result = _run("--check", "graph", "--no-network")
    assert "graph testrepo" in result.output
    assert "WARN" in result.output
    assert "stale" in result.output
    assert "HEALTH: WARN" in result.output


def test_coord_health_reports_absent_graph_distinctly_from_stale(
    tmp_path, monkeypatch
) -> None:
    _stub(
        monkeypatch,
        tmp_path,
        status=_status(present=False, unknown_reason="no graphify-out/graph.json"),
    )
    result = _run("--check", "graph", "--no-network")
    assert "graph testrepo" in result.output
    assert "WARN" in result.output
    assert "no graph built here" in result.output
    assert "stale" not in result.output


def test_coord_health_reports_topology_unchanged_as_ok_not_stale(
    tmp_path, monkeypatch
) -> None:
    """graphify leaves outputs untouched when the tree's topology hasn't
    changed — the stamp is behind HEAD but the content is current, and this
    must render OK, not WARN, or the probe cries wolf on every healthy
    no-op run."""
    _stub(
        monkeypatch,
        tmp_path,
        status=_status(present=True, built_sha="aaaa1111", head_sha="bbbb2222",
                        in_sync=False, age_seconds=200 * 3600.0,
                        verified_at=2000.0, head_committed_at=1000.0),
    )
    result = _run("--check", "graph", "--no-network")
    assert "graph testrepo" in result.output
    assert "OK" in result.output
    assert "content current" in result.output
    assert "HEALTH: OK" in result.output


def test_coord_health_exit_code_flag_reflects_graph_severity(
    tmp_path, monkeypatch
) -> None:
    """A stale, hooks-disabled graph is CRIT — ``--exit-code`` must surface it
    so a timer can page on it without parsing text."""
    from coord.health.cli import EXIT_CRIT

    _stub(
        monkeypatch,
        tmp_path,
        status=_status(present=True, built_sha="aaaa1111", head_sha="bbbb2222",
                        in_sync=False, age_seconds=200 * 3600.0),
        hooks=(False, "core.hooksPath is unset"),
    )
    result = _run("--check", "graph", "--no-network", "--exit-code")
    assert result.exit_code == EXIT_CRIT
