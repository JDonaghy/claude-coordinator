"""Tests for the new-issue-chat seed builder and dispatcher (#316)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coord.agent import AssignmentSpec, NEW_ISSUE_CHAT_SYSTEM_PROMPT, default_worker_command
from coord.models import Machine, Repo
from coord import new_issue_chat


# ── build_new_issue_briefing ─────────────────────────────────────────────────


def test_briefing_includes_repo_slug_and_guidance():
    out = new_issue_chat.build_new_issue_briefing(
        repo_slug="owner/repo",
        claude_md="# project rules",
        new_issue_guidance="Required sections: Title, What, Acceptance",
        open_issues=[],
    )
    assert "owner/repo" in out
    assert "# project rules" in out
    assert "Required sections" in out


def test_briefing_includes_open_issue_titles():
    issues = [
        {"number": 1, "title": "Add dark mode"},
        {"number": 2, "title": "Fix login redirect"},
    ]
    out = new_issue_chat.build_new_issue_briefing(
        repo_slug="owner/repo",
        claude_md="",
        new_issue_guidance="",
        open_issues=issues,
    )
    assert "#1: Add dark mode" in out
    assert "#2: Fix login redirect" in out


def test_briefing_handles_empty_open_issues():
    out = new_issue_chat.build_new_issue_briefing(
        repo_slug="o/r",
        claude_md="",
        new_issue_guidance="",
        open_issues=[],
    )
    assert "(none fetched)" in out


def test_briefing_handles_empty_claude_md():
    out = new_issue_chat.build_new_issue_briefing(
        repo_slug="o/r",
        claude_md="",
        new_issue_guidance="Some guidance",
        open_issues=[],
    )
    assert "(not found)" in out
    assert "Some guidance" in out


def test_briefing_dedup_titles_smoke():
    """open_issues list appears in output so the worker can detect dupes."""
    issues = [{"number": i, "title": f"Issue {i}"} for i in range(1, 21)]
    out = new_issue_chat.build_new_issue_briefing(
        repo_slug="o/r",
        claude_md="",
        new_issue_guidance="",
        open_issues=issues,
    )
    # All 20 issues should appear.
    for i in range(1, 21):
        assert f"#{i}:" in out


# ── guidance-from-file path ──────────────────────────────────────────────────


def test_briefing_with_guidance_from_file(tmp_path):
    guidance_file = tmp_path / "docs" / "ISSUE_GUIDANCE.md"
    guidance_file.parent.mkdir(parents=True)
    guidance_file.write_text("## From file\n- Rule 1", encoding="utf-8")
    repo = Repo(name="r", github="o/r", new_issue_guidance="docs/ISSUE_GUIDANCE.md")
    resolved = repo.resolve_new_issue_guidance(tmp_path)
    out = new_issue_chat.build_new_issue_briefing(
        repo_slug="o/r",
        claude_md="",
        new_issue_guidance=resolved,
        open_issues=[],
    )
    assert "## From file" in out
    assert "Rule 1" in out


def test_briefing_with_inline_guidance():
    repo = Repo(name="r", github="o/r", new_issue_guidance="**Must have:** Title, What")
    resolved = repo.resolve_new_issue_guidance(Path("/does/not/exist"))
    assert "**Must have:**" in resolved
    out = new_issue_chat.build_new_issue_briefing(
        repo_slug="o/r",
        claude_md="",
        new_issue_guidance=resolved,
        open_issues=[],
    )
    assert "**Must have:**" in out


def test_briefing_guidance_missing_uses_default(tmp_path):
    repo = Repo(name="r", github="o/r", new_issue_guidance=None)
    resolved = repo.resolve_new_issue_guidance(tmp_path)
    # The generic default must mention the required sections.
    assert "Title" in resolved
    assert "Acceptance" in resolved


# ── pick_new_issue_chat_machine ──────────────────────────────────────────────


def _make_machine(name: str, repos: list[str], host: str = "host", path: str = "/tmp") -> Machine:
    return Machine(
        name=name,
        host=host,
        capabilities=[],
        repos=repos,
        repo_paths={r: f"{path}/{r}" for r in repos},
    )


def test_pick_machine_returns_first_qualified(tmp_path):
    a = _make_machine("a", ["x"], path=str(tmp_path))
    b = _make_machine("b", ["x", "y"], path=str(tmp_path))
    cfg = type("Cfg", (), {"machines": [a, b]})()
    picked = new_issue_chat.pick_new_issue_chat_machine(cfg, "x")  # type: ignore[arg-type]
    assert picked is a


def test_pick_machine_returns_none_when_no_match(tmp_path):
    a = _make_machine("a", ["x"], path=str(tmp_path))
    cfg = type("Cfg", (), {"machines": [a]})()
    assert new_issue_chat.pick_new_issue_chat_machine(cfg, "y") is None  # type: ignore[arg-type]


# ── agent.py new-issue-chat branch ──────────────────────────────────────────


def test_default_worker_command_new_issue_chat_uses_read_bash():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=0,
        issue_title="(new issue draft)",
        briefing="b",
        type="new-issue-chat",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--allowedTools")
    assert argv[idx + 1] == "Read,Bash", (
        "new-issue-chat workers must use Read,Bash — got " + argv[idx + 1]
    )


def test_default_worker_command_new_issue_chat_uses_new_issue_prompt():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=0,
        issue_title="(new issue draft)",
        briefing="b",
        type="new-issue-chat",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    # The system prompt is the NEW_ISSUE_CHAT_SYSTEM_PROMPT + deny block.
    assert NEW_ISSUE_CHAT_SYSTEM_PROMPT in argv[idx + 1]


def test_default_worker_command_new_issue_chat_deny_list_blocks_gh_issue_create():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=0,
        issue_title="(new issue draft)",
        briefing="b",
        type="new-issue-chat",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    system_prompt = argv[idx + 1]
    assert "gh issue create" in system_prompt
    assert "FORBIDDEN" in system_prompt


def test_default_worker_command_new_issue_chat_honours_explicit_system_prompt():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=0,
        issue_title="(new issue draft)",
        briefing="b",
        type="new-issue-chat",
        system_prompt="custom prompt",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    # Custom prompt is used; deny block is still appended.
    assert argv[idx + 1].startswith("custom prompt")
    assert "FORBIDDEN" in argv[idx + 1]
