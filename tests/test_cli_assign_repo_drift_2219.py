"""#2219: `coord assign` (`--dry-run` especially) must refuse a dispatch the
LIVE agent will reject, not just one `coordinator.yml` happens to still list.

Before this, every pre-flight surface — `coord config`, `coord status`, and
`coord assign ... --dry-run` — read config only. A repo added to
coordinator.yml after the target agent process started stayed invisible to
that agent (no partial config re-read short of a full restart), so all three
said the dispatch was fine while the live `/health` repo list, and the
agent's own `assign()`, disagreed. `--dry-run` green-lit a dispatch that
could not succeed, and the drive queue burned both retry attempts
discovering that by trial before landing terminally `blocked`.

`_repo_capability_refusal` (coord/commands/dispatch.py) is the fix: a
pre-flight cross-check against the SAME `/health` read `coord doctor`/`coord
status` already make, reused via `coord.network.check_machine`. The autouse
`_no_assign_repo_drift_probe` fixture (tests/conftest.py) stubs it to `None`
by default for every other test in the suite; these tests exercise it
directly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.commands.dispatch import _repo_capability_refusal
from coord.models import Machine
from coord.network import ONLINE, OFFLINE, MachineStatus

CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
  - name: stick-demo
    github: acme/stick-demo
    default_branch: main
machines:
  - name: dellserver
    host: dellserver.tailnet
    repos: [api, stick-demo]
    repo_paths:
      api: /tmp/api
      stick-demo: /tmp/stick-demo
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _machine(**kwargs) -> Machine:
    base = dict(
        name="dellserver", host="dellserver.tailnet",
        repos=["api", "stick-demo"],
    )
    base.update(kwargs)
    return Machine(**base)


class TestRepoCapabilityRefusalUnit:
    """Pure unit coverage of the cross-check helper (network mocked)."""

    def test_refuses_when_live_repos_exclude_the_target(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.network.check_machine",
            lambda *a, **k: MachineStatus(
                machine=_machine(), state=ONLINE, latency_ms=5.0,
                health={
                    "machine": "dellserver",
                    "repos": ["api"],  # stick-demo missing — the #2219 shape
                },
            ),
        )
        reason = _repo_capability_refusal(_machine(), "stick-demo")
        assert reason is not None
        assert "stick-demo" in reason
        assert "does not handle repo" in reason
        assert "['api']" in reason

    def test_none_when_live_repos_include_the_target(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.network.check_machine",
            lambda *a, **k: MachineStatus(
                machine=_machine(), state=ONLINE, latency_ms=5.0,
                health={"machine": "dellserver", "repos": ["api", "stick-demo"]},
            ),
        )
        assert _repo_capability_refusal(_machine(), "stick-demo") is None

    def test_none_when_agent_is_offline(self, monkeypatch) -> None:
        """A transient/unreachable agent must not become a NEW dispatch
        failure mode — that's still left to the POST itself."""
        monkeypatch.setattr(
            "coord.network.check_machine",
            lambda *a, **k: MachineStatus(
                machine=_machine(), state=OFFLINE, reason="connection refused",
            ),
        )
        assert _repo_capability_refusal(_machine(), "stick-demo") is None

    def test_none_when_agent_publishes_no_repos_key(self, monkeypatch) -> None:
        """An agent old enough to predate the `repos` field in /health."""
        monkeypatch.setattr(
            "coord.network.check_machine",
            lambda *a, **k: MachineStatus(
                machine=_machine(), state=ONLINE, latency_ms=5.0,
                health={"machine": "dellserver"},
            ),
        )
        assert _repo_capability_refusal(_machine(), "stick-demo") is None

    def test_degraded_repo_gets_its_own_reason_not_the_stale_config_story(
        self, monkeypatch
    ) -> None:
        """A repo missing from /health's `repos` because it's DEGRADED
        (#1527 — no repo_path entry, or the configured path doesn't exist)
        is not the #2219 stale-config shape: `AgentServer.assign()` gates
        on the UNFILTERED `self.repos`, which still contains a degraded
        repo, so it would NOT reject with "does not handle repo" — it
        would proceed and fail later with a distinct "repo path does not
        exist" error. The refusal message must say so and must not tell
        the operator to restart coord-agent, which cannot fix a missing
        repo_paths entry."""
        monkeypatch.setattr(
            "coord.network.check_machine",
            lambda *a, **k: MachineStatus(
                machine=_machine(), state=ONLINE, latency_ms=5.0,
                health={
                    "machine": "dellserver",
                    "repos": ["api"],
                    "degraded": {
                        "stick-demo": "no repo_path configured for this machine",
                    },
                },
            ),
        )
        reason = _repo_capability_refusal(_machine(), "stick-demo")
        assert reason is not None
        assert "no repo_path configured for this machine" in reason
        assert "does not handle repo" not in reason
        assert "hasn't re-read its config" not in reason
        assert "Restart coord-agent" not in reason

    def test_none_when_agent_is_config_free(self, monkeypatch) -> None:
        """#1801: a config-free agent's repos come from the dispatch
        payload, not its own config — a mismatch there is not a capability
        gap."""
        monkeypatch.setattr(
            "coord.network.check_machine",
            lambda *a, **k: MachineStatus(
                machine=_machine(), state=ONLINE, latency_ms=5.0,
                health={
                    "machine": "dellserver", "repos": ["api"],
                    "config_free": "no local coordinator.yml and no board service",
                },
            ),
        )
        assert _repo_capability_refusal(_machine(), "stick-demo") is None


class TestAssignRefusesOnLiveDrift:
    """CLI-level: `coord assign` (including --dry-run) must surface the
    live refusal before any network/claim work."""

    def test_dry_run_refuses_on_live_repo_drift(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch(
            "coord.commands.dispatch._repo_capability_refusal",
            return_value=(
                "'dellserver' rejected the assignment: this agent does not "
                "handle repo 'stick-demo' (supported: ['api'])"
            ),
        ), patch("coord.github_ops.get_issue") as mock_get_issue:
            result = CliRunner().invoke(
                main,
                [
                    "assign", "dellserver", "stick-demo", "1",
                    "--config", str(config_file), "--dry-run",
                ],
            )
        assert result.exit_code == 2
        assert "does not handle repo" in result.output
        assert "stick-demo" in result.output
        # #2219's whole point: refuse BEFORE spending the (harmless but
        # unnecessary) GitHub round-trip a dry run would otherwise still make.
        mock_get_issue.assert_not_called()

    def test_real_dispatch_also_refuses_on_live_repo_drift(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch(
            "coord.commands.dispatch._repo_capability_refusal",
            return_value=(
                "'dellserver' rejected the assignment: this agent does not "
                "handle repo 'stick-demo' (supported: ['api'])"
            ),
        ), patch("coord.github_ops.get_issue") as mock_get_issue, \
             patch("coord.dispatch.dispatch") as mock_dispatch:
            result = CliRunner().invoke(
                main,
                [
                    "assign", "dellserver", "stick-demo", "1",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 2
        mock_get_issue.assert_not_called()
        mock_dispatch.assert_not_called()

    def test_dry_run_proceeds_when_no_live_drift(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Sanity check: the default (autouse-stubbed) fail-open path still
        lets a genuinely healthy dispatch through --dry-run."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "dellserver", "stick-demo", "1",
                    "--config", str(config_file), "--dry-run",
                ],
            )
        assert result.exit_code == 0
        assert "dry run" in result.output
