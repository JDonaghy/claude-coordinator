"""CLI tests for `coord acceptance run` / `coord acceptance record` (#944)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Proposal


CONFIG_YAML = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
    repo_paths:
      coord-tui: {repo_path}
acceptance:
  drivers:
    coord-tui:
      kind: tui-tuidriver
      run: {run_cmd}
"""


def _write_config(tmp_path: Path, *, repo_path: str, run_cmd: str) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML.format(repo_path=repo_path, run_cmd=json.dumps(run_cmd)))
    return p


def _write_manifest(acceptance_root: Path, mapping: dict[str, int]) -> None:
    ms = acceptance_root / "ms01"
    ms.mkdir(parents=True, exist_ok=True)
    tests_yaml = "\n".join(f"  {k}: {v}" for k, v in mapping.items())
    (ms / "manifest.yml").write_text(f"tests:\n{tests_yaml}\n")


class TestAcceptanceRun:
    def test_run_scoped_to_issue_all_pass(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::b", "status": "pass"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 944, "ms01::b": 944})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["issue"] == 944
        assert payload["total"] == 2
        assert payload["green"] is True

    def test_run_reports_failure_and_nonzero_exit(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::b", "status": "fail", "message": "expected A got B"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 944, "ms01::b": 944})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["failed"] == 1
        assert payload["green"] is False

    def test_run_filters_out_other_issues_tests(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "pass"},
            {"id": "ms01::other", "status": "fail"},
        ]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(
            cwd / "tests" / "acceptance", {"ms01::a": 944, "ms01::other": 945},
        )
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # Only ms01::a belongs to #944 — #945's failure must not leak in.
        assert payload["total"] == 1
        assert payload["green"] is True

    def test_run_all_ignores_manifest(self, tmp_path: Path) -> None:
        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        cwd = tmp_path / "repo"
        cwd.mkdir()
        # No manifest at all — --all must still work.
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["scope"] == "all"
        assert payload["total"] == 1

    def test_run_requires_issue_or_all(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "--issue N or --all" in result.output

    def test_run_missing_driver_errors(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: coord-tui\n    github: acme/coord-tui\n"
            "machines:\n  - name: laptop\n    host: laptop.tail\n    repos: [coord-tui]\n"
        )
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--all", "--config", str(p),
        ])
        assert result.exit_code == 1
        assert "no acceptance driver configured" in result.output

    def test_run_missing_manifest_errors(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "not been authored" in result.output

    def test_run_issue_with_no_slice_errors(self, tmp_path: Path) -> None:
        cwd = tmp_path / "repo"
        cwd.mkdir()
        _write_manifest(cwd / "tests" / "acceptance", {"ms01::a": 1})
        config_path = _write_config(tmp_path, repo_path=str(cwd), run_cmd="echo '{}'")
        result = CliRunner().invoke(main, [
            "acceptance", "run", "--repo", "coord-tui", "--issue", "944",
            "--path", str(cwd), "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no acceptance slice" in result.output


def _init_git_repo(path: Path, *, manifest: dict[str, int] | None = None) -> str:
    """Create a minimal git repo (with a real "origin" remote — a bare repo
    alongside it) and one commit pushed to origin; returns the commit SHA.

    ``coord acceptance record`` always does a real ``git fetch origin``
    before checking out the worktree, so the test repo needs an actual
    origin remote, not just local history.

    When *manifest* is given, ``tests/acceptance/ms01/manifest.yml`` mapping
    each test id to its issue number is committed too — ``record`` checks
    out this exact SHA in a throwaway worktree, so the manifest must be part
    of history at the SHA being recorded, not just sitting in the base
    checkout's working tree.
    """
    bare = path.parent / f"{path.name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    if manifest:
        _write_manifest(path / "tests" / "acceptance", manifest)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", branch], cwd=path, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return sha


@pytest.fixture(autouse=True)
def _acceptance_worktrees_in_tmp(monkeypatch, tmp_path: Path):
    """Keep `coord acceptance record`'s throwaway worktree under tmp_path —
    the real implementation lives under ``~/.coord/acceptance-worktrees/``
    (outside the base checkout, mirroring `coord test`'s worktree), which
    must never leak into the real user's home directory during tests."""
    wt_root = tmp_path / "acceptance-worktrees"

    def _fake_path(repo_name: str, issue_number: int) -> Path:
        return wt_root / f"{repo_name}-{issue_number}"

    monkeypatch.setattr(
        "coord.commands.acceptance._acceptance_worktree_path", _fake_path,
    )


def _acceptance_row(coord_db, assignment_id: str) -> dict:
    row = coord_db.execute(
        "SELECT acceptance_state, acceptance_reason, acceptance_sha "
        "FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    assert row is not None, f"no assignment row for {assignment_id!r}"
    return dict(row)


class TestAcceptanceRecord:
    def test_record_passed_writes_board_verdict(self, tmp_path: Path, coord_db) -> None:
        from coord import state

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        state.record_dispatched(
            assignment_id="aid-1",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output
        assert "Acceptance PASSED" in result.output

        row = _acceptance_row(coord_db, "aid-1")
        assert row["acceptance_state"] == "passed"
        assert row["acceptance_sha"] == sha

    def test_record_failed_writes_board_verdict_and_context(
        self, tmp_path: Path, coord_db,
    ) -> None:
        from coord import state

        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [
            {"id": "ms01::a", "status": "fail", "message": "expected A got B"},
        ]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        state.record_dispatched(
            assignment_id="aid-2",
            proposal=Proposal(
                id=1, machine_name="laptop", repo_name="coord-tui",
                issue_number=944, issue_title="oracle loop runner", rationale="",
            ),
            repo_github="acme/coord-tui",
        )

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "Acceptance FAILED" in result.output

        row = _acceptance_row(coord_db, "aid-2")
        assert row["acceptance_state"] == "failed"
        assert "expected A got B" in (row["acceptance_reason"] or "")

        # #603: a failure is recorded as durable per-issue context too.
        entries = state.list_issue_context("coord-tui", 944)
        assert any("Acceptance FAILED" in e["body"] for e in entries)

    def test_record_no_work_assignment_errors(self, tmp_path: Path, coord_db) -> None:
        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest={"ms01::a": 944})

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "no work assignment found" in result.output

        # #944 review: this is a lookup error (no `work` assignment for the
        # repo/issue), not a real failing-verdict "kept for inspection" case
        # — the throwaway worktree must not be left behind.
        wt_path = tmp_path / "acceptance-worktrees" / "coord-tui-944"
        assert not wt_path.exists(), "worktree leaked on no-work-assignment error path"

    def test_record_manifest_missing_cleans_up_worktree(
        self, tmp_path: Path, coord_db,
    ) -> None:
        # No manifest committed at all — `_scoped_verdict` exit(1)s inside
        # `dump_manifest_error_hint` before a work-assignment lookup ever
        # happens; that path must clean up the worktree too (#944 review).
        repo_dir = tmp_path / "repo"
        sha = _init_git_repo(repo_dir, manifest=None)

        blob = json.dumps({"tests": [{"id": "ms01::a", "status": "pass"}]})
        config_path = _write_config(tmp_path, repo_path=str(repo_dir), run_cmd=f"echo '{blob}'")

        result = CliRunner().invoke(main, [
            "acceptance", "record", "--repo", "coord-tui", "--issue", "944",
            "--sha", sha, "--config", str(config_path),
        ])
        assert result.exit_code == 1
        assert "not been authored" in result.output

        wt_path = tmp_path / "acceptance-worktrees" / "coord-tui-944"
        assert not wt_path.exists(), "worktree leaked on manifest-missing error path"
