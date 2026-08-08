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


def _wait_until(predicate, timeout: float = 30.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until it returns truthy or ``timeout`` elapses.

    Exits as soon as the condition holds (so the happy path stays fast) but
    tolerates background-thread work that runs slowly under full-suite CPU
    contention.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _wait_for_last_update(server, timeout: float = 30.0) -> dict:
    """Block until last_update.json exists AND parses as valid JSON, returning
    the parsed dict.

    ``_write_last_update`` does a plain ``write_text`` (truncate-then-write),
    so a reader that fires between the truncate and the write sees empty or
    partial content.  Waiting for a successful parse (not mere existence)
    closes that race without touching production code.
    """
    path = server.state_dir / "last_update.json"
    result: dict = {}

    def _parsed() -> bool:
        try:
            import json as _json
            result.update(_json.loads(path.read_text()))
            return True
        except Exception:
            return False

    assert _wait_until(_parsed, timeout=timeout), \
        "background update thread never wrote a parseable last_update.json"
    return result


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
        """exec_restart must be called after a successful upgrade.

        Force the editable-mode path so we always restart on a 0 returncode
        (the pip-install path also requires a version delta — covered in
        test_update_skips_restart_when_pip_no_change below).

        NOTE: the polling loop MUST stay inside the patch context — the
        background thread reads `subprocess.run` lazily; if the patch
        exits first, the real `git pull` runs against the fake path and
        fails with returncode != 0.
        """
        restarted: list[list[str]] = []
        with patch("coord.agent_app._detect_install_mode") as mock_detect:
            mock_detect.return_value = (True, "/fake/project")
            with patch("coord.agent_app.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                client, server = _make_client(tmp_path, exec_restart=restarted.append)
                client.post("/update")
                assert _wait_until(lambda: bool(restarted)), \
                    "exec_restart was never called"

        server.shutdown()

    def test_update_skips_restart_when_pip_no_change(self, tmp_path: Path) -> None:
        """If pip succeeds but resolves to the same version, no restart and
        a no_change result is persisted for the next /health to surface."""
        restarted: list[list[str]] = []
        with patch("coord.agent_app._detect_install_mode") as mock_detect:
            mock_detect.return_value = (False, None)  # pip path
            with patch("coord.agent_app._installed_version", return_value="0.3.0"):
                with patch("coord.agent_app.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                    client, server = _make_client(tmp_path, exec_restart=restarted.append)
                    client.post("/update")
                    # Wait for the background thread to finish (it persists
                    # last_update.json); then confirm no restart occurred.
                    last = _wait_for_last_update(server)

        assert not restarted, "exec_restart fired even though version didn't change"
        assert last["result"] == "no_change"
        assert last["version_before"] == "0.3.0"
        assert last["version_after"] == "0.3.0"
        server.shutdown()

    def test_update_does_not_restart_on_upgrade_failure(self, tmp_path: Path) -> None:
        """If the upgrade command fails, exec_restart must NOT be called."""
        restarted: list[list[str]] = []
        with patch("coord.agent_app.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            client, server = _make_client(tmp_path, exec_restart=restarted.append)
            client.post("/update")
            # The failure path persists last_update.json; wait for the
            # background thread to finish (inside the patch context, since it
            # reads subprocess.run lazily) before asserting no restart.
            assert _wait_until(
                lambda: (server.state_dir / "last_update.json").exists()
            ), "background update thread never wrote last_update.json"

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
            # Wait for the specific call the assertions need (git pull),
            # not just any git call.  Staying inside the patch context
            # ensures fake_run intercepts every subprocess.run the
            # background thread makes.
            _wait_until(lambda: any("pull" in c for c in calls))

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
            # Wait for the specific call the assertions need (pip install
            # --upgrade), not just any call.  Staying inside the patch
            # context ensures fake_run intercepts every subprocess.run the
            # background thread makes.
            _wait_until(
                lambda: any("install" in c and "--upgrade" in c for c in calls)
            )

        pip_cmds = [c for c in calls if "pip" in " ".join(c)]
        assert pip_cmds, "expected a pip call"
        assert any("install" in c and "--upgrade" in c for c in pip_cmds)
        server.shutdown()

    def test_update_pins_target_version_when_supplied(self, tmp_path: Path) -> None:
        """#1568: when the caller supplies target_version, pip must be
        pinned to that exact release (claude-coordinator==X.Y.Z) instead
        of a bare --upgrade — this turns a stale PyPI index/cache into a
        loud pip failure instead of a silent no-op resolving to old code."""
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            return MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
        ):
            client, server = _make_client(tmp_path)
            client.post("/update", json={"target_version": "9.9.9"})
            _wait_until(lambda: any("install" in c for c in calls))

        pip_cmds = [c for c in calls if "pip" in " ".join(c) or "install" in c]
        assert pip_cmds, "expected a pip call"
        # #1237: the spec carries the `[server]` extra — an agent must
        # reinstall itself with the server runtime, not the client base.
        assert any(
            "claude-coordinator[server]==9.9.9" in c for c in pip_cmds[0]
        ), pip_cmds
        server.shutdown()

    def test_update_omits_pin_when_no_target_version(self, tmp_path: Path) -> None:
        """Backward compat: no target_version in the request body means a
        bare --upgrade, same as before #1568."""
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
            _wait_until(lambda: any("install" in c for c in calls))

        pip_cmds = [c for c in calls if "install" in c]
        assert pip_cmds, "expected a pip call"
        # #1237: unpinned, but still carrying the mandatory `[server]` extra.
        assert "claude-coordinator[server]" in pip_cmds[0]
        assert not any("==" in arg for arg in pip_cmds[0])
        server.shutdown()

    def test_update_last_update_shows_upgraded_even_if_exec_restart_raises(
        self, tmp_path: Path
    ) -> None:
        """last_update.json must persist result='upgraded' BEFORE exec_restart runs.

        If the new process crashes on startup (e.g. _prune_worktrees raises
        FileNotFoundError), last_update.json was already written as 'upgraded'
        so the coordinator can distinguish a clean upgrade + dead-restart from
        a failed pip step.  Regression test for issue #280.
        """
        def boom(_argv: list[str]) -> None:
            raise RuntimeError("simulated exec_restart failure")

        with (
            patch("coord.agent_app._detect_install_mode", return_value=(True, "/fake/src")),
            patch("coord.agent_app.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="Already up to date.", stderr="")
            client, server = _make_client(tmp_path, exec_restart=boom)
            client.post("/update")
            # Give the background thread time to complete (inside the patch
            # context — it reads subprocess.run lazily).
            last = _wait_for_last_update(server)

        # The upgrade step succeeded; result must be "upgraded" even though
        # exec_restart itself raised an exception.
        assert last["result"] == "upgraded", (
            f"expected result='upgraded', got {last['result']!r}; "
            "last_update.json must be written BEFORE exec_restart is called"
        )
        server.shutdown()

    def test_update_git_pull_fnf_writes_failed_result(self, tmp_path: Path) -> None:
        """If the git pull cwd doesn't exist, result must be 'failed' (not 'upgraded').

        This covers the scenario where the editable install's source directory
        has been deleted (e.g. it was a worktree that got pruned).  The pip/git
        step raises FileNotFoundError BEFORE the upgrade succeeds, so
        last_update.json should record result='failed'.  Regression test for
        issue #280.
        """
        restarted: list = []

        # Build the server (and the underlying git repo) BEFORE patching
        # subprocess.run — _make_server calls git init/commit which must
        # use the real subprocess.run.
        client, server = _make_client(tmp_path, exec_restart=restarted.append)

        def fake_run(cmd, **kwargs):
            # Simulate subprocess.run raising FileNotFoundError because the
            # cwd (the deleted worktree) no longer exists.
            raise FileNotFoundError(
                2, "No such file or directory", "/home/user/.coord/worktrees/deadbeef"
            )

        with (
            patch("coord.agent_app._detect_install_mode",
                  return_value=(True, "/home/user/.coord/worktrees/deadbeef")),
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
        ):
            client.post("/update")
            # Wait for the background thread to write last_update.json.  This
            # must stay INSIDE the patch context (see NOTE in
            # test_update_triggers_exec_restart_after_success): the background
            # thread reads subprocess.run lazily.
            last = _wait_for_last_update(server)

        assert not restarted, "exec_restart must not fire when git pull raises"
        assert last["result"] == "failed"
        assert "FileNotFoundError" in (last.get("error") or "")
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

        assert _wait_until(lambda: bool(restarted)), "exec_restart was never called"
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

        assert _wait_until(lambda: bool(restarted)), "exec_restart was never called"
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


# ── _escalate_restart ────────────────────────────────────────────────────


class TestEscalateRestart:
    def test_runs_systemctl_restart_with_load_bearing_env_prefix(self) -> None:
        """#404 / #1568: XDG_RUNTIME_DIR=/run/user/$(id -u) is load-bearing —
        a bare `systemctl --user restart` silently no-ops over SSH."""
        from coord.commands.agent_ops import _escalate_restart

        machine = MagicMock()
        machine.host = "dellserver.tailnet"

        with patch("coord.commands.agent_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ok = _escalate_restart(machine)

        assert ok is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "dellserver.tailnet" in cmd
        joined = " ".join(cmd)
        assert "XDG_RUNTIME_DIR=/run/user/$(id -u)" in joined
        assert "systemctl --user restart coord-agent" in joined

    def test_returns_false_on_nonzero_exit(self) -> None:
        from coord.commands.agent_ops import _escalate_restart

        machine = MagicMock()
        machine.host = "dellserver.tailnet"

        with patch("coord.commands.agent_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=255, stdout="", stderr="ssh: connect timeout")
            ok = _escalate_restart(machine)

        assert ok is False

    def test_returns_false_when_ssh_raises(self) -> None:
        """Unreachable host, no ssh binary, etc. must not crash the CLI."""
        from coord.commands.agent_ops import _escalate_restart

        machine = MagicMock()
        machine.host = "dellserver.tailnet"

        with patch(
            "coord.commands.agent_ops.subprocess.run",
            side_effect=FileNotFoundError("no ssh binary"),
        ):
            ok = _escalate_restart(machine)

        assert ok is False


# ── #1886: PyPI-resolved target version ────────────────────────────────────


class TestResolveTargetVersion:
    """`_resolve_target_version` (#1886 Path A): the target must come from
    PyPI's simple index, not this CLI's own (possibly stale) __version__,
    except when --version explicitly overrides it."""

    def test_explicit_version_wins_without_hitting_pypi(self) -> None:
        from coord.commands.agent_ops import _resolve_target_version

        with patch("coord.health.pypi.latest_release") as mock_latest:
            target, warnings = _resolve_target_version("1.2.3", own_version="1.0.0")

        assert target == "1.2.3"
        assert warnings == []
        mock_latest.assert_not_called()

    def test_targets_newer_pypi_release_and_warns_when_cli_is_stale(self) -> None:
        """The reported bug: PyPI already has v0.4.108, the operator CLI is
        still v0.4.107 — the target must be 0.4.108, with a loud warning,
        never a silent 0.4.107."""
        from coord.commands.agent_ops import _resolve_target_version
        from coord.health.pypi import parse_version

        with patch(
            "coord.health.pypi.latest_release",
            return_value=(
                parse_version("0.4.108"),
                [parse_version("0.4.107"), parse_version("0.4.108")],
            ),
        ):
            target, warnings = _resolve_target_version(None, own_version="0.4.107")

        assert target == "0.4.108"
        assert warnings
        assert "0.4.107" in warnings[0]
        assert "0.4.108" in warnings[0]

    def test_targets_own_version_when_already_current(self) -> None:
        from coord.commands.agent_ops import _resolve_target_version
        from coord.health.pypi import parse_version

        with patch(
            "coord.health.pypi.latest_release",
            return_value=(parse_version("0.4.108"), [parse_version("0.4.108")]),
        ):
            target, warnings = _resolve_target_version(None, own_version="0.4.108")

        assert target == "0.4.108"
        assert warnings == []

    def test_targets_own_version_when_ahead_of_latest_pypi_release(self) -> None:
        """An editable/dev checkout ahead of the last PyPI release must not
        be dragged backwards."""
        from coord.commands.agent_ops import _resolve_target_version
        from coord.health.pypi import parse_version

        with patch(
            "coord.health.pypi.latest_release",
            return_value=(parse_version("0.4.108"), [parse_version("0.4.108")]),
        ):
            target, warnings = _resolve_target_version(None, own_version="0.4.109")

        assert target == "0.4.109"
        assert warnings == []

    def test_falls_back_to_own_version_when_pypi_unreachable(self) -> None:
        from coord.commands.agent_ops import _resolve_target_version

        with patch(
            "coord.health.pypi.latest_release",
            side_effect=Exception("connection refused"),
        ):
            target, warnings = _resolve_target_version(None, own_version="0.4.107")

        assert target == "0.4.107"
        assert warnings
        assert "pypi" in warnings[0].lower()

    def test_falls_back_to_own_version_when_pypi_has_no_releases(self) -> None:
        from coord.commands.agent_ops import _resolve_target_version

        with patch("coord.health.pypi.latest_release", return_value=(None, [])):
            target, warnings = _resolve_target_version(None, own_version="0.4.107")

        assert target == "0.4.107"
        assert warnings


# ── #1886: /health exposes running vs installed version ────────────────────


class TestHealthInstalledVersion:
    def test_health_reports_installed_version_separately_from_running(
        self, tmp_path: Path
    ) -> None:
        """#1886 Path B: `version` is bound at process import time (the
        RUNNING, loaded-module version) and `installed_version` is a fresh
        disk read that can advance without a restart. Both must be visible
        on /health so a caller can detect the drift itself."""
        client, server = _make_client(tmp_path)
        with patch("coord.agent_app._installed_version", return_value="9.9.9"):
            r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["installed_version"] == "9.9.9"
        assert body["version"] == __version__
        assert body["installed_version"] != body["version"]
        server.shutdown()

    def test_health_installed_version_matches_running_when_in_sync(
        self, tmp_path: Path
    ) -> None:
        client, server = _make_client(tmp_path)
        with patch("coord.agent_app._installed_version", return_value=__version__):
            r = client.get("/health")
        body = r.json()
        assert body["installed_version"] == body["version"] == __version__
        server.shutdown()


# ── #1886 / #404: systemd-aware restart ─────────────────────────────────────


class TestSystemdAwareRestart:
    def test_running_under_systemd_detects_invocation_id(self, monkeypatch) -> None:
        from coord.agent_app import _running_under_systemd

        monkeypatch.delenv("INVOCATION_ID", raising=False)
        assert _running_under_systemd() is False
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        assert _running_under_systemd() is True

    def test_restart_via_systemctl_sets_xdg_runtime_dir(self, monkeypatch) -> None:
        from coord.agent_app import _restart_via_systemctl

        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr("os.getuid", lambda: 1000, raising=False)
        captured: dict = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return MagicMock()

        with patch("coord.agent_app.subprocess.Popen", side_effect=fake_popen):
            ok = _restart_via_systemctl()

        assert ok is True
        assert captured["cmd"] == ["systemctl", "--user", "restart", "coord-agent"]
        assert captured["env"]["XDG_RUNTIME_DIR"] == "/run/user/1000"

    def test_restart_via_systemctl_returns_false_on_launch_failure(self) -> None:
        from coord.agent_app import _restart_via_systemctl

        with patch(
            "coord.agent_app.subprocess.Popen",
            side_effect=FileNotFoundError("no systemctl"),
        ):
            assert _restart_via_systemctl() is False

    def test_default_exec_restart_prefers_systemctl_under_systemd(
        self, monkeypatch
    ) -> None:
        """#404 / #1886: os.execv alone doesn't take under systemd (same
        PID, stale code survives). Under systemd, restart via `systemctl
        --user restart` and exit — never os.execv."""
        from coord import agent_app

        monkeypatch.setenv("INVOCATION_ID", "abc123")
        popen_calls: list = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append(cmd)
            return MagicMock()

        # The real os._exit() never returns — simulate that with an
        # exception so a bug that let control fall through to os.execv
        # afterward would show up as `mock_execv` having been called,
        # instead of silently passing.
        exit_calls: list = []

        def fake_exit(code):
            exit_calls.append(code)
            raise SystemExit(code)

        with (
            patch("coord.agent_app.subprocess.Popen", side_effect=fake_popen),
            patch("coord.agent_app.os._exit", side_effect=fake_exit),
            patch("coord.agent_app.os.execv") as mock_execv,
        ):
            with pytest.raises(SystemExit):
                agent_app._default_exec_restart(["coord", "agent"])

        assert popen_calls, "expected systemctl --user restart to be launched"
        assert popen_calls[0][:3] == ["systemctl", "--user", "restart"]
        assert "coord-agent" in popen_calls[0]
        assert exit_calls == [0]
        mock_execv.assert_not_called()

    def test_default_exec_restart_falls_back_to_execv_without_systemd(
        self, monkeypatch
    ) -> None:
        from coord import agent_app

        monkeypatch.delenv("INVOCATION_ID", raising=False)
        with (
            patch("coord.agent_app.subprocess.Popen") as mock_popen,
            patch("coord.agent_app.os.execv") as mock_execv,
        ):
            agent_app._default_exec_restart(["coord", "agent"])

        mock_popen.assert_not_called()
        assert mock_execv.called

    def test_default_exec_restart_falls_back_to_execv_when_systemctl_launch_fails(
        self, monkeypatch
    ) -> None:
        from coord import agent_app

        monkeypatch.setenv("INVOCATION_ID", "abc123")
        with (
            patch(
                "coord.agent_app.subprocess.Popen",
                side_effect=FileNotFoundError("no systemctl"),
            ),
            patch("coord.agent_app.os.execv") as mock_execv,
        ):
            agent_app._default_exec_restart(["coord", "agent"])

        assert mock_execv.called


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
        """#1568: success requires the agent to actually report the
        requested version — not just that the POST was accepted."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "machine": "laptop",
                "version": __version__,
                "last_update": {
                    "result": "upgraded",
                    "version_before": "0.0.1",
                    "version_after": __version__,
                },
            }
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
        assert __version__ in result.output

    def test_update_all_machines(
        self, config_file: Path, coord_db
    ) -> None:
        """#1568: success requires the agent to actually report the
        requested version — not just that the POST was accepted."""
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
            r.json.return_value = {
                "version": __version__,
                "last_update": {
                    "result": "upgraded",
                    "version_before": "0.0.1",
                    "version_after": __version__,
                },
            }
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

    def test_update_sends_target_version(
        self, config_file: Path, coord_db
    ) -> None:
        """#1568: the CLI must tell the agent which version it's asking for
        so the agent can pin its pip install instead of a bare --upgrade
        that can silently resolve to a stale cached version."""
        posted_bodies: list[dict] = []

        def fake_post(url, *args, **kwargs):
            posted_bodies.append(kwargs.get("json", {}))
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": __version__, "last_update": {"result": "upgraded"}}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert posted_bodies
        assert posted_bodies[0].get("target_version") == __version__

    def test_update_no_op_reports_failure(
        self, config_file: Path, coord_db
    ) -> None:
        """Cause A (#1568): pip resolves to a cached/stale version and
        exits 0. The POST is accepted but the agent's reported version
        never advances — `coord agent update` must NOT report success."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "version": "0.4.84",
                "last_update": {
                    "result": "no_change",
                    "version_before": "0.4.84",
                    "version_after": "0.4.84",
                    "error": "pip resolved to 0.4.84 (same as installed).",
                },
            }
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch("coord.commands.agent_ops._escalate_restart", return_value=False),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "1",
                 "--config", str(config_file)],
            )

        assert result.exit_code != 0, result.output
        assert "no change" in result.output.lower()

    def test_update_stubbed_agent_never_changes_version_reports_failure(
        self, config_file: Path, coord_db
    ) -> None:
        """Acceptance criterion from #1568: a stubbed agent that accepts
        the POST but never changes version must produce a failure, not a
        success — regardless of whether it exposes last_update at all."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            # A stub agent: accepts /update, always answers /health, but
            # its version field never moves and it exposes no last_update.
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": "0.1.0"}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch("coord.commands.agent_ops._escalate_restart", return_value=False),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "1",
                 "--config", str(config_file)],
            )

        assert result.exit_code != 0, result.output
        assert "✗" in result.output

    def test_update_escalates_when_upgrade_succeeds_but_process_is_stuck(
        self, config_file: Path, coord_db
    ) -> None:
        """Cause B (#404 / #1568): pip genuinely succeeded (last_update.result
        == "upgraded") but the os.execv self-restart doesn't take under
        systemd, so the OLD process keeps answering /health. The CLI must
        escalate to a driven `systemctl --user restart` and re-check —
        not report a bare "did not come back"."""
        restarted = {"done": False}

        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            version = __version__ if restarted["done"] else "0.4.84"
            r.json.return_value = {
                "version": version,
                "last_update": {
                    "result": "upgraded",
                    "version_before": "0.4.84",
                    "version_after": __version__,
                },
            }
            return r

        def fake_escalate(machine):
            restarted["done"] = True
            return True

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch(
                "coord.commands.agent_ops._escalate_restart",
                side_effect=fake_escalate,
            ) as mock_escalate,
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "1",
                 "--config", str(config_file)],
            )

        assert mock_escalate.called, "expected an escalation attempt"
        assert result.exit_code == 0, result.output
        assert __version__ in result.output

    def test_update_installed_advanced_but_running_stuck_reports_failure(
        self, config_file: Path, coord_db
    ) -> None:
        """Acceptance criterion (#1886): a stubbed agent whose *installed*
        version has advanced (pip/disk) but whose *running* process has
        not (the execv-under-systemd stall, #404) must report failure, not
        ✓ — even though `installed_version` already agrees with the
        target and `last_update.result == "upgraded"`."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                # installed_version reflects the freshly pip-installed
                # package; version is the stale, still-running process.
                "version": "0.4.106",
                "installed_version": __version__,
                "last_update": {
                    "result": "upgraded",
                    "version_before": "0.4.106",
                    "version_after": __version__,
                },
            }
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch("coord.commands.agent_ops._escalate_restart", return_value=False),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--version", __version__,
                 "--timeout", "1", "--config", str(config_file)],
            )

        assert result.exit_code != 0, result.output
        assert "✗" in result.output
        assert "0.4.106" in result.output

    def test_update_targets_pypi_latest_when_operator_cli_is_stale(
        self, config_file: Path, coord_db
    ) -> None:
        """#1886 Path A: the reported bug — PyPI already has a newer
        release than this CLI's own __version__. The target must be the
        PyPI release, with a loud warning, never a silent
        under-update pinned to this CLI's own stale version."""
        from coord.health.pypi import parse_version

        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": "9.9.9", "last_update": {"result": "upgraded"}}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch(
                "coord.health.pypi.latest_release",
                return_value=(parse_version("9.9.9"), [parse_version("9.9.9")]),
            ),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "9.9.9" in result.output
        assert "stale" in result.output.lower()

    def test_update_version_override_skips_pypi_resolution(
        self, config_file: Path, coord_db
    ) -> None:
        """--version pins the target directly — no PyPI lookup, no warning."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": "1.2.3", "last_update": {"result": "upgraded"}}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch("coord.health.pypi.latest_release") as mock_latest,
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--version", "1.2.3",
                 "--timeout", "5", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "1.2.3" in result.output
        mock_latest.assert_not_called()


class TestAgentVersionsCLI:
    """#1568 suggested fix #4: a fleet-wide check the operator can run to
    prove version uniformity before trusting a rule change — a split-brain
    is only detectable by comparing versions."""

    def test_versions_all_match(self, config_file: Path, coord_db) -> None:
        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": __version__}
            return r

        with patch("coord.cli.httpx.get", side_effect=fake_get):
            result = CliRunner().invoke(
                main, ["agent", "versions", "--all", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert result.output.count(__version__) >= 2  # coordinator line + machines
        assert "mismatch" not in result.output.lower()

    def test_versions_flags_split_brain(self, config_file: Path, coord_db) -> None:
        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            version = __version__ if "laptop" in url else "0.4.84"
            r.json.return_value = {"version": version}
            return r

        with patch("coord.cli.httpx.get", side_effect=fake_get):
            result = CliRunner().invoke(
                main, ["agent", "versions", "--all", "--config", str(config_file)],
            )

        assert result.exit_code != 0
        assert "split-brain" in result.output.lower()
        assert "mismatch" in result.output.lower()

    def test_versions_flags_uniform_mismatch_with_coordinator(
        self, config_file: Path, coord_db
    ) -> None:
        """Every agent agrees with every other agent (no split-brain among
        them), but the whole fleet is stale relative to the coordinator's
        own __version__ — e.g. the coordinator bumped locally but `coord
        agent update --all` hasn't landed yet. This must still exit
        non-zero: it's exactly the "confirm the rollout actually landed
        everywhere" case the docs promise, and versions_seen having only
        one element must not let it slip through as a false all-clear."""
        stale = "0.0.1-stale"
        assert stale != __version__

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": stale}
            return r

        with patch("coord.cli.httpx.get", side_effect=fake_get):
            result = CliRunner().invoke(
                main, ["agent", "versions", "--all", "--config", str(config_file)],
            )

        assert result.exit_code != 0, result.output
        assert "split-brain" not in result.output.lower()
        assert "mismatch" in result.output.lower()

    def test_versions_flags_unreachable_machine(self, config_file: Path, coord_db) -> None:
        with patch(
            "coord.cli.httpx.get", side_effect=httpx.ConnectError("connection refused")
        ):
            result = CliRunner().invoke(
                main, ["agent", "versions", "--machine", "laptop",
                       "--config", str(config_file)],
            )

        assert result.exit_code != 0
        assert "unreachable" in result.output.lower()


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

    def test_status_flags_running_installed_drift(
        self, config_file: Path, coord_db
    ) -> None:
        """#1886 item 4: a process that upgraded on disk but never
        restarted (the execv-under-systemd stall, #404) must be visible
        from `coord status` alone — no update needs to be running."""
        from coord import network

        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE,
                latency_ms=12.0,
                health={
                    "machine": "laptop",
                    "version": "0.4.106",
                    "installed_version": "0.4.108",
                },
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        status_data = {"active": [], "completed": [], "version": "0.4.106"}
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
        assert "0.4.106" in result.output
        assert "0.4.108" in result.output
        assert "restart" in result.output.lower()

    def test_status_no_drift_warning_when_running_matches_installed(
        self, config_file: Path, coord_db
    ) -> None:
        from coord import network

        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE,
                latency_ms=12.0,
                health={
                    "machine": "laptop",
                    "version": __version__,
                    "installed_version": __version__,
                },
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        status_data = {"active": [], "completed": [], "version": __version__}
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
        assert "hasn't restarted" not in result.output
