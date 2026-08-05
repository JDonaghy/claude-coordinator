"""Behavioural tests for deploy/coord-web-rollback.sh (#1560).

Unlike the systemd-unit / build-script content pins in
tests/test_deploy_coord_web_dist.py, this script is plain bash + filesystem
symlink math with no systemd, network, or npm involved -- so it can be
exercised for real under pytest via subprocess, against scratch
$RELEASES_DIR / $LIVE_LINK directories, exactly like a human would run it
over ssh against ~/.coord-web-releases / ~/coord-web-dist. This is the
"one-command revert" #1560's acceptance criteria calls out; these tests are
the regression guard for it actually working, independent of the manual
drill transcript recorded in docs/PHONE_WEBAPP.md.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLLBACK_SCRIPT = REPO_ROOT / "deploy" / "coord-web-rollback.sh"


def _run(releases_dir: Path, live_link: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["RELEASES_DIR"] = str(releases_dir)
    env["LIVE_LINK"] = str(live_link)
    return subprocess.run(
        ["bash", str(ROLLBACK_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _make_release(releases_dir: Path, sha: str, *, age_seconds: float, root: bool = True) -> Path:
    release = releases_dir / sha
    release.mkdir(parents=True)
    if root:
        (release / "index.html").write_text(f"<div id='root'>{sha}</div>")
    # Backdate mtime so `ls -dt` orders releases deterministically -- two
    # releases created in the same test can otherwise land in the same
    # filesystem-timestamp bucket.
    stamp = time.time() - age_seconds
    os.utime(release, (stamp, stamp))
    return release


def test_rolls_back_to_second_newest_release(tmp_path: Path) -> None:
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    old = _make_release(releases_dir, "sha-old", age_seconds=7200)
    new = _make_release(releases_dir, "sha-new", age_seconds=60)
    live_link = tmp_path / "live"
    live_link.symlink_to(new)

    result = _run(releases_dir, live_link)

    assert result.returncode == 0, result.stderr
    assert live_link.resolve() == old.resolve()


def test_refuses_when_fewer_than_two_releases(tmp_path: Path) -> None:
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    only = _make_release(releases_dir, "sha-only", age_seconds=60)
    live_link = tmp_path / "live"
    live_link.symlink_to(only)

    result = _run(releases_dir, live_link)

    assert result.returncode != 0
    assert "fewer than 2 releases" in result.stderr
    # Must not have touched the live link.
    assert live_link.resolve() == only.resolve()


def test_refuses_target_missing_index_html(tmp_path: Path) -> None:
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    bad = _make_release(releases_dir, "sha-bad", age_seconds=7200, root=False)
    good = _make_release(releases_dir, "sha-good", age_seconds=60)
    live_link = tmp_path / "live"
    live_link.symlink_to(good)

    result = _run(releases_dir, live_link)

    assert result.returncode != 0
    assert "no index.html" in result.stderr
    assert live_link.resolve() == good.resolve()


def test_missing_live_link_publishes_newest_release(tmp_path: Path) -> None:
    """$LIVE_LINK absent (e.g. the very first build was interrupted before
    ever publishing) means there is no "current" release to roll back FROM
    -- with nothing to distrust, the sensible recovery is the newest release
    actually on disk, not an arbitrary older one."""
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    _make_release(releases_dir, "sha-old", age_seconds=7200)
    new = _make_release(releases_dir, "sha-new", age_seconds=60)
    live_link = tmp_path / "live"  # deliberately never created

    result = _run(releases_dir, live_link)

    assert result.returncode == 0, result.stderr
    assert live_link.resolve() == new.resolve()


def test_is_fast(tmp_path: Path) -> None:
    """#1560 acceptance: time the recovery. A pure symlink swap should be
    well under a second, let alone the issue's one-minute bar."""
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    _make_release(releases_dir, "sha-old", age_seconds=7200)
    new = _make_release(releases_dir, "sha-new", age_seconds=60)
    live_link = tmp_path / "live"
    live_link.symlink_to(new)

    started = time.monotonic()
    result = _run(releases_dir, live_link)
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < 5.0, f"rollback took {elapsed:.2f}s"


def test_reports_no_restart_and_other_lanes_untouched(tmp_path: Path) -> None:
    """#1560 acceptance: coord-serve (7435) and coord-agent (7433) must be
    untouched by the revert -- assert the script itself says so, not just
    that its code happens not to mention them (test_deploy_coord_web_dist.py
    already pins that)."""
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    _make_release(releases_dir, "sha-old", age_seconds=7200)
    new = _make_release(releases_dir, "sha-new", age_seconds=60)
    live_link = tmp_path / "live"
    live_link.symlink_to(new)

    result = _run(releases_dir, live_link)

    assert result.returncode == 0, result.stderr
    assert "no coord-web restart needed" in result.stderr
    assert "7435" in result.stderr and "7433" in result.stderr


def test_writes_sentinel_naming_the_sha_rolled_back_from(tmp_path: Path) -> None:
    """#1560 review fix: coord-web-dist-build.timer fires again within
    about a minute of a rollback, and (because fixing a bad commit on main
    realistically takes longer than that) would otherwise rebuild and
    silently republish the exact SHA an operator just rolled back away
    from. This script must leave a sentinel naming that bad SHA so the
    build script can refuse to republish it -- see
    test_deploy_coord_web_dist.py's
    test_build_script_refuses_to_republish_a_just_rolled_back_from_sha for
    the other half of this contract."""
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    _make_release(releases_dir, "sha-old", age_seconds=7200)
    bad = _make_release(releases_dir, "sha-bad", age_seconds=60)
    live_link = tmp_path / "live"
    live_link.symlink_to(bad)

    result = _run(releases_dir, live_link)

    assert result.returncode == 0, result.stderr
    sentinel = releases_dir / ".rollback-blocked-sha"
    assert sentinel.read_text().strip() == "sha-bad"


def test_warns_that_the_build_timer_will_try_to_republish(tmp_path: Path) -> None:
    """The minimal half of the #1560 review fix: even independent of the
    sentinel, the operator must be told in the script's own output that the
    build timer is about to fire again and how to pause it if they need
    more than a minute to fix main."""
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    _make_release(releases_dir, "sha-old", age_seconds=7200)
    bad = _make_release(releases_dir, "sha-bad", age_seconds=60)
    live_link = tmp_path / "live"
    live_link.symlink_to(bad)

    result = _run(releases_dir, live_link)

    assert result.returncode == 0, result.stderr
    assert "coord-web-dist-build.timer" in result.stderr
    assert "systemctl --user stop coord-web-dist-build.timer" in result.stderr


def test_no_sentinel_written_when_there_was_no_prior_live_release(tmp_path: Path) -> None:
    """When $LIVE_LINK was absent (nothing to distrust -- see
    test_missing_live_link_publishes_newest_release above), there is no
    "bad SHA" to block, so no sentinel should be written and no timer
    warning should fire."""
    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    _make_release(releases_dir, "sha-old", age_seconds=7200)
    new = _make_release(releases_dir, "sha-new", age_seconds=60)
    live_link = tmp_path / "live"  # deliberately never created

    result = _run(releases_dir, live_link)

    assert result.returncode == 0, result.stderr
    sentinel = releases_dir / ".rollback-blocked-sha"
    assert not sentinel.exists()
    assert "coord-web-dist-build.timer" not in result.stderr
