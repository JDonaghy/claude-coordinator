"""Tests for operational defaults baked into the coordinator.

Covers:
- WORKER_SYSTEM_PROMPT audit step and forbidden-files instruction
- Default ReviewsConfig checklist (platform-neutrality)
- coordinator_only_files parsing and dispatch injection
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coord.agent import WORKER_SYSTEM_PROMPT, AssignmentSpec, default_worker_command
from coord.config import ConfigError, ReviewsConfig, load
from coord.models import Machine, Proposal, Repo


# ── WORKER_SYSTEM_PROMPT audit step ─────────────────────────────────────────


def test_worker_system_prompt_contains_audit_step() -> None:
    """Workers must verify a feature doesn't already exist before coding."""
    assert "already implemented" in WORKER_SYSTEM_PROMPT


def test_worker_system_prompt_contains_forbidden_files_instruction() -> None:
    """Workers must be told not to read or modify forbidden files."""
    assert "forbidden files" in WORKER_SYSTEM_PROMPT
    assert "do NOT read or modify them" in WORKER_SYSTEM_PROMPT


def test_worker_system_prompt_requires_clean_build_before_done() -> None:
    """Workers must run the build, fix warnings, and not silently ship them.

    Motivated by smoke testing quadraui#233 — the build emitted warnings the
    worker should have fixed before declaring done, but the prompt didn't
    require it.  The human ended up cleaning up after the worker.
    """
    assert "Before declaring done" in WORKER_SYSTEM_PROMPT
    assert "warnings" in WORKER_SYSTEM_PROMPT
    assert "FIX THEM" in WORKER_SYSTEM_PROMPT
    # Escape hatch for genuinely unfixable warnings — workers must call them
    # out explicitly, not silently leave them.
    assert "explicitly call it out" in WORKER_SYSTEM_PROMPT


# ── Default ReviewsConfig checklist ─────────────────────────────────────────


def test_default_reviews_checklist_includes_platform_neutrality() -> None:
    cfg = ReviewsConfig()
    assert any(
        "platform-specific" in item for item in cfg.checklist
    ), f"expected platform-neutrality check in default checklist, got: {cfg.checklist}"


# ── coordinator_only_files config parsing ────────────────────────────────────


def _minimal_yaml(extra_repo_fields: str = "") -> str:
    return (
        "repos:\n"
        f"  - name: api\n    github: acme/api\n{extra_repo_fields}"
        "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
    )


def test_coordinator_only_files_parsed_from_config(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "    coordinator_only_files:\n"
        "      - CLAUDE.md\n"
        "      - coordinator.yml\n"
        "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
    )
    cfg = load(p)
    repo = cfg.repo("api")
    assert repo is not None
    assert repo.coordinator_only_files == ["CLAUDE.md", "coordinator.yml"]


def test_coordinator_only_files_empty_by_default(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(_minimal_yaml())
    cfg = load(p)
    repo = cfg.repo("api")
    assert repo is not None
    assert repo.coordinator_only_files == []


def test_coordinator_only_files_invalid_type_raises_config_error(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "    coordinator_only_files: not-a-list\n"
        "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
    )
    with pytest.raises(ConfigError, match="coordinator_only_files must be a list of strings"):
        load(p)


# ── dispatch() injects coordinator_only_files into files_forbidden ───────────


def _make_proposal(**overrides) -> Proposal:
    base = dict(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=10,
        issue_title="Fix auth",
        rationale="best fit",
        files_likely=["auth.py"],
        briefing="Fix the auth module",
    )
    base.update(overrides)
    return Proposal(**base)


def _make_config(repo: Repo) -> object:
    from coord.config import Config, ModelsConfig

    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            ),
        ],
        models=ModelsConfig(default="sonnet"),
    )


def test_dispatch_includes_coordinator_only_files_in_forbidden() -> None:
    from coord.dispatch import dispatch

    repo = Repo(
        name="api",
        github="acme/api",
        coordinator_only_files=["CLAUDE.md", "coordinator.yml"],
    )
    cfg = _make_config(repo)
    proposal = _make_proposal()

    with patch("coord.dispatch.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["files_forbidden"] == ["CLAUDE.md", "coordinator.yml"]


def test_dispatch_with_no_coordinator_only_files_sends_empty_forbidden() -> None:
    from coord.dispatch import dispatch

    repo = Repo(name="api", github="acme/api")
    cfg = _make_config(repo)
    proposal = _make_proposal()

    with patch("coord.dispatch.httpx.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["files_forbidden"] == []
