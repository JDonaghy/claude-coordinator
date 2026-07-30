"""End-to-end tests for `coord diagnose --graph` — driven through the real
Click command against real git checkouts, asserting on the rendered output.

This is the surface that makes graph drift *visible*: graphify's own hooks
skip rebase/merge/cherry-pick, fire on nothing for `git reset --hard`, and
swallow every background-rebuild failure, so a silently stale graph is the
normal failure mode rather than an exotic one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.commands.status import diagnose

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def _repo_with_graph(root: Path, *, built_sha: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", ".", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    head = _git("rev-parse", "HEAD", cwd=root).stdout.strip()
    out = root / "graphify-out"
    out.mkdir(exist_ok=True)
    (out / "graph.json").write_text('{"nodes": []}', encoding="utf-8")
    (out / "GRAPH_REPORT.md").write_text(
        f"- Built from commit: `{built_sha or head[:8]}`\n", encoding="utf-8"
    )
    return root


def _config_for(tmp_path: Path, repo_path: Path) -> Path:
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tailnet\n"
        "    capabilities: [python]\n"
        "    repos: [api]\n"
        "    repo_paths:\n"
        f"      api: {repo_path}\n",
        encoding="utf-8",
    )
    return cfg


def _run(cfg: Path) -> str:
    result = CliRunner().invoke(
        diagnose, ["--graph", "--config", str(cfg)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_reports_a_healthy_in_sync_graph(tmp_path: Path) -> None:
    repo = _repo_with_graph(tmp_path / "api")
    out = _run(_config_for(tmp_path, repo))

    assert "graph in sync" in out
    assert "GRAPH_HEALTH: checkouts=1 stale=0" in out


def test_reports_drift_when_head_moved_past_the_graph(tmp_path: Path) -> None:
    """The exact shape a rebase or `reset --hard` leaves behind: the graph is
    present and looks fine, but was built from a commit that is no longer HEAD."""
    repo = _repo_with_graph(tmp_path / "api")
    _git("commit", "-q", "--allow-empty", "-m", "moves HEAD", cwd=repo)

    out = _run(_config_for(tmp_path, repo))

    assert "STALE" in out
    assert "GRAPH_HEALTH: checkouts=1 stale=1" in out
    assert "graphify update ." in out, "must tell the operator how to fix it"


def test_flags_a_repo_without_the_bootstrap_hook(tmp_path: Path) -> None:
    """core.hooksPath is the one-time per-machine step that decides whether
    worktrees get a linked graph — invisible until an agent is graph-blind."""
    repo = _repo_with_graph(tmp_path / "api")
    out = _run(_config_for(tmp_path, repo))

    assert "no .githooks/post-checkout in this repo" in out


def test_reports_a_missing_graph_without_counting_it_stale(tmp_path: Path) -> None:
    repo = tmp_path / "api"
    repo.mkdir()
    _git("init", "-q", ".", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)

    out = _run(_config_for(tmp_path, repo))

    assert "never built here" in out
    assert "stale=0" in out


def test_handles_a_machine_whose_checkout_is_absent(tmp_path: Path) -> None:
    """coordinator.yml describes the whole fleet; only some checkouts exist on
    any given machine.  A path that isn't here must be skipped, not crash."""
    cfg = _config_for(tmp_path, tmp_path / "nonexistent")
    out = _run(cfg)

    assert "no local checkouts" in out
