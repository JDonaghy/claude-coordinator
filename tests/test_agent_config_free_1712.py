"""#1712: `coord agent --machine NAME` on a host with no LOCAL coordinator.yml
must still obtain its config from the daemon.

The bug was a branch-ORDER error, not a missing feature: `_start_agent_server`
tested ``not config_path.exists() and machine_name`` *first*, so config-free
mode was entered before the thin-client daemon fetch (#1080, inside
``_load_config``) was ever attempted. Two machines with byte-identical config
availability (no local file, daemon reachable) published different
capabilities purely because one systemd unit passed ``--machine`` and the
other didn't — precision came up with ``capabilities: []``, silently
ineligible for every ``rust``/``python``/``gtk`` dispatch, and nothing
anywhere reported an error (#1673).

THE ASSERTION THAT WOULD HAVE CAUGHT IT is
``test_machine_flag_does_not_change_which_config_source_is_used``: given the
same config availability, passing ``--machine`` must not change the config
source or the published capabilities.

The other half of the contract is that genuine config-free mode — no local
file AND no board service, the ephemeral Azure worker of
docs/EPHEMERAL_WORKERS.md — keeps working, publishing ``capabilities=[]``
without crashing. Breaking that strands the ephemeral-worker flow, so it is
asserted here too.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import coord.commands.agent_ops as agent_ops
from coord.agent import AgentServer
from coord.client import ServiceConfig
from coord.commands.agent_ops import _resolve_agent_startup


def _thin_client(monkeypatch, served_config: Path) -> list[ServiceConfig]:
    """Make this host look like a thin client whose daemon serves
    ``served_config``. Returns the list the fetch appends to, so a test can
    assert the daemon was actually *reached* — not merely reachable."""
    svc = ServiceConfig(url="http://daemon.tailnet:7435", token=None)
    fetches: list[ServiceConfig] = []

    def _fetch(service, **_kwargs):
        fetches.append(service)
        return served_config

    monkeypatch.setattr("coord.client.resolve_board_service", lambda *a, **k: svc)
    monkeypatch.setattr("coord.client.fetch_remote_config", _fetch)
    return fetches


def _absent(tmp_path: Path) -> Path:
    """A coordinator.yml path that does not exist — the real fleet shape:
    both elitebook and precision resolve to ``~/.coord/coordinator.yml``,
    which exists on neither."""
    p = tmp_path / "no-such-dir" / "coordinator.yml"
    assert not p.exists()
    return p


class TestDaemonFetchIsTriedBeforeConfigFree:
    def test_machine_flag_with_no_local_file_still_fetches_from_the_daemon(
        self, tmp_path: Path, valid_config_path: Path, monkeypatch
    ) -> None:
        """The direct regression: --machine + no local file + a reachable
        daemon must publish the machine's DECLARED capabilities."""
        fetches = _thin_client(monkeypatch, valid_config_path)

        startup = _resolve_agent_startup(_absent(tmp_path), "server")

        assert fetches, "the daemon config fetch was never attempted"
        assert startup.machine.name == "server"
        assert startup.machine.capabilities == ["python", "docker"]
        assert startup.machine.repos == ["api"]
        # Not config-free — nothing to warn about.
        assert startup.config_free_reason is None

    def test_machine_flag_does_not_change_which_config_source_is_used(
        self, tmp_path: Path, valid_config_path: Path, monkeypatch
    ) -> None:
        """#1712's root assertion. Same config availability, with and without
        the flag → same source, same published capabilities. Only the flag
        differed between elitebook (correct) and precision (broken)."""
        fetches = _thin_client(monkeypatch, valid_config_path)
        monkeypatch.setattr(agent_ops.socket, "gethostname", lambda: "server")
        absent = _absent(tmp_path)

        with_flag = _resolve_agent_startup(absent, "server")
        without_flag = _resolve_agent_startup(absent, None)

        assert len(fetches) == 2, "both invocations must hit the same source"
        assert with_flag.machine.name == without_flag.machine.name == "server"
        assert (
            with_flag.machine.capabilities
            == without_flag.machine.capabilities
            == ["python", "docker"]
        )
        assert with_flag.config_free_reason == without_flag.config_free_reason is None

    def test_local_file_present_is_still_loaded_when_no_board_service(
        self, valid_config_path: Path, monkeypatch
    ) -> None:
        """Unchanged behaviour on the daemon host: no board service, a real
        local file, --machine given → load the file (the `_no_board_service`
        autouse fixture already guarantees svc is None here)."""
        startup = _resolve_agent_startup(valid_config_path, "laptop")

        assert startup.machine.capabilities == ["python"]
        assert startup.health_config is not None
        assert startup.config_free_reason is None


class TestGenuineConfigFreeStillWorks:
    def test_no_local_file_and_no_board_service_starts_config_free(
        self, tmp_path: Path
    ) -> None:
        """docs/EPHEMERAL_WORKERS.md depends on this path: an Azure worker
        with nothing on disk and no daemon configured must still come up."""
        startup = _resolve_agent_startup(_absent(tmp_path), "ephemeral-1")

        assert startup.machine.name == "ephemeral-1"
        assert startup.machine.capabilities == []
        assert startup.machine.repos == []
        assert startup.machine.repo_paths == {}
        assert startup.health_config is None
        assert startup.providers == {}

    def test_config_free_startup_is_usable_by_the_server_constructor(
        self, tmp_path: Path
    ) -> None:
        """Config-free mode must not crash `_start_agent_server` — it reads
        `concurrency.bash_wrap_spawn` / `.first_output_timeout` off whatever
        this returns."""
        startup = _resolve_agent_startup(_absent(tmp_path), "ephemeral-1")

        assert isinstance(startup.concurrency.bash_wrap_spawn, bool)
        assert isinstance(startup.concurrency.first_output_timeout, (int, float))

    def test_config_free_says_so_loudly_instead_of_silently(
        self, tmp_path: Path
    ) -> None:
        """#1671's startup diagnostics iterate over `machine.capabilities`, so
        with an empty list they print NOTHING in exactly the case that most
        needs a signal. Config-free mode carries its own notice + a reason
        that rides into /health."""
        startup = _resolve_agent_startup(_absent(tmp_path), "ephemeral-1")

        assert startup.config_free_reason
        assert "no board service" in startup.config_free_reason
        assert any("NOTICE" in line for line in startup.notices)

    def test_config_free_without_a_machine_name_errors_instead_of_guessing(
        self, tmp_path: Path, capsys
    ) -> None:
        with pytest.raises(SystemExit) as exc:
            _resolve_agent_startup(_absent(tmp_path), None)

        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "--machine" in captured.out + captured.err


class TestUnreachableDaemonNeverDegradesToEmpty:
    def _unreachable(self, monkeypatch) -> None:
        svc = ServiceConfig(url="http://daemon.tailnet:7435", token=None)
        monkeypatch.setattr("coord.client.resolve_board_service", lambda *a, **k: svc)

        def _boom(*_a, **_k):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr("coord.client.fetch_remote_config", _boom)

    def test_daemon_down_fails_loudly_rather_than_publishing_empty(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """The fleet-wide hazard in #1712: elitebook has no local file either,
        so a daemon-down restart would otherwise silently drop the fleet's
        only `browser` machine. Refuse to start instead."""
        self._unreachable(monkeypatch)
        slept: list[float] = []

        with pytest.raises(SystemExit) as exc:
            _resolve_agent_startup(
                _absent(tmp_path),
                "elitebook",
                sleep=slept.append,
                attempts=3,
                retry_delay=0.01,
            )

        assert exc.value.code == 2
        text = "".join(capsys.readouterr())
        # ...and says which it did: retried, then gave up, and why.
        assert slept == [0.01, 0.01]
        assert "retrying" in text
        assert "FATAL" in text
        assert "config-free" in text
        assert "capabilities=[]" in text

    def test_local_file_failure_is_not_retried(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """No board service → the failure is a bad/absent local file, which
        fails identically every time. Don't burn startup time retrying it."""
        broken = tmp_path / "coordinator.yml"
        broken.write_text("repos: [oops\n")
        slept: list[float] = []

        with pytest.raises(SystemExit):
            _resolve_agent_startup(
                broken, "laptop", sleep=slept.append, attempts=3, retry_delay=0.01
            )

        assert slept == []
        assert "FATAL" in "".join(capsys.readouterr())


class TestHealthMarksConfigFree:
    def test_health_reports_none_on_the_normal_path(self, tmp_path: Path) -> None:
        server = AgentServer(
            machine_name="laptop",
            capabilities=["python"],
            repos=[],
            state_dir=tmp_path / "state",
            worktree_writable_settings_files=[],
        )
        assert server.health()["config_free"] is None

    def test_health_carries_the_config_free_reason(self, tmp_path: Path) -> None:
        """A legitimately config-free ephemeral worker must be
        distinguishable from a machine whose declared capabilities vanished —
        `capabilities: []` alone cannot tell those apart, which is how #1673
        stayed 'unexplained'."""
        server = AgentServer(
            machine_name="ephemeral-1",
            capabilities=[],
            repos=[],
            state_dir=tmp_path / "state",
            worktree_writable_settings_files=[],
            config_free_reason="no local coordinator.yml and no board service",
        )
        health = server.health()
        assert health["capabilities"] == []
        assert "no board service" in health["config_free"]
