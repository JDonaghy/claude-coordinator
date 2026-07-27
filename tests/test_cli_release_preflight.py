"""Tests for `coord release-preflight` (#1471).

Main is now a protected branch: `git push origin main` can be silently
rejected while a subsequent `git push origin vX.Y.Z` still succeeds, since
they're independent refs. `release_preflight_checks` is meant to catch that
locally, before a tag is ever pushed, by confirming local main actually
matches origin/main (plus a clean tree and a consistent version bump).

These fixtures build real local-only git repos with a local bare "origin" so
the ancestor/head checks exercise the same git plumbing a real release does,
without touching the network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.commands.release import release_preflight_checks


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _write_versions(repo: Path, version: str) -> None:
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "claude-coordinator"\nversion = "{version}"\n'
    )
    coord_dir = repo / "coord"
    coord_dir.mkdir(exist_ok=True)
    (coord_dir / "__init__.py").write_text(f'__version__ = "{version}"\n')


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    """A clone on ``main``, in sync with a local bare ``origin``, versions
    consistent at 0.4.82 — the fully-green baseline other tests mutate."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "remote", "add", "origin", str(origin))
    _write_versions(clone, "0.4.82")
    _commit_all(clone, "initial")
    _git(clone, "push", "-u", "origin", "main")
    return clone


class TestReleasePreflightChecks:
    def test_clean_synced_repo_has_no_problems(self, clean_repo: Path) -> None:
        assert release_preflight_checks(clean_repo) == []

    def test_dirty_working_tree_is_flagged(self, clean_repo: Path) -> None:
        (clean_repo / "coord" / "__init__.py").write_text('__version__ = "0.4.83"\n')

        problems = release_preflight_checks(clean_repo)

        assert any("not clean" in p for p in problems)

    def test_local_main_ahead_of_origin_is_flagged(self, clean_repo: Path) -> None:
        """The #1471 core failure: a local commit that main's branch
        protection rejected. Local main has moved past origin/main —
        exactly the state that let a bad tag get pushed."""
        _write_versions(clean_repo, "0.4.83")
        _commit_all(clean_repo, "bump to 0.4.83")
        # Deliberately NOT pushed to origin — simulates the rejected push.

        problems = release_preflight_checks(clean_repo)

        assert any("!=" in p and "origin/main" in p for p in problems)

    def test_local_main_behind_origin_is_flagged(self, clean_repo: Path) -> None:
        """origin/main moved (e.g. someone else merged) and local wasn't
        pulled — also unsafe to tag from."""
        origin_url = _git(clean_repo, "remote", "get-url", "origin")
        other_clone_dir = clean_repo.parent / "other_clone"
        _git(clean_repo.parent, "clone", origin_url, str(other_clone_dir))
        _git(other_clone_dir, "config", "user.email", "t@t.com")
        _git(other_clone_dir, "config", "user.name", "Test")
        _write_versions(other_clone_dir, "0.4.83")
        _commit_all(other_clone_dir, "someone else's bump")
        _git(other_clone_dir, "push", "origin", "main")

        problems = release_preflight_checks(clean_repo)

        assert any("!=" in p and "origin/main" in p for p in problems)

    def test_version_mismatch_between_files_is_flagged(self, clean_repo: Path) -> None:
        (clean_repo / "coord" / "__init__.py").write_text('__version__ = "0.4.83"\n')
        _commit_all(clean_repo, "oops mismatched bump")
        _git(clean_repo, "push", "origin", "main")

        problems = release_preflight_checks(clean_repo)

        assert any("version mismatch" in p for p in problems)

    def test_not_on_main_is_flagged(self, clean_repo: Path) -> None:
        _git(clean_repo, "checkout", "-b", "some-feature-branch")

        problems = release_preflight_checks(clean_repo)

        assert any("not on main" in p for p in problems)

    def test_already_tagged_version_is_flagged(self, clean_repo: Path) -> None:
        _git(clean_repo, "tag", "v0.4.82")

        problems = release_preflight_checks(clean_repo)

        assert any("v0.4.82" in p and "already exists" in p for p in problems)

    def test_not_a_git_repo_is_flagged(self, tmp_path: Path) -> None:
        plain_dir = tmp_path / "not_a_repo"
        plain_dir.mkdir()

        problems = release_preflight_checks(plain_dir)

        assert len(problems) == 1
        assert "not a git checkout" in problems[0]


class TestReleasePreflightCli:
    def test_cli_exits_zero_on_clean_repo(self, clean_repo: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["release-preflight", "--path", str(clean_repo)])

        assert result.exit_code == 0
        assert "OK" in result.output

    def test_cli_exits_nonzero_and_lists_problems_when_dirty(self, clean_repo: Path) -> None:
        (clean_repo / "coord" / "__init__.py").write_text('__version__ = "0.4.83"\n')

        runner = CliRunner()
        result = runner.invoke(main, ["release-preflight", "--path", str(clean_repo)])

        assert result.exit_code != 0
        assert "FAILED" in result.output
