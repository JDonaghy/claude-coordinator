"""Black-box tests for ``coord health`` and its JSON contract (#1628).

``--json``'s shape is what H-3 (board projection) and H-4 (renderers)
consume, so it is asserted directly and structurally rather than through a
snapshot — a field silently renamed here breaks two downstream children that
have not been written yet.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from coord.config import HealthConfig
from coord.health import registry
from coord.health.cli import EXIT_CRIT, EXIT_WARN, health
from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import Check, HealthReport
from coord.health.render import render_report, render_result


@pytest.fixture
def fake_checks(monkeypatch, tmp_path):
    """Replace the whole registry with two scripted checks and stub the context.

    The CLI must be testable without touching the machine it runs on — the
    point of these tests is the command's contract, not this laptop's disks.
    """
    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_discovered", True)

    registry.register(
        Check(
            id="disk",
            scope="machine",
            title="disk",
            order=1,
            probe=lambda ctx: CheckResult(
                check_id="disk", scope="machine", subject="/home", title="disk",
                severity=Severity.WARN, headroom="86% used (22G free)",
                threshold="crit at 93%", detail="free some space",
                values={"free_pct": 14.0},
            ),
        )
    )
    registry.register(
        Check(
            id="graph",
            scope="checkout",
            title="graph",
            order=2,
            probe=lambda ctx: CheckResult(
                check_id="graph", scope="checkout", subject="vimcode", title="graph",
                severity=Severity.CRIT,
                headroom="128.8h stale, hooks disabled -> will not self-heal",
                values={"age_hours": 128.8, "hooks_ok": False},
            ),
        )
    )
    registry.register(
        Check(
            id="net_thing",
            scope="machine",
            title="net thing",
            order=3,
            cost=registry.COST_NETWORK,
            probe=lambda ctx: CheckResult(
                check_id="net_thing", scope="machine", severity=Severity.OK,
                headroom="fine",
            ),
        )
    )

    thresholds = HealthConfig()

    def _build_context(config=None, **kwargs):
        return HealthContext(
            thresholds=thresholds,
            home=tmp_path,
            coord_dir=tmp_path / ".coord",
            now=0.0,
            allow_network=kwargs.get("allow_network", True),
        )

    monkeypatch.setattr("coord.health.cli.build_context", _build_context)
    monkeypatch.setattr("coord.health.cli._load_config_or_none", lambda p: None)
    return thresholds


def _run(*args):
    return CliRunner().invoke(health, list(args), catch_exceptions=False)


# ── text output ──────────────────────────────────────────────────────────────


def test_health_prints_one_line_per_check_with_headroom(fake_checks) -> None:
    result = _run("--local", "--no-network")
    assert result.exit_code == 0
    assert "disk /home" in result.output
    assert "WARN" in result.output
    assert "86% used (22G free)" in result.output
    assert "crit at 93%" in result.output
    assert "graph vimcode" in result.output
    assert "128.8h stale, hooks disabled -> will not self-heal" in result.output


def test_health_summary_trailer_is_greppable(fake_checks) -> None:
    result = _run("--no-network")
    assert "HEALTH: CRIT crit=1 warn=1 unknown=0 ok=0" in result.output


def test_health_lists_skipped_network_checks(fake_checks) -> None:
    """"We didn't look" must be visible, not silently absent."""
    result = _run("--no-network")
    assert "net_thing (network probe, --no-network)" in result.output
    assert "net thing" not in result.output.replace("net_thing", "")


def test_health_runs_network_checks_by_default(fake_checks) -> None:
    result = _run()
    assert "net thing" in result.output
    assert "skipped" not in result.output


def test_health_check_filter(fake_checks) -> None:
    result = _run("--check", "disk", "--no-network")
    assert "disk /home" in result.output
    assert "graph vimcode" not in result.output


def test_health_local_flag_excludes_fleet_scope(fake_checks) -> None:
    registry.register(
        Check(
            id="fleet_thing",
            scope="fleet",
            order=4,
            probe=lambda ctx: CheckResult(
                check_id="fleet_thing", scope="fleet",
                severity=Severity.OK, headroom="fleet ok",
            ),
        )
    )
    assert "fleet ok" not in _run("--local", "--no-network").output
    assert "fleet ok" in _run("--no-network").output


def test_health_disabled_reports_that_it_is_disabled(fake_checks) -> None:
    """A health check silently switched off looks identical to a healthy fleet."""
    fake_checks.enabled = False
    result = _run("--no-network")
    assert "disabled in coordinator.yml" in result.output
    assert "disk /home" not in result.output


# ── exit codes ───────────────────────────────────────────────────────────────


def test_health_exits_zero_by_default_even_on_crit(fake_checks) -> None:
    """Default is report-only; a bare `coord health` in a pipeline is safe."""
    assert _run("--no-network").exit_code == 0


def test_health_exit_code_flag_maps_severity(fake_checks) -> None:
    assert _run("--no-network", "--exit-code").exit_code == EXIT_CRIT
    assert _run("--no-network", "--exit-code", "--check", "disk").exit_code == EXIT_WARN
    assert _run("--no-network", "--exit-code", "--check", "nothing").exit_code == 0


# ── the JSON contract (H-3 / H-4 consume this) ───────────────────────────────


def test_health_json_shape(fake_checks) -> None:
    result = _run("--json", "--no-network")
    payload = json.loads(result.output)

    assert payload["schema"] == 1
    assert payload["severity"] == "crit"
    assert payload["counts"] == {"ok": 0, "warn": 1, "crit": 1, "unknown": 0}
    assert isinstance(payload["duration_secs"], float)
    assert payload["skipped"] == ["net_thing (network probe, --no-network)"]
    assert len(payload["results"]) == 2

    disk_row = payload["results"][0]
    assert set(disk_row) == {
        "key", "check_id", "scope", "subject", "title", "label",
        "severity", "headroom", "threshold", "detail", "trend",
        "values", "error",
    }
    assert disk_row["key"] == "disk:/home"
    assert disk_row["label"] == "disk /home"
    assert disk_row["scope"] == "machine"
    assert disk_row["severity"] == "warn"
    assert disk_row["headroom"] == "86% used (22G free)"
    assert disk_row["values"] == {"free_pct": 14.0}
    assert disk_row["error"] is None


def test_health_json_carries_rendered_headroom_so_renderers_need_no_thresholds(
    fake_checks,
) -> None:
    """The contract that stops H-4 from forking the severity logic.

    Every row must be renderable from ``severity`` + ``headroom`` alone. If a
    consumer had to read ``values`` and re-apply a threshold to produce a
    line, there would be two implementations of "what is WARN" the day the
    second renderer ships.
    """
    payload = json.loads(_run("--json", "--no-network").output)
    for row in payload["results"]:
        assert row["severity"] in {"ok", "warn", "crit", "unknown"}
        assert row["headroom"], f"{row['key']} has no rendered headroom"
        assert row["label"]


def test_health_json_is_valid_when_nothing_ran(fake_checks) -> None:
    payload = json.loads(_run("--json", "--check", "nope").output)
    assert payload["results"] == []
    assert payload["severity"] == "ok"


def test_health_without_a_config_still_runs_machine_checks(monkeypatch, tmp_path) -> None:
    """A machine with no coordinator.yml is the one most likely misconfigured."""
    from coord.config import ConfigError

    monkeypatch.setattr(
        "coord.config.load",
        lambda p: (_ for _ in ()).throw(ConfigError("Config file not found")),
    )
    result = CliRunner().invoke(
        health, ["--no-network", "--check", "claude_binary"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "HEALTH:" in result.output


# ── renderer ─────────────────────────────────────────────────────────────────


def test_render_result_omits_the_threshold_reminder_on_healthy_rows() -> None:
    """On an OK line the threshold is clutter; next to a bad number it's context."""
    ok = CheckResult(
        check_id="disk", scope="machine", subject="/", title="disk",
        severity=Severity.OK, headroom="20% used", threshold="crit at 93%",
    )
    assert "crit at 93%" not in render_result(ok)

    warn = CheckResult(
        check_id="disk", scope="machine", subject="/", title="disk",
        severity=Severity.WARN, headroom="86% used", threshold="crit at 93%",
    )
    assert "crit at 93%" in render_result(warn)


def test_render_report_shows_detail_for_unhealthy_rows_only() -> None:
    report = HealthReport(
        results=[
            CheckResult(check_id="a", scope="machine", title="a", severity=Severity.OK,
                        headroom="fine", detail="healthy detail"),
            CheckResult(check_id="b", scope="machine", title="b", severity=Severity.CRIT,
                        headroom="bad", detail="fix: do the thing"),
        ]
    )
    body = render_report(report)
    assert "fix: do the thing" in body
    assert "healthy detail" not in body
    assert "healthy detail" in render_report(report, verbose=True)


def test_render_report_handles_an_empty_run() -> None:
    assert "no checks ran" in render_report(HealthReport())


def test_render_report_widens_the_label_column_instead_of_truncating() -> None:
    """A clipped repo name costs more than a ragged right edge."""
    report = HealthReport(
        results=[
            CheckResult(check_id="a", scope="machine", title="short", subject="x",
                        severity=Severity.OK, headroom="fine"),
            CheckResult(check_id="b", scope="checkout", title="worktree clean",
                        subject="claude-coordinator", severity=Severity.OK,
                        headroom="clean"),
        ]
    )
    lines = render_report(report).splitlines()
    assert "worktree clean claude-coordinator" in lines[1]
    # Both severity tokens start at the same column.
    assert lines[0].index("OK") == lines[1].index("OK")


# ── #2137: the cargo GC's give-up signal reaches `coord health` ─────────────


def test_coord_health_renders_the_cargo_gc_over_cap_verdict(tmp_path, monkeypatch):
    """End-to-end through the real registry: the agent's last sweep said it
    could not get the shared cache under its cap, and an operator running
    ``coord health`` sees that — the whole defect in #2137 was a value the GC
    computed and no surface ever showed."""
    import time

    from coord import cargo_cache

    # Path.home() reads $HOME on POSIX, so this keeps the probe off the real
    # machine: it totals a cargo-target tree we made, not this laptop's.
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = tmp_path / ".coord" / "cargo-target" / "quadraui"
    cache.mkdir(parents=True)
    (cache / "blob").write_bytes(b"\0" * 4096)
    cargo_cache.write_gc_status(
        tmp_path / ".coord",
        {
            "cargo_cache_bytes": 38 * 1024**3,
            "cargo_over_cap": True,
            "cargo_over_cap_reason": "38.0G of 20.0G cap (18.0G over) — live build in quadraui",
            "cargo_prune_blocked": ["quadraui"],
        },
        now=time.time(),
    )

    result = CliRunner().invoke(
        health, ["--check", "cargo_targets", "--no-network", "--exit-code"]
    )

    assert "GC over cap" in result.output
    assert "could not get under cap" in result.output
    assert "HEALTH: WARN" in result.output
    assert result.exit_code == EXIT_WARN
