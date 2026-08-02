"""#1712: `coord doctor` must report a machine whose `/health` contradicts
`coordinator.yml` as a CRIT misconfiguration, not as an absence.

Before this, precision published `capabilities: []` / `repos: []` while the
config declared `['gtk','rust','python']` for it, and `coord doctor` rendered
that as an unremarkable two-line entry (`gh`, `git`) — a machine with no
capabilities is indistinguishable from a machine that legitimately has none.
That is how #1673 stayed "unexplained" while precision was silently
ineligible for every capability-matched Test dispatch.

Driven the same way tests/test_cli_doctor.py drives the command: mock
`coord.network.check_all` so the test is hermetic, then assert on real output
and the real exit code.
"""

from __future__ import annotations

from click.testing import CliRunner

import coord.network as network_mod
from coord.commands.status import _health_vs_config_lines, doctor
from coord.models import Machine
from coord.network import ONLINE, MachineStatus


def _run_doctor(config_path, monkeypatch, statuses, *, extra_args=None):
    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: statuses)
    runner = CliRunner()
    return runner.invoke(
        doctor,
        ["--config", str(config_path), *(extra_args or [])],
        catch_exceptions=False,
    )


def _ok_probe(capability: str | None = None) -> dict:
    return {
        "found": True, "version": "9.9.9", "min_version": None,
        "meets_floor": None, "capability": capability, "ok": True,
    }


_BASELINE_ONLY = {"git": _ok_probe(), "gh": _ok_probe()}


class TestDoctorCommand:
    def test_crit_when_health_publishes_no_capabilities_but_config_declares_some(
        self, valid_config_path, monkeypatch
    ) -> None:
        """The precision shape, verbatim: baseline probes only, empty
        capabilities, while coordinator.yml declares some."""
        from coord.config import load

        cfg = load(valid_config_path)
        laptop, server = cfg.machines
        statuses = [
            MachineStatus(
                machine=laptop, state=ONLINE, latency_ms=5.0,
                health={
                    "machine": "laptop", "capabilities": [], "repos": [],
                    "tool_versions": _BASELINE_ONLY,
                },
            ),
            MachineStatus(
                machine=server, state=ONLINE, latency_ms=5.0,
                health={
                    "machine": "server",
                    "capabilities": ["python", "docker"],
                    "repos": ["api"],
                    "tool_versions": {
                        **_BASELINE_ONLY, "python3": _ok_probe("python"),
                    },
                },
            ),
        ]

        result = _run_doctor(valid_config_path, monkeypatch, statuses)

        assert result.exit_code == 1, result.output
        assert "CRIT capabilities" in result.output
        assert "'python'" in result.output or "python" in result.output
        # The healthy machine must not be dragged in with it.
        assert result.output.count("CRIT capabilities") == 1

    def test_no_crit_when_published_capabilities_match(
        self, valid_config_path, monkeypatch
    ) -> None:
        from coord.config import load

        cfg = load(valid_config_path)
        statuses = [
            MachineStatus(
                machine=m, state=ONLINE, latency_ms=5.0,
                health={
                    "machine": m.name,
                    "capabilities": list(m.capabilities),
                    "repos": list(m.repos),
                    "tool_versions": {
                        **_BASELINE_ONLY, "python3": _ok_probe("python"),
                    },
                },
            )
            for m in cfg.machines
        ]

        result = _run_doctor(valid_config_path, monkeypatch, statuses)

        assert result.exit_code == 0, result.output
        assert "CRIT" not in result.output

    def test_crit_is_reported_even_when_the_agent_predates_tool_versions(
        self, valid_config_path, monkeypatch
    ) -> None:
        """The mismatch is the loudest thing doctor can say — it must not be
        skipped by the `no tool_versions` early-continue."""
        from coord.config import load

        cfg = load(valid_config_path)
        statuses = [
            MachineStatus(
                machine=m, state=ONLINE, latency_ms=5.0,
                health={"machine": m.name, "capabilities": [], "repos": []},
            )
            for m in cfg.machines
        ]

        result = _run_doctor(valid_config_path, monkeypatch, statuses)

        assert result.exit_code == 1, result.output
        assert "CRIT capabilities" in result.output


class TestHealthVsConfigLines:
    """Unit coverage of the pure cross-check helper."""

    def _machine(self, **kwargs) -> Machine:
        base = dict(
            name="precision", host="precision.tailnet",
            capabilities=["gtk", "rust", "python"], repos=["tui"],
        )
        base.update(kwargs)
        return Machine(**base)

    def test_empty_capabilities_against_declared_is_a_problem(self) -> None:
        lines = _health_vs_config_lines(
            self._machine(), {"capabilities": [], "repos": ["tui"]}
        )
        assert any(is_problem for is_problem, _ in lines)
        assert any("CRIT capabilities" in text for _, text in lines)

    def test_empty_repos_against_declared_is_a_problem(self) -> None:
        """#1485's shape: `repos: []` makes the machine look repo-less to any
        reader that trusts /health."""
        lines = _health_vs_config_lines(
            self._machine(),
            {"capabilities": ["gtk", "rust", "python"], "repos": []},
        )
        assert any("CRIT repos" in text for _, text in lines)

    def test_degraded_reasons_are_surfaced_with_the_repo_crit(self) -> None:
        lines = _health_vs_config_lines(
            self._machine(),
            {
                "capabilities": ["gtk", "rust", "python"],
                "repos": [],
                "degraded": {"tui": "repo_path /home/x/src/tui does not exist"},
            },
        )
        assert any("does not exist" in text for _, text in lines)

    def test_config_free_reason_is_quoted_in_the_crit_when_present(self) -> None:
        lines = _health_vs_config_lines(
            self._machine(),
            {
                "capabilities": [],
                "repos": [],
                "config_free": "no local coordinator.yml and no board service",
            },
        )
        assert any("running config-free" in text for _, text in lines)

    def test_upgrade_hint_when_the_agent_reports_no_config_free_marker(self) -> None:
        """An agent predating #1712 (precision today) publishes no
        `config_free` key at all — point the operator at the restart/upgrade,
        which is the actual remedy."""
        lines = _health_vs_config_lines(self._machine(), {"capabilities": []})
        joined = "\n".join(text for _, text in lines)
        assert "--machine" in joined
        assert "coord agent update" in joined

    def test_legitimately_config_free_machine_is_a_warning_not_a_problem(self) -> None:
        """An ephemeral worker declaring nothing and running config-free is
        working as designed — surface it, but do not fail the fleet report."""
        lines = _health_vs_config_lines(
            self._machine(capabilities=[], repos=[]),
            {
                "capabilities": [], "repos": [],
                "config_free": "no local coordinator.yml and no board service",
            },
        )
        assert lines
        assert not any(is_problem for is_problem, _ in lines)

    def test_matching_health_produces_no_lines(self) -> None:
        assert (
            _health_vs_config_lines(
                self._machine(),
                {"capabilities": ["gtk", "rust", "python"], "repos": ["tui"]},
            )
            == []
        )
