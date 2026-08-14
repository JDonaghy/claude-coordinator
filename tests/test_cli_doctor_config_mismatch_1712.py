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
    # #2082: default to --no-pypi so this stays hermetic — see the identical
    # comment in tests/test_cli_doctor.py.
    return runner.invoke(
        doctor,
        ["--config", str(config_path), "--no-pypi", *(extra_args or [])],
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

    def test_partial_repo_drift_is_a_problem_even_when_health_is_nonempty(self) -> None:
        """#2219: stick-demo's actual shape — /health still publishes
        something (unlike the all-repos-missing #1485 case above), just not
        the ONE repo added to coordinator.yml after this agent process
        started. The `not published_repos` branch above never fires here,
        so this needs its own check."""
        lines = _health_vs_config_lines(
            self._machine(repos=["vimcode", "quadraui", "stick-demo"]),
            {
                "capabilities": ["gtk", "rust", "python"],
                "repos": ["vimcode", "quadraui"],
            },
        )
        assert any(is_problem for is_problem, _ in lines)
        assert any(
            "CRIT repos" in text and "stick-demo" in text for _, text in lines
        )

    def test_partial_repo_drift_is_degraded_not_stale_when_in_degraded_dict(
        self,
    ) -> None:
        """#2219 follow-up: a repo missing from `/health`'s `repos` because
        it's DEGRADED (#1527 — no repo_path entry, or the configured path
        doesn't exist) is not the "hasn't re-read config" story —
        `AgentServer.assign()` gates on the UNFILTERED `self.repos`, which
        still contains a degraded repo, so it would NOT reject with "does
        not handle repo"; it would proceed and fail later with a distinct
        "repo path does not exist" error. `coord doctor` must say so and
        must not tell the operator to restart coord-agent, which cannot fix
        a missing/misconfigured repo_paths entry."""
        lines = _health_vs_config_lines(
            self._machine(repos=["vimcode", "quadraui", "stick-demo"]),
            {
                "capabilities": ["gtk", "rust", "python"],
                "repos": ["vimcode", "quadraui"],
                "degraded": {
                    "stick-demo": "no repo_path configured for this machine",
                },
            },
        )
        assert any(is_problem for is_problem, _ in lines)
        assert any(
            "stick-demo" in text and "no repo_path configured" in text
            for _, text in lines
        )
        joined = "\n".join(text for _, text in lines)
        assert "hasn't re-read them" not in joined
        assert "Restart coord-agent" not in joined

    def test_partial_repo_drift_mixed_stale_and_degraded_repos(self) -> None:
        """One repo genuinely stale (missing from /health, not degraded)
        and one degraded (missing from /health, present in `degraded`) —
        each must get its own accurate story, not be lumped together."""
        lines = _health_vs_config_lines(
            self._machine(repos=["vimcode", "quadraui", "stick-demo", "tui"]),
            {
                "capabilities": ["gtk", "rust", "python"],
                "repos": ["vimcode", "quadraui"],
                "degraded": {
                    "tui": "repo_path /home/x/src/tui does not exist",
                },
            },
        )
        joined = "\n".join(text for _, text in lines)
        assert "stick-demo" in joined and "hasn't re-read them" in joined
        assert "tui" in joined and "repo_path /home/x/src/tui does not exist" in joined
        # The stale-config CRIT's *drifted-repos* list — the part actually
        # blamed on staleness — must name only the truly-stale repo, not
        # the degraded one (which `declared_repos` also mentions, harmlessly,
        # in the same line's leading "coordinator.yml declares [...]" clause).
        stale_lines = [t for _, t in lines if "hasn't re-read them" in t]
        assert len(stale_lines) == 1
        assert "['stick-demo'] were added to config" in stale_lines[0]

    def test_partial_repo_drift_is_silent_when_agent_is_config_free(self) -> None:
        """A config-free agent's repos come from the dispatch payload, not
        its own coordinator.yml (#1801) — a mismatch against the
        coordinator's OWN machine entry for it is not a capability gap."""
        lines = _health_vs_config_lines(
            self._machine(repos=["vimcode", "quadraui", "stick-demo"]),
            {
                "capabilities": [],
                "repos": ["vimcode", "quadraui"],
                "config_free": "no local coordinator.yml and no board service",
            },
        )
        assert not any("stick-demo" in text for _, text in lines)

    def test_no_partial_drift_line_when_published_repos_match_declared(self) -> None:
        assert (
            _health_vs_config_lines(
                self._machine(repos=["vimcode", "quadraui"]),
                {
                    "capabilities": ["gtk", "rust", "python"],
                    "repos": ["vimcode", "quadraui", "extra-repo-agent-also-serves"],
                },
            )
            == []
        )
