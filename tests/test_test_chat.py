"""Tests for the test-chat seed builder and dispatcher (#314 Phase B)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from coord.agent import (
    AssignmentSpec,
    TEST_CHAT_SYSTEM_PROMPT,
    default_worker_command,
)
from coord.models import Machine, Repo
from coord import test_chat


# ── build_test_briefing ──────────────────────────────────────────────────────


def test_briefing_includes_required_sections():
    out = test_chat.build_test_briefing(
        work_assignment_id="abc123",
        issue_number=42,
        issue_title="Add foo",
        repo_slug="owner/repo",
        branch="issue-42-add-foo",
        smoke_tests=["foo runs — open app — see foo"],
        run_cmd="cargo run",
        diff="--- a/src/main.rs\n+++ b/src/main.rs\n@@ -1,2 +1,3 @@",
        build_log="   Compiling foo v0.1.0\n   Finished dev",
        claude_md="# rules\n- be brief",
    )
    assert "owner/repo#42: Add foo" in out
    assert "PR DIFF:" in out
    assert "src/main.rs" in out
    assert "MOST RECENT BUILD LOG:" in out
    assert "Compiling foo" in out
    assert "WORKER SMOKE_TESTS:" in out
    assert "foo runs" in out
    assert "RUN COMMAND:" in out
    assert "cargo run" in out
    assert "PROJECT CLAUDE.md:" in out
    assert "be brief" in out


def test_briefing_handles_empty_smoke_tests():
    out = test_chat.build_test_briefing(
        work_assignment_id="x",
        issue_number=1,
        issue_title="t",
        repo_slug="o/r",
        branch=None,
        smoke_tests=[],
        run_cmd=None,
        diff="",
        build_log="",
        claude_md="",
    )
    assert "(none — worker reported change is internal)" in out


def test_briefing_handles_none_smoke_tests():
    out = test_chat.build_test_briefing(
        work_assignment_id="x",
        issue_number=1,
        issue_title="t",
        repo_slug="o/r",
        branch=None,
        smoke_tests=None,
        run_cmd=None,
        diff="",
        build_log="",
        claude_md="",
    )
    assert "(worker did not emit a SMOKE_TESTS block)" in out


def test_briefing_handles_no_run_cmd():
    out = test_chat.build_test_briefing(
        work_assignment_id="x",
        issue_number=1,
        issue_title="t",
        repo_slug="o/r",
        branch=None,
        smoke_tests=None,
        run_cmd=None,
        diff="",
        build_log="",
        claude_md="",
    )
    assert "(not configured)" in out


def test_briefing_truncates_diff(tmp_path):
    # Each line is one character + newline = cheap to generate many.
    big_diff = "\n".join([f"line{i}" for i in range(test_chat.MAX_DIFF_LINES * 2)])
    out = test_chat.build_test_briefing(
        work_assignment_id="x",
        issue_number=1,
        issue_title="t",
        repo_slug="o/r",
        branch="b",
        smoke_tests=None,
        run_cmd=None,
        diff=big_diff,
        build_log="",
        claude_md="",
    )
    assert "truncated" in out
    # The full diff must NOT appear in the briefing (it would exceed MAX_DIFF_LINES
    # if every line was there).
    assert f"line{test_chat.MAX_DIFF_LINES + 1}" not in out


# ── _read_build_log ──────────────────────────────────────────────────────────


def test_read_build_log_missing(tmp_path):
    """Returns placeholder when the log file doesn't exist."""
    with patch.object(Path, "home", return_value=tmp_path):
        result = test_chat._read_build_log("nonexistent-id")
    assert "(no build log" in result


def test_read_build_log_present(tmp_path):
    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir()
    log = coord_dir / "test-build-myid.log"
    log.write_text("--- stdout ---\nok\n--- stderr ---\n", encoding="utf-8")
    with patch.object(Path, "home", return_value=tmp_path):
        result = test_chat._read_build_log("myid")
    assert "stdout" in result
    assert "ok" in result


def test_read_build_log_truncates(tmp_path):
    coord_dir = tmp_path / ".coord"
    coord_dir.mkdir()
    log = coord_dir / "test-build-bigid.log"
    content = "\n".join([f"line{i}" for i in range(test_chat.MAX_BUILD_LOG_LINES * 2)])
    log.write_text(content, encoding="utf-8")
    with patch.object(Path, "home", return_value=tmp_path):
        result = test_chat._read_build_log("bigid")
    assert "truncated" in result
    assert f"line{test_chat.MAX_BUILD_LOG_LINES + 1}" not in result


# ── _read_claude_md ──────────────────────────────────────────────────────────


def test_read_claude_md_missing(tmp_path):
    assert test_chat._read_claude_md(tmp_path) == ""


def test_read_claude_md_present(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# rules\n- be brief", encoding="utf-8")
    assert test_chat._read_claude_md(tmp_path) == "# rules\n- be brief"


def test_read_claude_md_truncates(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "x" * (test_chat.MAX_CLAUDE_MD_CHARS * 2), encoding="utf-8"
    )
    out = test_chat._read_claude_md(tmp_path)
    assert out.endswith("…[truncated]")
    assert len(out) <= test_chat.MAX_CLAUDE_MD_CHARS + len("\n…[truncated]")


# ── pick_test_chat_machine ───────────────────────────────────────────────────


def _make_machine(name: str, repos: list[str], path: str = "/tmp") -> Machine:
    return Machine(
        name=name,
        host="host",
        capabilities=[],
        repos=repos,
        repo_paths={r: f"{path}/{r}" for r in repos},
    )


def test_pick_machine_returns_first_qualified(tmp_path):
    a = _make_machine("a", ["x"], path=str(tmp_path))
    b = _make_machine("b", ["x", "y"], path=str(tmp_path))
    cfg = type("Cfg", (), {"machines": [a, b]})()
    picked = test_chat.pick_test_chat_machine(cfg, "x")  # type: ignore[arg-type]
    assert picked is a


def test_pick_machine_returns_none_when_no_match(tmp_path):
    a = _make_machine("a", ["x"], path=str(tmp_path))
    cfg = type("Cfg", (), {"machines": [a]})()
    assert test_chat.pick_test_chat_machine(cfg, "y") is None  # type: ignore[arg-type]


# ── agent.py: test-chat branch ───────────────────────────────────────────────


def test_default_worker_command_test_chat_allows_read_and_bash():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="test-chat",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--allowedTools")
    tools = argv[idx + 1]
    assert "Read" in tools
    assert "Bash" in tools


def test_default_worker_command_test_chat_not_write():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="test-chat",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--allowedTools")
    tools = argv[idx + 1]
    assert "Edit" not in tools
    assert "Write" not in tools


def test_default_worker_command_test_chat_uses_test_chat_prompt_by_default():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="test-chat",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    assert argv[idx + 1] == TEST_CHAT_SYSTEM_PROMPT


def test_default_worker_command_test_chat_honours_explicit_system_prompt():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="test-chat",
        system_prompt="custom",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    assert argv[idx + 1].startswith("custom")


def test_default_worker_command_test_chat_applies_deny_prompt():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="test-chat",
        deny_commands=["gh *"],
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    prompt = argv[idx + 1]
    assert "FORBIDDEN" in prompt
    assert "gh *" in prompt


# ── CLI smoke test ────────────────────────────────────────────────────────────


def test_cli_test_chat_missing_assignment(tmp_path, coord_db):
    """coord test-chat should exit with error when the assignment isn't in the DB."""
    from click.testing import CliRunner
    from coord.cli import main

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["test-chat", "nonexistent-assignment-id"],
        catch_exceptions=False,
    )
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "error" in result.output.lower()
