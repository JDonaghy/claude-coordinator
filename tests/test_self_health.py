"""Unit tests for :mod:`coord.self_health` — #2436.

Drives real git checkouts + a real (local, file://-less) bare "origin" so the
stale/up-to-date/unknown detection is exercised against real git state, not
mocks. No network access: the "fetch" here is `git fetch` against a bare repo
on local disk, exactly like the CLI tests for --graph/--config-provenance.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coord.self_health import (
    SelfFreshness,
    default_install_path,
    format_status_lines,
    self_freshness,
    summary_line,
)

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
    """A checkout with an `origin` remote pointing at a local bare repo, both
    at commit "init"."""
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


# ── default_install_path ─────────────────────────────────────────────────────


def test_default_install_path_honors_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("COORD_SELF_CHECKOUT", str(tmp_path))
    assert default_install_path() == tmp_path


def test_default_install_path_falls_back_to_coord_dunder_file(monkeypatch) -> None:
    monkeypatch.delenv("COORD_SELF_CHECKOUT", raising=False)
    import coord as _coord

    assert default_install_path() == Path(_coord.__file__).resolve().parents[1]


# ── self_freshness ────────────────────────────────────────────────────────────


def test_reports_up_to_date_when_head_matches_origin(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)

    st = self_freshness(install_path=checkout, fetch=True)

    assert st.is_git_checkout is True
    assert st.unknown_reason is None
    assert st.stale is False
    assert st.healthy is True
    assert st.commits_behind == 0
    assert st.default_branch == "main"


def test_reports_stale_with_commit_count_when_origin_moved_ahead(tmp_path: Path) -> None:
    """The exact #2436/#2286 shape: origin/main has a commit this checkout
    was never pulled to get."""
    checkout = _make_checkout(tmp_path)
    caught_up_at = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()

    # Simulate someone else pushing a fix, and THIS checkout never pulling it:
    # commit + push from a clone, leaving `checkout` parked at the old commit.
    clone = tmp_path / "other-clone"
    _git("clone", "-q", str(tmp_path / "coord-remote.git"), str(clone), cwd=tmp_path)
    (clone / "marker.txt").write_text("v2 - the fix\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "the fix", cwd=clone)
    _git("push", "-q", "origin", "HEAD:main", cwd=clone)
    assert _git("rev-parse", "HEAD", cwd=checkout).stdout.strip() == caught_up_at

    st = self_freshness(install_path=checkout, fetch=True)

    assert st.is_git_checkout is True
    assert st.unknown_reason is None
    assert st.stale is True
    assert st.healthy is False
    assert st.commits_behind == 1

    lines = format_status_lines(st)
    joined = "\n".join(lines)
    assert "STALE" in joined
    assert "1 commit behind" in joined
    assert f"git -C {checkout} pull" in joined
    assert "SELF_FRESHNESS: git_checkout=true stale=true commits_behind=1" in summary_line(st)


def test_fetch_false_compares_against_the_last_known_ref_without_a_network_call(
    tmp_path: Path,
) -> None:
    """fetch=False must never itself run `git fetch` — it compares against
    whatever origin/main this checkout already has recorded locally."""
    checkout = _make_checkout(tmp_path)
    clone = tmp_path / "other-clone"
    _git("clone", "-q", str(tmp_path / "coord-remote.git"), str(clone), cwd=tmp_path)
    (clone / "marker.txt").write_text("v2\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "v2", cwd=clone)
    _git("push", "-q", "origin", "HEAD:main", cwd=clone)

    # No fetch was ever run in `checkout` since the push, so its
    # refs/remotes/origin/main still points at the original commit.
    st = self_freshness(install_path=checkout, fetch=False)

    assert st.fetch_ok is None
    assert st.commits_behind == 0  # stale content exists on origin, but unseen locally


def test_reports_unknown_reason_for_a_non_git_directory(tmp_path: Path) -> None:
    install = tmp_path / "site-packages" / "coord"
    install.mkdir(parents=True)

    st = self_freshness(install_path=install, fetch=False)

    assert st.is_git_checkout is False
    assert st.unknown_reason is not None
    assert st.stale is False
    assert st.healthy is False

    lines = format_status_lines(st)
    assert any("not a git checkout" in line for line in lines)
    assert summary_line(st) == f"SELF_FRESHNESS: git_checkout=false path={install}"


def test_reports_unknown_when_no_origin_remote_exists(tmp_path: Path) -> None:
    checkout = tmp_path / "solo"
    checkout.mkdir()
    _git("init", "-q", "-b", "main", ".", cwd=checkout)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=checkout)

    st = self_freshness(install_path=checkout, fetch=False)

    assert st.is_git_checkout is True
    assert st.unknown_reason is not None
    assert st.stale is False  # unknown must never be counted as drift
    assert "unknown=true" in summary_line(st)


def test_reports_unknown_not_up_to_date_when_rev_list_fails_after_refs_resolve(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression for the #2436 review finding: if HEAD and origin/<branch>
    both resolve but the `git rev-list --count` comparison itself fails
    (timeout, transient git error, shallow-clone edge case), that must be
    reported as unknown — never as a false-positive "up to date"."""
    checkout = _make_checkout(tmp_path)

    monkeypatch.setattr(
        "coord.self_health._commits_behind",
        lambda repo_path, head, origin: None,
    )

    st = self_freshness(install_path=checkout, fetch=True)

    assert st.is_git_checkout is True
    assert st.commits_behind is None
    assert st.unknown_reason is not None
    assert "rev-list" in st.unknown_reason
    assert st.stale is False  # unknown must never be counted as drift
    assert st.healthy is False

    lines = format_status_lines(st)
    joined = "\n".join(lines)
    assert "?" in joined
    assert "up to date" not in joined
    assert "SELF_FRESHNESS: git_checkout=true unknown=true" in summary_line(st)


def test_healthy_false_for_a_freshly_constructed_default_instance() -> None:
    """Sanity check on the dataclass defaults themselves — a never-populated
    SelfFreshness (e.g. a bug that returns early) must read as unhealthy,
    never as a false 'up to date'."""
    st = SelfFreshness(install_path=Path("/nowhere"))
    assert st.healthy is False
    assert st.stale is False
