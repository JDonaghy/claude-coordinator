"""Tests for worker command deny-list (Phase 1 safety)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.agent import (
    AssignmentSpec,
    WORKER_SYSTEM_PROMPT,
    build_deny_prompt,
    default_worker_command,
)
from coord.config import DEFAULT_DENY_COMMANDS, load
from coord.models import WorkerPermissionsConfig


# ── build_deny_prompt ────────────────────────────────────────────────────────


class TestBuildDenyPrompt:
    def test_empty_deny_list_returns_empty_string(self) -> None:
        assert build_deny_prompt([]) == ""

    def test_formats_patterns_into_prompt(self) -> None:
        result = build_deny_prompt(["Bash(git push --force *)", "Bash(rm -rf *)"])
        assert "FORBIDDEN COMMANDS" in result
        assert "git push --force *" in result
        assert "rm -rf *" in result
        assert "STUCK:" in result

    def test_strips_bash_wrapper(self) -> None:
        result = build_deny_prompt(["Bash(git reset --hard *)"])
        # The human-readable line should not include the Bash() wrapper.
        assert "- git reset --hard *" in result
        assert "Bash(" not in result

    def test_preserves_plain_patterns(self) -> None:
        result = build_deny_prompt(["rm -rf /"])
        assert "- rm -rf /" in result


# ── default_worker_command includes deny-list ────────────────────────────────


def _spec(**overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="do the thing",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


class TestDefaultWorkerCommand:
    def test_deny_commands_appear_in_system_prompt(self) -> None:
        spec = _spec(deny_commands=["Bash(git push --force *)"])
        argv = default_worker_command(spec)
        # The system prompt is the arg after --system-prompt.
        idx = argv.index("--system-prompt")
        system_prompt = argv[idx + 1]
        assert "FORBIDDEN COMMANDS" in system_prompt
        assert "git push --force *" in system_prompt

    def test_no_deny_commands_no_forbidden_section(self) -> None:
        spec = _spec(deny_commands=[])
        argv = default_worker_command(spec)
        idx = argv.index("--system-prompt")
        system_prompt = argv[idx + 1]
        assert "FORBIDDEN COMMANDS" not in system_prompt
        # The base prompt should still be there.
        assert "Claude Code worker" in system_prompt

    def test_base_prompt_always_present(self) -> None:
        spec = _spec(deny_commands=["Bash(rm -rf *)"])
        argv = default_worker_command(spec)
        idx = argv.index("--system-prompt")
        system_prompt = argv[idx + 1]
        assert WORKER_SYSTEM_PROMPT in system_prompt

    def test_stream_json_flags_present(self) -> None:
        """Workers must launch with stream-json output for observability."""
        spec = _spec()
        argv = default_worker_command(spec)
        assert "--output-format" in argv
        idx = argv.index("--output-format")
        assert argv[idx + 1] == "stream-json"
        assert "--verbose" in argv

    def test_input_format_stream_json_for_injection(self) -> None:
        """Workers must launch with stream-json input so messages can be
        injected mid-session via AgentServer.inject_message."""
        spec = _spec()
        argv = default_worker_command(spec)
        assert "--input-format" in argv
        idx = argv.index("--input-format")
        assert argv[idx + 1] == "stream-json"

    def test_briefing_not_appended_as_positional_arg(self) -> None:
        """Briefing is sent via stdin (first stream-json user message),
        not as a positional argv entry — keeping a positional briefing
        would force claude into text input mode and break injection."""
        spec = _spec(briefing="DO NOT APPEAR IN ARGV")
        argv = default_worker_command(spec)
        assert spec.briefing not in argv


# ── Config parsing ───────────────────────────────────────────────────────────


class TestWorkerPermissionsConfigParsing:
    def test_default_deny_list_when_no_config(self, tmp_path: Path) -> None:
        """Repos without explicit worker_permissions get the default deny-list."""
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        repo = cfg.repo("api")
        assert repo is not None
        assert repo.worker_permissions is not None
        assert repo.worker_permissions.deny == DEFAULT_DENY_COMMANDS

    def test_custom_deny_list(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    worker_permissions:\n"
            "      deny:\n"
            "        - 'Bash(rm -rf /)'\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        repo = cfg.repo("api")
        assert repo is not None
        assert repo.worker_permissions is not None
        assert repo.worker_permissions.deny == ["Bash(rm -rf /)"]

    def test_empty_deny_list_means_no_restrictions(self, tmp_path: Path) -> None:
        """An explicit `deny: []` clears all restrictions."""
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    worker_permissions:\n"
            "      deny: []\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        repo = cfg.repo("api")
        assert repo is not None
        assert repo.worker_permissions is not None
        assert repo.worker_permissions.deny == []

    def test_allow_list_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    worker_permissions:\n"
            "      allow:\n"
            "        - 'Bash(npm install)'\n"
            "      deny: []\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        repo = cfg.repo("api")
        assert repo.worker_permissions.allow == ["Bash(npm install)"]
        assert repo.worker_permissions.deny == []

    def test_invalid_worker_permissions_type(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    worker_permissions: true\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        with pytest.raises(Exception, match="worker_permissions must be a mapping"):
            load(p)

    def test_invalid_deny_type(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    worker_permissions:\n"
            "      deny: not-a-list\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        with pytest.raises(Exception, match="deny must be a list"):
            load(p)


# ── Dispatch payload carries deny_commands ───────────────────────────────────


class TestDispatchDenyCommands:
    def test_dispatch_includes_deny_commands_in_payload(self) -> None:
        from unittest.mock import MagicMock, patch

        from coord.config import Config
        from coord.dispatch import dispatch
        from coord.models import Machine, Repo

        repo = Repo(
            name="api",
            github="acme/api",
            worker_permissions=WorkerPermissionsConfig(
                deny=["Bash(git push --force *)"]
            ),
        )
        cfg = Config(
            repos=[repo],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tailnet",
                    repos=["api"],
                    repo_paths={"api": "/home/user/src/api"},
                ),
            ],
        )
        from coord.models import Proposal

        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix auth",
            rationale="best fit",
            files_likely=["auth.py"],
            briefing="Fix the auth module",
        )

        with patch("coord.dispatch.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"ok": True}
            mock_post.return_value = mock_resp

            dispatch(proposal, cfg)

            payload = mock_post.call_args.kwargs["json"]
            assert payload["deny_commands"] == ["Bash(git push --force *)"]

    def test_dispatch_default_deny_when_no_permissions(self) -> None:
        """When worker_permissions uses defaults, deny_commands is the default list."""
        from unittest.mock import MagicMock, patch

        from coord.config import Config
        from coord.dispatch import dispatch
        from coord.models import Machine, Repo

        repo = Repo(
            name="api",
            github="acme/api",
            worker_permissions=WorkerPermissionsConfig(
                deny=list(DEFAULT_DENY_COMMANDS)
            ),
        )
        cfg = Config(
            repos=[repo],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tailnet",
                    repos=["api"],
                    repo_paths={"api": "/home/user/src/api"},
                ),
            ],
        )
        from coord.models import Proposal

        proposal = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix auth",
            rationale="best fit",
        )

        with patch("coord.dispatch.httpx.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"ok": True}
            mock_post.return_value = mock_resp

            dispatch(proposal, cfg)

            payload = mock_post.call_args.kwargs["json"]
            assert payload["deny_commands"] == DEFAULT_DENY_COMMANDS


# ── WorkerPermissionsConfig dataclass ────────────────────────────────────────


class TestWorkerPermissionsConfigDataclass:
    def test_defaults_are_empty_lists(self) -> None:
        wp = WorkerPermissionsConfig()
        assert wp.allow == []
        assert wp.deny == []

    def test_constructed_with_values(self) -> None:
        wp = WorkerPermissionsConfig(
            allow=["Bash(npm install)"],
            deny=["Bash(rm -rf *)"],
        )
        assert wp.allow == ["Bash(npm install)"]
        assert wp.deny == ["Bash(rm -rf *)"]
