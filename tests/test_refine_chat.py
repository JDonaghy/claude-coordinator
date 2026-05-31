"""Tests for the refinement-chat seed builder and dispatcher (#264)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coord.agent import AssignmentSpec, REFINEMENT_SYSTEM_PROMPT, default_worker_command
from coord.models import Machine, Repo
from coord import refine_chat


# ── build_refinement_briefing ────────────────────────────────────────────────


def test_briefing_includes_issue_body_and_title():
    out = refine_chat.build_refinement_briefing(
        repo_slug="owner/repo",
        issue_number=42,
        issue_title="Add foo",
        issue_body="We need foo because bar.",
        comments=[],
        claude_md="# project rules",
        file_tree="src/\n  main.rs",
    )
    assert "owner/repo#42: Add foo" in out
    assert "We need foo because bar." in out
    assert "# project rules" in out
    assert "main.rs" in out


def test_briefing_handles_empty_inputs():
    out = refine_chat.build_refinement_briefing(
        repo_slug="o/r",
        issue_number=1,
        issue_title="t",
        issue_body="",
        comments=[],
        claude_md="",
        file_tree="",
    )
    # Empty inputs produce placeholder stubs, not crashes.
    assert "ISSUE BODY:" in out
    assert "(empty)" in out
    assert "(none)" in out
    assert "(not found)" in out


def test_briefing_truncates_long_comment_body():
    long_body = "x" * (refine_chat.MAX_COMMENT_BODY_CHARS * 2)
    out = refine_chat.build_refinement_briefing(
        repo_slug="o/r",
        issue_number=1,
        issue_title="t",
        issue_body="b",
        comments=[{"author": {"login": "alice"}, "body": long_body}],
        claude_md="",
        file_tree="",
    )
    # Capped body length + truncation marker, plus the author line.
    assert "@alice" in out
    assert "…[truncated]" in out
    assert "x" * (refine_chat.MAX_COMMENT_BODY_CHARS + 100) not in out


# ── pick_refinement_machine ──────────────────────────────────────────────────


def _make_machine(name: str, repos: list[str], host: str = "host", path: str = "/tmp") -> Machine:
    return Machine(
        name=name,
        host=host,
        capabilities=[],
        repos=repos,
        repo_paths={r: f"{path}/{r}" for r in repos},
    )


def test_pick_machine_returns_first_qualified(tmp_path):
    # tmp_path fixture creates real dirs so machine.repo_path() resolves cleanly.
    a = _make_machine("a", ["x"], path=str(tmp_path))
    b = _make_machine("b", ["x", "y"], path=str(tmp_path))
    cfg = type("Cfg", (), {"machines": [a, b]})()
    picked = refine_chat.pick_refinement_machine(cfg, "x")  # type: ignore[arg-type]
    assert picked is a


def test_pick_machine_returns_none_when_no_match(tmp_path):
    a = _make_machine("a", ["x"], path=str(tmp_path))
    cfg = type("Cfg", (), {"machines": [a]})()
    assert refine_chat.pick_refinement_machine(cfg, "y") is None  # type: ignore[arg-type]


# ── _read_claude_md ──────────────────────────────────────────────────────────


def test_read_claude_md_returns_empty_when_missing(tmp_path):
    assert refine_chat._read_claude_md(tmp_path) == ""


def test_read_claude_md_returns_contents(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# my rules\n- be brief", encoding="utf-8")
    assert refine_chat._read_claude_md(tmp_path) == "# my rules\n- be brief"


def test_read_claude_md_truncates_overlong_file(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x" * (refine_chat.MAX_CLAUDE_MD_CHARS * 2), encoding="utf-8")
    out = refine_chat._read_claude_md(tmp_path)
    assert out.endswith("…[truncated]")
    assert len(out) <= refine_chat.MAX_CLAUDE_MD_CHARS + len("\n…[truncated]")


# ── _file_tree_snapshot ──────────────────────────────────────────────────────


def test_file_tree_snapshot_excludes_dotgit_and_target(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "build.log").write_text("noise")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}")
    snap = refine_chat._file_tree_snapshot(tmp_path)
    assert "src" in snap
    assert "main.rs" in snap
    assert ".git" not in snap
    assert "target" not in snap


# ── agent.py refinement branch ───────────────────────────────────────────────


def test_default_worker_command_refinement_uses_read_only_tools():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="refinement",
    )
    argv = default_worker_command(spec)
    # Find --allowedTools and check it.
    idx = argv.index("--allowedTools")
    assert argv[idx + 1] == "Read", (
        "refinement workers must be Read-only — got " + argv[idx + 1]
    )


def test_default_worker_command_refinement_uses_refinement_prompt_by_default():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="refinement",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    assert argv[idx + 1] == REFINEMENT_SYSTEM_PROMPT


def test_default_worker_command_refinement_honours_explicit_system_prompt():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=1,
        issue_title="t",
        briefing="b",
        type="refinement",
        system_prompt="custom seed",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    assert argv[idx + 1] == "custom seed"
