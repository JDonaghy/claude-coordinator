"""Tests for smoke test orchestration — config, dispatch, and reconcile hook."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from coord.config import Config, ConfigError, SmokeTestsConfig, load
from coord.models import Assignment, Board, Machine, Repo
from coord.smoke import (
    build_smoke_briefing,
    dispatch_smoke_test,
    pick_smoke_machine,
)


# ── Config parsing ──────────────────────────────────────────────────────────


class TestSmokeTestsConfig:
    def test_parsed_from_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "smoke_tests:\n"
            "  enabled: true\n"
            "  auto_dispatch: true\n"
            "  timeout: 300\n"
        )
        cfg = load(p)
        assert cfg.smoke_tests.enabled is True
        assert cfg.smoke_tests.auto_dispatch is True
        assert cfg.smoke_tests.timeout == 300

    def test_defaults_when_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.smoke_tests.enabled is False
        assert cfg.smoke_tests.auto_dispatch is True
        assert cfg.smoke_tests.timeout == 600

    def test_invalid_timeout_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "smoke_tests:\n  timeout: -1\n"
        )
        with pytest.raises(ConfigError, match="non-negative"):
            load(p)


# ── Machine selection ───────────────────────────────────────────────────────


class TestPickSmokeMachine:
    def test_prefers_different_machine(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        board = Board()
        m = pick_smoke_machine("laptop", "api", board, config)
        assert m is not None
        assert m.name == "server"

    def test_falls_back_to_same_machine(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        board = Board()
        m = pick_smoke_machine("laptop", "api", board, config)
        assert m is not None
        assert m.name == "laptop"

    def test_skips_busy_machines(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
        )
        board = Board(active=[
            Assignment(machine_name="server", repo_name="api", issue_number=1,
                       issue_title="x", status="running"),
        ])
        m = pick_smoke_machine("laptop", "api", board, config)
        assert m.name == "laptop"

    def test_returns_none_when_no_machine_available(self) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[
                Machine(name="laptop", host="l", repos=["other"], repo_paths={}),
            ],
        )
        board = Board()
        assert pick_smoke_machine("laptop", "api", board, config) is None


# ── Briefing ────────────────────────────────────────────────────────────────


class TestBuildSmokeBriefing:
    def test_includes_branch_and_commands(self) -> None:
        b = build_smoke_briefing(
            repo_name="api", branch="feat/x", issue_number=42,
            issue_title="Fix auth", build_command="make build",
            test_command="make test",
        )
        assert "feat/x" in b
        assert "make build" in b
        assert "make test" in b
        assert "#42" in b

    def test_no_commands_note(self) -> None:
        b = build_smoke_briefing(
            repo_name="api", branch="feat/x", issue_number=1,
            issue_title="x", build_command=None, test_command=None,
        )
        assert "No build or test commands" in b


# ── Dispatch ────────────────────────────────────────────────────────────────


class TestDispatchSmokeTest:
    @pytest.fixture
    def config(self) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api", build_command="make", test_command="make test")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
            smoke_tests=SmokeTestsConfig(enabled=True, auto_dispatch=True),
        )

    @pytest.fixture
    def completed(self) -> Assignment:
        return Assignment(
            machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="Fix auth", assignment_id="abc",
            status="done", branch="feat/auth", type="work",
        )

    def test_dispatches_smoke_test(self, config: Config, completed: Assignment) -> None:
        board = Board()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "smoke123"}
        mock_resp.raise_for_status = lambda: None
        mock_client.post.return_value = mock_resp

        result = dispatch_smoke_test(completed, board, config, http_client=mock_client)
        assert result is not None
        assert result.type == "smoke_test"
        assert result.assignment_id == "smoke123"
        assert result.branch == "feat/auth"
        assert result in board.active

    def test_skips_when_disabled(self, completed: Assignment) -> None:
        config = Config(
            repos=[Repo(name="api", github="a/a")],
            machines=[],
            smoke_tests=SmokeTestsConfig(enabled=False),
        )
        assert dispatch_smoke_test(completed, Board(), config) is None

    def test_skips_non_work_assignments(self, config: Config) -> None:
        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="x", status="done", branch="b", type="review",
        )
        assert dispatch_smoke_test(review, Board(), config) is None

    def test_skips_no_branch(self, config: Config) -> None:
        no_branch = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="x", status="done", branch=None, type="work",
        )
        assert dispatch_smoke_test(no_branch, Board(), config) is None

    def test_skips_failed_assignment(self, config: Config) -> None:
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="x", status="failed", branch="b", type="work",
        )
        assert dispatch_smoke_test(failed, Board(), config) is None


# ── Reconcile hook ──────────────────────────────────────────────────────────


class TestReconcileHook:
    @patch("coord.reconcile._query_agent")
    def test_smoke_test_auto_dispatched(self, mock_query: MagicMock) -> None:
        from coord.reconcile import reconcile

        config = Config(
            repos=[Repo(name="api", github="acme/api", build_command="make")],
            machines=[
                Machine(name="laptop", host="l", repos=["api"], repo_paths={"api": "/tmp/a"}),
                Machine(name="server", host="s", repos=["api"], repo_paths={"api": "/tmp/a"}),
            ],
            smoke_tests=SmokeTestsConfig(enabled=True, auto_dispatch=True),
        )
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=42,
                issue_title="Fix auth", assignment_id="work1",
                status="running", type="work",
            ),
        ])

        mock_query.return_value = {
            "active": [],
            "completed": [
                {"id": "work1", "status": "done", "branch": "feat/auth", "finished_at": 100.0},
            ],
        }

        with patch("coord.smoke.httpx") as mock_httpx:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"id": "smoke1"}
            mock_resp.raise_for_status = lambda: None
            mock_httpx.post.return_value = mock_resp

            changed = reconcile(board, config)

        assert "work1" in changed
        assert "smoke1" in changed
        smoke_assignments = [a for a in board.active if a.type == "smoke_test"]
        assert len(smoke_assignments) == 1
        assert smoke_assignments[0].issue_title == "[smoke] Fix auth"
