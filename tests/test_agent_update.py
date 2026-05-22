"""Tests for the /update and /restart agent endpoints and the corresponding CLI commands."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from coord import __version__
from coord.agent import AgentServer
from coord.agent_app import _detect_install_mode, build_app
from coord.cli import main


# ── Helpers ────────────────────────────────────────────────────────────────


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def _make_server(tmp_path: Path, argv: list[str] | None = None):
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv or ["/bin/sh", "-c", "echo ok"],
        repo_paths={"api": str(repo)},
    )
    return server, repo


def _make_client(
    tmp_path: Path,
    argv: list[str] | None = None,
    exec_restart: object = None,
) -> tuple[TestClient, AgentServer]:
    server, _ = _make_server(tmp_path, argv)
    # Default no-op restart so tests never replace the test process.
    noop_restart = exec_restart if exec_restart is not None else (lambda _argv: None)
    app = build_app(server, exec_restart=noop_restart)
    return TestClient(app), server


# ── /status: version field ─────────────────────────────────────────────────


class TestStatusVersion:
    def test_status_includes_version_field(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        r = client.get("/status")
        assert r.status_code == 200
        body = r.json()
        assert "version" in body
        assert body["version"] == __version__
        server.shutdown()

    def test_version_is_string(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        body = client.get("/status").json()
        assert isinstance(body["version"], str)
        assert body["version"]  # not empty
        server.shutdown()


# ── /update ───────────────────────────────────────────────────────────────


class TestUpdateEndpoint:
    def test_update_returns_202(self, tmp_path: Path) -> None:
        restarted: list[list[str]] = []
        client, server = _make_client(tmp_path, exec_restart=restarted.append)
        r = client.post("/update")
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "updating"
        assert "mode" in body
        server.shutdown()

    def test_update_response_has_mode_field(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        r = client.post("/update")
        assert r.status_code == 202
        body = r.json()
        assert body["mode"] in ("editable (git pull)", "pip install --upgrade")
        server.shutdown()

    def test_update_triggers_exec_restart_after_success(self, tmp_path: Path) -> None:
        """exec_restart must be called after a successful upgrade."""
        restarted: list[list[str]] = []
        # Patch subprocess.run so the upgrade always succeeds (returncode 0)
        # and returns quickly.
        with patch("coord.agent_app.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            client, server = _make_client(tmp_path, exec_restart=restarted.append)
            client.post("/update")

        # Give the background thread time to finish.
        deadline = time.time() + 5
        while not restarted and time.time() < deadline:
            time.sleep(0.05)

        assert restarted, "exec_restart was never called"
        server.shutdown()

    def test_update_does_not_restart_on_upgrade_failure(self, tmp_path: Path) -> None:
        """If the upgrade command fails, exec_restart must NOT be called."""
        restarted: list[list[str]] = []
        with patch("coord.agent_app.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            client, server = _make_client(tmp_path, exec_restart=restarted.append)
            client.post("/update")

        # Wait a bit to confirm no restart occurred.
        time.sleep(0.5)
        assert not restarted, "exec_restart should not have been called on failure"
        server.shutdown()

    def test_update_editable_mode_uses_git_pull(self, tmp_path: Path) -> None:
        """In editable mode, /update should run 'git pull --ff-only'."""
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("coord.agent_app._detect_install_mode", return_value=(True, "/src/coord")),
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
        ):
            client, server = _make_client(tmp_path)
            client.post("/update")

        # Give the background thread time to run.
        deadline = time.time() + 3
        while not any("git" in " ".join(c) for c in calls) and time.time() < deadline:
            time.sleep(0.05)

        git_cmds = [c for c in calls if "git" in c]
        assert git_cmds, "expected a git call"
        assert any("pull" in c for c in git_cmds)
        server.shutdown()

    def test_update_pip_mode_uses_pip_install(self, tmp_path: Path) -> None:
        """In non-editable mode, /update should run pip install --upgrade."""
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
        ):
            client, server = _make_client(tmp_path)
            client.post("/update")

        deadline = time.time() + 3
        while not calls and time.time() < deadline:
            time.sleep(0.05)

        pip_cmds = [c for c in calls if "pip" in " ".join(c)]
        assert pip_cmds, "expected a pip call"
        assert any("install" in c and "--upgrade" in c for c in pip_cmds)
        server.shutdown()


# ── /restart ──────────────────────────────────────────────────────────────


class TestRestartEndpoint:
    def test_restart_returns_202(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        r = client.post("/restart")
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "restarting"
        server.shutdown()

    def test_restart_response_shape(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        r = client.post("/restart", json={"cancel_timeout": 10})
        assert r.status_code == 202
        body = r.json()
        assert "status" in body
        assert "active_workers" in body
        assert "cancel_timeout" in body
        assert body["cancel_timeout"] == pytest.approx(10)
        server.shutdown()

    def test_restart_default_cancel_timeout(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        r = client.post("/restart")
        body = r.json()
        assert body["cancel_timeout"] == pytest.approx(30)
        server.shutdown()

    def test_restart_triggers_exec_restart_when_idle(self, tmp_path: Path) -> None:
        """With no active workers, exec_restart should be called quickly."""
        restarted: list[list[str]] = []
        client, server = _make_client(tmp_path, exec_restart=restarted.append)
        client.post("/restart", json={"cancel_timeout": 5})

        deadline = time.time() + 5
        while not restarted and time.time() < deadline:
            time.sleep(0.05)

        assert restarted, "exec_restart was never called"
        server.shutdown()

    def test_restart_reports_active_worker_count(self, tmp_path: Path) -> None:
        """active_workers field in the response must reflect the current count."""
        repo = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "sleep 30"],
            repo_paths={"api": str(repo)},
        )
        app = build_app(server, exec_restart=lambda _: None)
        client = TestClient(app)

        from coord.agent import AssignmentSpec
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(repo),
            issue_number=1,
            issue_title="test",
            briefing="b",
        )
        a = server.assign(spec)

        # Wait for the worker to actually start running.
        for _ in range(50):
            if server.get(a.id).status == "running":
                break
            time.sleep(0.02)

        r = client.post("/restart", json={"cancel_timeout": 1})
        assert r.status_code == 202
        assert r.json()["active_workers"] >= 1

        server.shutdown(kill_running=True)

    def test_restart_cancels_active_workers(self, tmp_path: Path) -> None:
        """Workers still running at cancel_timeout should be cancelled."""
        restarted: list = []
        repo = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "sleep 60"],
            repo_paths={"api": str(repo)},
        )
        app = build_app(server, exec_restart=restarted.append)
        client = TestClient(app)

        from coord.agent import AssignmentSpec
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(repo),
            issue_number=2,
            issue_title="long job",
            briefing="b",
        )
        a = server.assign(spec)
        for _ in range(50):
            if server.get(a.id).status == "running":
                break
            time.sleep(0.02)

        # Request restart with very short cancel_timeout so the worker is cancelled.
        client.post("/restart", json={"cancel_timeout": 0})

        deadline = time.time() + 10
        while not restarted and time.time() < deadline:
            time.sleep(0.1)

        assert restarted, "exec_restart was never called"
        assert server.get(a.id).status == "cancelled"
        server.shutdown()

    def test_restart_accepts_empty_body(self, tmp_path: Path) -> None:
        """POST /restart with no body should still return 202."""
        client, server = _make_client(tmp_path)
        r = client.post("/restart")
        assert r.status_code == 202
        server.shutdown()


# ── _detect_install_mode ──────────────────────────────────────────────────


class TestDetectInstallMode:
    def test_editable_install_detected(self) -> None:
        pip_output = (
            "Name: claude-coordinator\n"
            "Version: 0.2.0\n"
            "Location: /src/claude-coordinator\n"
            "Editable project location: /src/claude-coordinator\n"
        )
        with patch("coord.agent_app.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=pip_output, stderr="")
            is_editable, path = _detect_install_mode()
        assert is_editable is True
        assert path == "/src/claude-coordinator"

    def test_regular_install_detected(self) -> None:
        pip_output = (
            "Name: claude-coordinator\n"
            "Version: 0.2.0\n"
            "Location: /usr/local/lib/python3.12/site-packages\n"
        )
        with patch("coord.agent_app.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=pip_output, stderr="")
            is_editable, path = _detect_install_mode()
        assert is_editable is False
        assert path is None

    def test_subprocess_failure_returns_non_editable(self) -> None:
        with patch("coord.agent_app.subprocess.run", side_effect=Exception("boom")):
            is_editable, path = _detect_install_mode()
        assert is_editable is False
        assert path is None


# ── CLI: coord agent update / restart ─────────────────────────────────────


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_db_fixture(coord_db):
    return coord_db


class TestAgentUpdateCLI:
    def test_update_single_machine(
        self, config_file: Path, coord_db
    ) -> None:
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"machine": "laptop"}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "laptop" in result.output
        assert "accepted" in result.output
        assert "online" in result.output

    def test_update_all_machines(
        self, config_file: Path, coord_db
    ) -> None:
        posted_to: list[str] = []

        def fake_post(url, *args, **kwargs):
            posted_to.append(url)
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--all", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        # Both machines should have been contacted.
        assert len(posted_to) == 2
        assert any("laptop" in u for u in posted_to)
        assert any("server" in u for u in posted_to)

    def test_update_requires_machine_or_all(
        self, config_file: Path, coord_db
    ) -> None:
        result = CliRunner().invoke(
            main,
            ["agent", "update", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "--machine" in result.output or "--all" in result.output

    def test_update_machine_and_all_mutually_exclusive(
        self, config_file: Path, coord_db
    ) -> None:
        result = CliRunner().invoke(
            main,
            ["agent", "update", "--machine", "laptop", "--all",
             "--config", str(config_file)],
        )
        assert result.exit_code != 0

    def test_update_unknown_machine_errors(
        self, config_file: Path, coord_db
    ) -> None:
        result = CliRunner().invoke(
            main,
            ["agent", "update", "--machine", "ghost",
             "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "ghost" in result.output

    def test_update_agent_offline_reported(
        self, config_file: Path, coord_db
    ) -> None:
        with (
            patch(
                "coord.cli.httpx.post",
                side_effect=httpx.ConnectError("connection refused"),
            ),
            patch(
                "coord.cli.httpx.get",
                side_effect=httpx.ConnectError("connection refused"),
            ),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "1",
                 "--config", str(config_file)],
            )
        # Should report error, not crash
        assert "error" in result.output.lower() or "refused" in result.output.lower() or "✗" in result.output


class TestAgentRestartCLI:
    def test_restart_single_machine(
        self, config_file: Path, coord_db
    ) -> None:
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {
                "status": "restarting",
                "active_workers": 0,
                "cancel_timeout": 30,
            }
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "restart", "--machine", "laptop", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "laptop" in result.output
        assert "accepted" in result.output

    def test_restart_all_machines(
        self, config_file: Path, coord_db
    ) -> None:
        posted_to: list[str] = []

        def fake_post(url, *args, **kwargs):
            posted_to.append(url)
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {
                "status": "restarting",
                "active_workers": 0,
                "cancel_timeout": 30,
            }
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "restart", "--all", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert len(posted_to) == 2

    def test_restart_cancel_timeout_forwarded(
        self, config_file: Path, coord_db
    ) -> None:
        posted_bodies: list[dict] = []

        def fake_post(url, *args, **kwargs):
            posted_bodies.append(kwargs.get("json", {}))
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {
                "status": "restarting",
                "active_workers": 0,
                "cancel_timeout": 60,
            }
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            CliRunner().invoke(
                main,
                [
                    "agent", "restart", "--machine", "laptop",
                    "--cancel-timeout", "60", "--timeout", "5",
                    "--config", str(config_file),
                ],
            )

        assert posted_bodies
        assert posted_bodies[0].get("cancel_timeout") == 60

    def test_restart_requires_machine_or_all(
        self, config_file: Path, coord_db
    ) -> None:
        result = CliRunner().invoke(
            main,
            ["agent", "restart", "--config", str(config_file)],
        )
        assert result.exit_code != 0


# ── Version in coord status output ────────────────────────────────────────


class TestStatusVersionDisplay:
    def test_version_shown_in_status_output(
        self, config_file: Path, coord_db
    ) -> None:
        from coord import network

        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE,
                latency_ms=12.0,
                health={"machine": "laptop"},
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        status_data = {
            "active": [],
            "completed": [],
            "version": "0.2.0",
        }
        with (
            patch("coord.network.check_all", return_value=statuses),
            patch(
                "coord.network.fetch_status",
                return_value=network.StatusResult(data=status_data),
            ),
        ):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        assert "agent-version: 0.2.0" in result.output

    def test_version_mismatch_flagged(
        self, config_file: Path, coord_db
    ) -> None:
        from coord import network

        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE,
                latency_ms=12.0,
                health={"machine": "laptop"},
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        # Report a *different* version from the agent.
        status_data = {
            "active": [],
            "completed": [],
            "version": "0.1.0",  # older than __version__
        }
        with (
            patch("coord.network.check_all", return_value=statuses),
            patch(
                "coord.network.fetch_status",
                return_value=network.StatusResult(data=status_data),
            ),
        ):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        # Warning indicator should appear
        assert "⚠" in result.output or "mismatch" in result.output.lower()
        assert "0.1.0" in result.output

    def test_no_version_shown_when_offline(
        self, config_file: Path, coord_db
    ) -> None:
        from coord import network

        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.OFFLINE,
                reason="connection refused",
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        with patch("coord.network.check_all", return_value=statuses):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        assert "agent-version" not in result.output
