"""End-to-end tests for `coord diagnose --self` — driven through the real
Click command against real git checkouts, asserting on rendered output
(CLAUDE.md black-box coverage bar).

#2436: nothing previously checked whether the coordinator's own editable
`coord/**` install had actually been `git pull`ed since a fix merged — a
coordinator host could run stale code indefinitely with zero signal, because
every symptom (board rows, `coord diagnose` findings, escalations) reads
exactly as if the bug were still unfixed. This drives `coord diagnose --self`
hermetically via $COORD_SELF_CHECKOUT, never touching the real running
install.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.commands.status import diagnose

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def _make_checkout(root: Path) -> Path:
    checkout = root / "coord"
    checkout.mkdir(parents=True)
    _git("init", "-q", "-b", "main", ".", cwd=checkout)
    (checkout / "marker.txt").write_text("v1\n", encoding="utf-8")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    remote = root / "coord-remote.git"
    _git("init", "-q", "-b", "main", "--bare", str(remote), cwd=root)
    _git("remote", "add", "origin", str(remote), cwd=checkout)
    _git("push", "-q", "-u", "origin", "HEAD:main", cwd=checkout)
    return checkout


def _run(monkeypatch, checkout: Path, *extra_args: str) -> str:
    monkeypatch.setenv("COORD_SELF_CHECKOUT", str(checkout))
    result = CliRunner().invoke(
        diagnose, ["--self", *extra_args], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_reports_up_to_date_install(tmp_path: Path, monkeypatch) -> None:
    checkout = _make_checkout(tmp_path)
    out = _run(monkeypatch, checkout)

    assert "up to date with origin/main" in out
    assert "SELF_FRESHNESS: git_checkout=true stale=false commits_behind=0" in out


def test_reports_stale_with_commit_count_behind(tmp_path: Path, monkeypatch) -> None:
    """The core #2436/#2286 acceptance: a merged fix nobody pulled shows up
    as STALE with a commit count, not silently as healthy."""
    checkout = _make_checkout(tmp_path)

    clone = tmp_path / "other-clone"
    _git("clone", "-q", str(tmp_path / "coord-remote.git"), str(clone), cwd=tmp_path)
    (clone / "marker.txt").write_text("v2 - the fix\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "the fix", cwd=clone)
    _git("push", "-q", "origin", "HEAD:main", cwd=clone)

    out = _run(monkeypatch, checkout)

    assert "STALE" in out
    assert "1 commit behind" in out
    assert f"git -C {checkout} pull" in out
    assert "SELF_FRESHNESS: git_checkout=true stale=true commits_behind=1" in out


def test_no_fetch_flag_skips_the_network_call(tmp_path: Path, monkeypatch) -> None:
    """--no-fetch must compare against the last known ref without ever
    running `git fetch` — a stale-but-unfetched checkout reads as caught up
    on the axis it can actually see (still not silently 'stale')."""
    checkout = _make_checkout(tmp_path)

    clone = tmp_path / "other-clone"
    _git("clone", "-q", str(tmp_path / "coord-remote.git"), str(clone), cwd=tmp_path)
    (clone / "marker.txt").write_text("v2\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "v2", cwd=clone)
    _git("push", "-q", "origin", "HEAD:main", cwd=clone)

    out = _run(monkeypatch, checkout, "--no-fetch")

    assert "STALE" not in out
    assert "SELF_FRESHNESS: git_checkout=true stale=false commits_behind=0" in out


def test_reports_a_non_git_install_neutrally(tmp_path: Path, monkeypatch) -> None:
    install = tmp_path / "site-packages" / "coord"
    install.mkdir(parents=True)

    out = _run(monkeypatch, install)

    assert "not a git checkout" in out
    assert f"SELF_FRESHNESS: git_checkout=false path={install}" in out
