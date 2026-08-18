"""#2373 review fix: `coord.reconcile_tick.run_reconcile_tick` is the single
shared implementation of "run `coord drive-queue tick --reconcile-only` as a
subprocess" — extracted so `coord release propagate`'s `_run_reconcile_tick`
(`coord/commands/release.py`, #2110) and `AgentServer.reconcile_drive_queue`
(`coord/agent.py`, #2373) stop hand-maintaining two copies of the same argv
construction, error handling, and result shape (the drift the review flagged:
a hardcoded 120s timeout vs. a caller-supplied one, 200-char vs. 2000-char
detail truncation).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from coord.reconcile_tick import run_reconcile_tick


class TestRunReconcileTick:
    def test_builds_the_expected_argv_and_reports_success(self, tmp_path):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout="1 reconciled\n", stderr="")

        config_path = tmp_path / "coordinator.yml"
        ok, detail = run_reconcile_tick(config_path, timeout=5.0, runner=fake_run)

        assert (ok, detail) == (True, "1 reconciled")
        [(argv, kwargs)] = calls
        assert argv[0] == sys.executable
        assert argv[1:] == [
            "-m", "coord.cli", "drive-queue", "tick",
            "--reconcile-only", "--config", str(config_path),
        ]
        assert kwargs["timeout"] == 5.0

    def test_a_failed_tick_reports_ok_false_with_stderr(self, tmp_path):
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="board unreadable\n")

        ok, detail = run_reconcile_tick(tmp_path / "c.yml", runner=fake_run)

        assert ok is False
        assert detail == "board unreadable"

    def test_a_hung_tick_does_not_raise(self, tmp_path):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=5.0)

        ok, detail = run_reconcile_tick(tmp_path / "c.yml", timeout=5.0, runner=fake_run)

        assert ok is False
        assert "TimeoutExpired" in detail

    @pytest.mark.parametrize("limit", [200, 2000])
    def test_detail_limit_is_caller_controlled(self, tmp_path, limit):
        long_output = "x" * 3000

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=long_output, stderr="")

        _ok, detail = run_reconcile_tick(tmp_path / "c.yml", detail_limit=limit, runner=fake_run)

        assert detail == long_output[:limit]

    def test_default_runner_is_subprocess_run(self, tmp_path, monkeypatch):
        """No *runner* passed → falls back to the real `subprocess.run`,
        which is what both real callers rely on (and what their own tests
        monkeypatch globally rather than injecting a seam)."""

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        ok, detail = run_reconcile_tick(tmp_path / "c.yml", timeout=5.0)

        assert (ok, detail) == (True, "ok")
