"""Tests for `coord release-preflight` (#1471).

Main is now a protected branch: `git push origin main` can be silently
rejected while a subsequent `git push origin vX.Y.Z` still succeeds, since
they're independent refs. `release_preflight_checks` is meant to catch that
locally, before a tag is ever pushed, by confirming local main actually
matches origin/main (plus a clean tree).

#1238: the version is now single-sourced from the git tag (setuptools-scm)
rather than hand-maintained `pyproject.toml`/`coord/__init__.py` literals, so
the checks that used to compare those two files (and guard against re-using
an already-tagged version) are gone along with the literals themselves —
see `release_preflight_checks`'s docstring.

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


def _write_marker_file(repo: Path, content: str) -> None:
    """A trivial tracked file to commit/dirty — stands in for whatever a real
    release's source changes are, now that there's no version file to bump."""
    (repo / "MARKER.txt").write_text(content)


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    """A clone on ``main``, in sync with a local bare ``origin`` — the fully
    green baseline other tests mutate."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "remote", "add", "origin", str(origin))
    _write_marker_file(clone, "initial\n")
    _commit_all(clone, "initial")
    _git(clone, "push", "-u", "origin", "main")
    return clone


class TestReleasePreflightChecks:
    def test_clean_synced_repo_has_no_problems(self, clean_repo: Path) -> None:
        assert release_preflight_checks(clean_repo) == []

    def test_dirty_working_tree_is_flagged(self, clean_repo: Path) -> None:
        _write_marker_file(clean_repo, "uncommitted change\n")

        problems = release_preflight_checks(clean_repo)

        assert any("not clean" in p for p in problems)

    def test_local_main_ahead_of_origin_is_flagged(self, clean_repo: Path) -> None:
        """The #1471 core failure: a local commit that main's branch
        protection rejected. Local main has moved past origin/main —
        exactly the state that let a bad tag get pushed."""
        _write_marker_file(clean_repo, "unpushed change\n")
        _commit_all(clean_repo, "unpushed change")
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
        _write_marker_file(other_clone_dir, "someone else's change\n")
        _commit_all(other_clone_dir, "someone else's change")
        _git(other_clone_dir, "push", "origin", "main")

        problems = release_preflight_checks(clean_repo)

        assert any("!=" in p and "origin/main" in p for p in problems)

    def test_not_on_main_is_flagged(self, clean_repo: Path) -> None:
        _git(clean_repo, "checkout", "-b", "some-feature-branch")

        problems = release_preflight_checks(clean_repo)

        assert any("not on main" in p for p in problems)

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
        _write_marker_file(clean_repo, "uncommitted change\n")

        runner = CliRunner()
        result = runner.invoke(main, ["release-preflight", "--path", str(clean_repo)])

        assert result.exit_code != 0
        assert "FAILED" in result.output
