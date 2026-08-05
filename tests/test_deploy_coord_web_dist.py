"""Regression guard for the #1543 deploy artifacts: `coord web` must serve
from `~/coord-web-dist` (not read `~/.coord-venv`'s bundled webapp) so that
shipping a webapp change never upgrades the venv `coord-agent`/`coord-serve`
also `ExecStart` from, and the build/publish timer must stay a locked,
oneshot, minute-cadence unit.

Mirrors tests/test_deploy_drive_queue_unit.py's approach: these are
systemd-file-content pins, not behavioural tests (systemd itself isn't
exercised here) -- they exist so an edit to deploy/coord-web*.service/.timer
that silently drops the #1543 fix gets caught in CI instead of on
dellserver.
"""

from __future__ import annotations

import configparser
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_UNIT = REPO_ROOT / "deploy" / "coord-web.service"
BUILD_SERVICE = REPO_ROOT / "deploy" / "coord-web-dist-build.service"
BUILD_TIMER = REPO_ROOT / "deploy" / "coord-web-dist-build.timer"
BUILD_SCRIPT = REPO_ROOT / "deploy" / "coord-web-dist-build.sh"


def _parse_unit(path: Path) -> configparser.RawConfigParser:
    # RawConfigParser: systemd's `%h`/`%%` specifiers collide with
    # configparser's default `%`-interpolation and raise otherwise.
    cp = configparser.RawConfigParser(strict=False)
    cp.read(path)
    return cp


def test_coord_web_service_serves_from_dist_symlink() -> None:
    """The actual #1543 fix: coord-web.service's ExecStart must pass
    --dist %h/coord-web-dist, so the live dashboard reads from a path outside
    ~/.coord-venv that the build timer keeps in sync with merged main."""
    unit = _parse_unit(WEB_UNIT)
    exec_start = unit.get("Service", "ExecStart")
    assert "--dist %h/coord-web-dist" in exec_start
    # Still the same venv-installed `coord` binary -- only the webapp bundle
    # source moves, not the daemon/CLI itself.
    assert exec_start.startswith("%h/.coord-venv/bin/coord web")


def test_coord_web_service_notes_are_current() -> None:
    """#1543 acceptance: the stale 2026-08-03-and-earlier note (which named
    the venv's bundled wheel as THE way a webapp change goes live) must be
    corrected, not just contradicted by an unrelated line elsewhere."""
    text = WEB_UNIT.read_text()
    assert "#1543" in text
    assert "coord-web-dist-build" in text


def test_dist_build_service_is_a_locked_oneshot() -> None:
    """Type=oneshot activated by the paired timer, mirroring
    coord-drive-queue.service's tick pattern -- a Type=simple long-runner
    here would never exit and the timer would never re-fire correctly."""
    unit = _parse_unit(BUILD_SERVICE)
    assert unit.get("Service", "Type") == "oneshot"
    # No [Install] section -- must be enabled via the .timer, not directly
    # (same convention as coord-drive-queue.service).
    assert not unit.has_section("Install")


def test_dist_build_timer_cadence_is_one_minute() -> None:
    """#1543 acceptance target: a merged webapp change is live within about
    a minute."""
    unit = _parse_unit(BUILD_TIMER)
    assert unit.get("Timer", "OnUnitActiveSec") == "1min"


def test_dist_build_script_is_executable_and_locked() -> None:
    """The script must guard against overlapping runs (a timer firing every
    minute WILL overlap a slow build) and must never touch ~/.coord-venv."""
    mode = BUILD_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "coord-web-dist-build.sh must be executable"
    text = BUILD_SCRIPT.read_text()
    assert "flock" in text
    # The header comment names ~/.coord-venv for context (why this script
    # exists), but no LINE OF CODE may invoke anything inside it -- that
    # would reintroduce the #1543 coupling to coord-agent/coord-serve.
    code_lines = [
        line for line in text.splitlines() if not line.strip().startswith("#")
    ]
    assert not any(".coord-venv" in line for line in code_lines), (
        "coord-web-dist-build.sh must not execute anything under "
        "~/.coord-venv -- only comments may name it"
    )
    assert not any("pip install" in line for line in code_lines)
