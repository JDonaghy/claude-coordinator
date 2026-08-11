"""#1801: `coord doctor` false-CRITs a config-free ephemeral worker as
undispatchable, and contradicts itself in its own detail line.

Evidence (azure-epic1709, #1708 proof run): `coordinator.yml` declared
`capabilities: ['rust', 'python']` and `repos: ['claude-coordinator']` for a
machine whose agent runs config-free (no local `coordinator.yml`, no board
service — capabilities/repos come from the coordinator at dispatch time).
`coord doctor` rendered that as TWO CRITs ("every dispatch to this machine
will be refused") whose own detail line said the opposite ("capabilities and
repos come from the coordinator at dispatch time"). Dispatch in fact worked
once `repo_paths` was added — the CRITs were simply wrong, and the check
never caught the two things that *actually* would have blocked dispatch:
a missing `repo_paths` entry and a missing `provider:*` capability.

Driven the same way tests/test_cli_doctor.py and
tests/test_cli_doctor_config_mismatch_1712.py drive the command: mock
`coord.network.check_all` so the test is hermetic, then assert on real
output and the real exit code. Pure-function coverage of the two helpers
(`_health_vs_config_lines`, `_dispatch_blocker_lines_for_config_free`) lives
alongside the CLI-level tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import coord.network as network_mod
from coord.commands.status import (
    _dispatch_blocker_lines_for_config_free,
    _health_vs_config_lines,
    doctor,
)
from coord.models import Machine, Repo
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

CONFIG_FREE_MACHINE_NAME = "azure-epic1709"

# The azure-epic1709 shape (#1708 evidence): a repo with NO provider
# override (implicit `claude`, so the provider-capability check below never
# fires) and a machine that DOES declare capabilities/repos in the
# coordinator's own coordinator.yml, plus a `repo_paths` entry so the new
# dispatch-blocker check doesn't fire either — isolates the CRIT-vs-WARN fix
# from the two new checks.
NO_CRIT_CONFIG = """\
repos:
  - name: claude-coordinator
    github: acme/claude-coordinator
machines:
  - name: azure-epic1709
    host: azure-epic1709.tailnet
    capabilities: [rust, python]
    repos: [claude-coordinator]
    repo_paths:
      claude-coordinator: /home/coord/src/claude-coordinator
"""

# Same machine, but coordinator.yml declares neither repo_paths NOR the
# provider:opencode capability the repo (via provider: opencode) needs —
# the two genuine blockers #1801 says the check missed.
BLOCKERS_CONFIG = """\
repos:
  - name: claude-coordinator
    github: acme/claude-coordinator
    provider: opencode
machines:
  - name: azure-epic1709
    host: azure-epic1709.tailnet
    capabilities: [rust, python]
    repos: [claude-coordinator]
providers:
  definitions:
    opencode:
      type: opencode
"""

# Only the repo_paths gap — provider stays implicit `claude` so the
# provider-capability check can't also fire and muddy the assertion.
MISSING_REPO_PATHS_ONLY_CONFIG = """\
repos:
  - name: claude-coordinator
    github: acme/claude-coordinator
machines:
  - name: azure-epic1709
    host: azure-epic1709.tailnet
    capabilities: [rust, python]
    repos: [claude-coordinator]
"""

# Only the provider gap — repo_paths is present so the repo_paths check
# can't also fire.
MISSING_PROVIDER_CAP_ONLY_CONFIG = """\
repos:
  - name: claude-coordinator
    github: acme/claude-coordinator
    provider: opencode
machines:
  - name: azure-epic1709
    host: azure-epic1709.tailnet
    capabilities: [rust, python]
    repos: [claude-coordinator]
    repo_paths:
      claude-coordinator: /home/coord/src/claude-coordinator
providers:
  definitions:
    opencode:
      type: opencode
"""


def _config_free_health(machine, *, capabilities=(), repos=()) -> dict:
    return {
        "machine": machine.name,
        "capabilities": list(capabilities),
        "repos": list(repos),
        "config_free": (
            "no local coordinator.yml at /home/coord/.coord/coordinator.yml "
            "and no board service configured — running config-free "
            "(capabilities and repos come from the coordinator at dispatch "
            "time)"
        ),
        "tool_versions": _BASELINE_ONLY,
    }


@pytest.fixture
def no_crit_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(NO_CRIT_CONFIG)
    return p


@pytest.fixture
def blockers_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(BLOCKERS_CONFIG)
    return p


@pytest.fixture
def missing_repo_paths_only_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(MISSING_REPO_PATHS_ONLY_CONFIG)
    return p


@pytest.fixture
def missing_provider_cap_only_config_path(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(MISSING_PROVIDER_CAP_ONLY_CONFIG)
    return p


class TestDoctorCommand:
    def test_config_free_agent_with_declared_caps_and_repos_is_not_a_crit(
        self, no_crit_config_path, monkeypatch,
    ) -> None:
        """The azure-epic1709 shape, verbatim: coordinator.yml declares
        capabilities/repos, the config-free agent's /health publishes none
        of them. Must NOT be a CRIT — this is designed behaviour, not a
        fault. FAILS against pre-#1801 code."""
        from coord.config import load

        cfg = load(no_crit_config_path)
        (machine,) = cfg.machines
        statuses = [
            MachineStatus(
                machine=machine, state=ONLINE, latency_ms=5.0,
                health=_config_free_health(machine),
            ),
        ]

        result = _run_doctor(no_crit_config_path, monkeypatch, statuses)

        assert "CRIT capabilities" not in result.output, result.output
        assert "CRIT repos" not in result.output, result.output
        assert "CRIT repo_paths" not in result.output, result.output
        assert "CRIT provider capability" not in result.output, result.output
        assert result.exit_code == 0, result.output
        # The explanation is still surfaced — just not as an alarm.
        assert "running config-free" in result.output

    def test_configured_agent_publishing_nothing_still_crits(
        self, no_crit_config_path, monkeypatch,
    ) -> None:
        """#1485/#1712 regression risk: a STANDING agent (no `config_free`
        marker) that publishes empty capabilities/repos despite a loadable
        coordinator.yml must still CRIT — the fix for #1801 must not soften
        this real silent-ineligibility case."""
        from coord.config import load

        cfg = load(no_crit_config_path)
        (machine,) = cfg.machines
        statuses = [
            MachineStatus(
                machine=machine, state=ONLINE, latency_ms=5.0,
                health={
                    "machine": machine.name, "capabilities": [], "repos": [],
                    "tool_versions": _BASELINE_ONLY,
                },
            ),
        ]

        result = _run_doctor(no_crit_config_path, monkeypatch, statuses)

        assert result.exit_code == 1, result.output
        assert "CRIT capabilities" in result.output
        assert "CRIT repos" in result.output

    def test_config_free_machine_missing_repo_paths_is_flagged(
        self, missing_repo_paths_only_config_path, monkeypatch,
    ) -> None:
        """The real blocker #1801 says the check missed: a declared repo
        with no `repo_paths` entry refuses every dispatch
        (`coord.dispatch.dispatch` raises `ValueError` on exactly this)."""
        from coord.config import load

        cfg = load(missing_repo_paths_only_config_path)
        (machine,) = cfg.machines
        statuses = [
            MachineStatus(
                machine=machine, state=ONLINE, latency_ms=5.0,
                health=_config_free_health(machine),
            ),
        ]

        result = _run_doctor(missing_repo_paths_only_config_path, monkeypatch, statuses)

        assert result.exit_code == 1, result.output
        assert "CRIT repo_paths" in result.output
        assert "claude-coordinator" in result.output
        # Must not also mis-fire the (fixed) #1712 CRIT for the same reason.
        assert "CRIT capabilities" not in result.output
        assert "CRIT repos" not in result.output

    def test_config_free_machine_missing_provider_capability_is_flagged(
        self, missing_provider_cap_only_config_path, monkeypatch,
    ) -> None:
        """The other real blocker: the repo expects `provider: opencode`
        but the machine never declared `provider:opencode` in its
        capabilities — the #1711 structural gate would refuse this at
        dispatch time."""
        from coord.config import load

        cfg = load(missing_provider_cap_only_config_path)
        (machine,) = cfg.machines
        statuses = [
            MachineStatus(
                machine=machine, state=ONLINE, latency_ms=5.0,
                health=_config_free_health(machine),
            ),
        ]

        result = _run_doctor(
            missing_provider_cap_only_config_path, monkeypatch, statuses,
        )

        assert result.exit_code == 1, result.output
        assert "CRIT provider capability" in result.output
        assert "provider:opencode" in result.output

    def test_config_free_machine_with_both_gaps_flags_both(
        self, blockers_config_path, monkeypatch,
    ) -> None:
        """The azure-epic1709 evidence in full: neither gap alone, both
        together — and no leftover false CRIT from #1712's check."""
        from coord.config import load

        cfg = load(blockers_config_path)
        (machine,) = cfg.machines
        statuses = [
            MachineStatus(
                machine=machine, state=ONLINE, latency_ms=5.0,
                health=_config_free_health(machine),
            ),
        ]

        result = _run_doctor(blockers_config_path, monkeypatch, statuses)

        assert result.exit_code == 1, result.output
        assert "CRIT repo_paths" in result.output
        assert "CRIT provider capability" in result.output
        assert "CRIT capabilities" not in result.output
        assert "CRIT repos" not in result.output


class TestHealthVsConfigLinesConfigFree:
    """Unit coverage of the (is_problem, line) severity fix in the pure
    `_health_vs_config_lines` helper."""

    def _machine(self, **kwargs) -> Machine:
        base = dict(
            name=CONFIG_FREE_MACHINE_NAME, host="azure-epic1709.tailnet",
            capabilities=["rust", "python"], repos=["claude-coordinator"],
        )
        base.update(kwargs)
        return Machine(**base)

    def test_config_free_capabilities_mismatch_is_not_a_problem(self) -> None:
        lines = _health_vs_config_lines(
            self._machine(),
            {
                "capabilities": [], "repos": ["claude-coordinator"],
                "config_free": "no local coordinator.yml",
            },
        )
        assert not any(is_problem for is_problem, _ in lines)
        assert any("CRIT" not in text and "capabilities" in text for _, text in lines)

    def test_config_free_repos_mismatch_is_not_a_problem(self) -> None:
        lines = _health_vs_config_lines(
            self._machine(),
            {
                "capabilities": ["rust", "python"], "repos": [],
                "config_free": "no local coordinator.yml",
            },
        )
        assert not any(is_problem for is_problem, _ in lines)

    def test_non_config_free_capabilities_mismatch_still_crits(self) -> None:
        """No `config_free` key at all (a standing agent) — the #1712
        behaviour must be untouched."""
        lines = _health_vs_config_lines(
            self._machine(), {"capabilities": [], "repos": ["claude-coordinator"]},
        )
        assert any(is_problem for is_problem, _ in lines)
        assert any("CRIT capabilities" in text for _, text in lines)

    def test_non_config_free_repos_mismatch_still_crits(self) -> None:
        lines = _health_vs_config_lines(
            self._machine(), {"capabilities": ["rust", "python"], "repos": []},
        )
        assert any(is_problem for is_problem, _ in lines)
        assert any("CRIT repos" in text for _, text in lines)


class TestDispatchBlockerLinesForConfigFree:
    """Unit coverage of the new pure `_dispatch_blocker_lines_for_config_free`
    helper — the two checks that actually determine dispatchability for a
    config-free machine, straight out of coordinator.yml."""

    def _cfg(self, *, repo_provider=None, provider_definitions=None):
        from coord.config import Config, ProviderDef, ProvidersConfig

        repo = Repo(
            name="claude-coordinator", github="acme/claude-coordinator",
            provider=repo_provider,
        )
        definitions = {}
        if provider_definitions:
            for name, ptype in provider_definitions.items():
                definitions[name] = ProviderDef(type=ptype)
        return Config(
            repos=[repo], machines=[],
            providers=ProvidersConfig(definitions=definitions),
        )

    def test_missing_repo_paths_is_flagged(self) -> None:
        machine = Machine(
            name=CONFIG_FREE_MACHINE_NAME, host="x",
            capabilities=["rust", "python"], repos=["claude-coordinator"],
        )
        lines = _dispatch_blocker_lines_for_config_free(machine, self._cfg())

        assert any(is_problem for is_problem, _ in lines)
        assert any("CRIT repo_paths" in text for _, text in lines)

    def test_present_repo_paths_is_not_flagged(self) -> None:
        machine = Machine(
            name=CONFIG_FREE_MACHINE_NAME, host="x",
            capabilities=["rust", "python"], repos=["claude-coordinator"],
            repo_paths={"claude-coordinator": "/home/coord/src/claude-coordinator"},
        )
        lines = _dispatch_blocker_lines_for_config_free(machine, self._cfg())

        assert not any("repo_paths" in text for _, text in lines)

    def test_missing_provider_capability_is_flagged(self) -> None:
        machine = Machine(
            name=CONFIG_FREE_MACHINE_NAME, host="x",
            capabilities=["rust", "python"], repos=["claude-coordinator"],
            repo_paths={"claude-coordinator": "/home/coord/src/claude-coordinator"},
        )
        cfg = self._cfg(
            repo_provider="opencode", provider_definitions={"opencode": "opencode"},
        )
        lines = _dispatch_blocker_lines_for_config_free(machine, cfg)

        assert any(is_problem for is_problem, _ in lines)
        assert any(
            "CRIT provider capability" in text and "provider:opencode" in text
            for _, text in lines
        )

    def test_present_provider_capability_is_not_flagged(self) -> None:
        machine = Machine(
            name=CONFIG_FREE_MACHINE_NAME, host="x",
            capabilities=["rust", "python", "provider:opencode"],
            repos=["claude-coordinator"],
            repo_paths={"claude-coordinator": "/home/coord/src/claude-coordinator"},
        )
        cfg = self._cfg(
            repo_provider="opencode", provider_definitions={"opencode": "opencode"},
        )
        lines = _dispatch_blocker_lines_for_config_free(machine, cfg)

        assert lines == []

    def test_implicit_claude_provider_needs_no_capability(self) -> None:
        """A repo with no `provider:` override resolves to the implicit
        `claude` provider — every machine is assumed to have the `claude`
        CLI, so this must never be flagged."""
        machine = Machine(
            name=CONFIG_FREE_MACHINE_NAME, host="x",
            capabilities=[], repos=["claude-coordinator"],
            repo_paths={"claude-coordinator": "/home/coord/src/claude-coordinator"},
        )
        lines = _dispatch_blocker_lines_for_config_free(machine, self._cfg())

        assert not any("provider capability" in text for _, text in lines)
