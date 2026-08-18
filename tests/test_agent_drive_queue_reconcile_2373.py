"""#2373: the agent-side half of closing a launch-host-only liveness
ambiguity without a human SSH session.

Live incident, 2026-08-18 (claude-coordinator#2360): a drive-queue entry
launched on elitebook — a non-daemon host running only
``coord-agent.service``, no ``coord-drive-queue.timer`` — sat ``running`` for
~17h because #1870's cross-host guard (correctly) refused to let ANY other
host declare it dead, and nothing ever ran a local reconcile on elitebook
itself. Running ``coord drive-queue tick --reconcile-only`` there by hand
resolved it in one call.

:class:`~coord.agent.AgentServer` gains ``reconcile_drive_queue`` — the same
command, reachable over this agent's existing HTTP API
(``POST /drive-queue-reconcile``) instead of a human's SSH session. See
``tests/test_release_cordon_2101.py``'s #2373 section for the caller side:
``coord release propagate``'s drain-deadline escalation invoking this before
it escalates loudly.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest


# ── AgentServer.reconcile_drive_queue ───────────────────────────────────────


class TestReconcileDriveQueue:
    @pytest.fixture
    def server(self, tmp_path):
        from coord.agent import AgentServer

        return AgentServer(
            machine_name="elitebook",
            capabilities=["python"],
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/true"],
            health_config=SimpleNamespace(path=str(tmp_path / "coordinator.yml")),
        )

    def test_runs_the_reconcile_only_tick_and_reports_success(
        self, server, tmp_path, monkeypatch
    ):
        import sys

        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout="tick: 1 entry reconciled -> parked\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = server.reconcile_drive_queue(timeout=5.0)

        assert result == {"ok": True, "detail": "tick: 1 entry reconciled -> parked"}
        [argv] = calls
        assert argv[0] == sys.executable
        assert argv[1:5] == ["-m", "coord.cli", "drive-queue", "tick"]
        assert "--reconcile-only" in argv
        assert argv[-2:] == ["--config", str(tmp_path / "coordinator.yml")]

    def test_a_failed_tick_reports_ok_false_with_the_stderr(self, server, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr="could not read the board\n"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = server.reconcile_drive_queue(timeout=5.0)

        assert result["ok"] is False
        assert "could not read the board" in result["detail"]

    def test_a_hung_tick_does_not_raise(self, server, monkeypatch):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=5.0)

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = server.reconcile_drive_queue(timeout=5.0)

        assert result["ok"] is False
        assert "TimeoutExpired" in result["detail"]

    def test_config_free_mode_refuses_rather_than_guessing_a_config_path(self, tmp_path):
        """A thin-client/config-free agent (docs/EPHEMERAL_WORKERS.md) has no
        local coordinator.yml — there is nothing for `--config` to resolve
        to, so this must refuse cleanly rather than shell out with a bad
        path or `None`."""
        from coord.agent import AgentServer

        agent = AgentServer(
            machine_name="ephemeral",
            capabilities=["python"],
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/true"],
        )

        result = agent.reconcile_drive_queue(timeout=5.0)

        assert result["ok"] is False
        assert "no local coordinator.yml" in result["detail"]


# ── the agent endpoint ──────────────────────────────────────────────────────


class TestDriveQueueReconcileEndpoint:
    @pytest.fixture
    def client(self, tmp_path):
        from starlette.testclient import TestClient

        from coord.agent import AgentServer
        from coord.agent_app import build_app

        server = AgentServer(
            machine_name="elitebook",
            capabilities=["python"],
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/true"],
            health_config=SimpleNamespace(path=str(tmp_path / "coordinator.yml")),
        )
        return TestClient(build_app(server))

    def test_post_runs_the_local_tick_off_the_event_loop(self, client, monkeypatch):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        resp = client.post("/drive-queue-reconcile", json={"timeout": 5})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "detail": "ok"}

    def test_post_with_no_body_still_works(self, client, monkeypatch):
        """`coord release propagate`'s caller posts an empty body — there is
        nothing per-repo to scope this to, unlike `/graph-fix`."""
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        resp = client.post("/drive-queue-reconcile")

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
