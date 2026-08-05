"""Regression guard for the #1543 deploy artifacts: `coord web` must serve
from `~/coord-web-dist` (not read `~/.coord-venv`'s bundled webapp) so that
shipping a webapp change never upgrades the venv `coord-agent`/`coord-serve`
also `ExecStart` from, and the build/publish timer must stay a locked,
oneshot, minute-cadence unit.

Also covers the #1560 last-known-good rollback: a pre-cutover health check
in coord-web-dist-build.sh (a scratch `coord web --fixture ... --dist
<release>` instance, probed before a release is ever symlinked live) and the
"no LINE OF CODE may touch ~/.coord-venv" invariant refined for it -- #1560
legitimately needs to READ-ONLY-resolve and execute the installed `coord`
binary to run that scratch instance (mirroring
scripts/azure-workers/epic-up.sh/epic-down.sh's fallback chain), which is a
different thing from the #1543 hazard this test originally guarded
(`pip install`/`--upgrade` mutating that venv, which is what silently
upgrades coord-agent/coord-serve too). See deploy/coord-web-dist-build.sh's
"Why fail-closed, not auto-revert" comment for the design rationale.

Mirrors tests/test_deploy_drive_queue_unit.py's approach: these are mostly
systemd-file-content / script-content pins, not behavioural tests (systemd
itself isn't exercised here) -- they exist so an edit to
deploy/coord-web*.service/.timer/.sh that silently drops the #1543 or #1560
fix gets caught in CI instead of on dellserver. tests/test_deploy_coord_web_rollback.py
covers deploy/coord-web-rollback.sh's symlink-swap logic behaviourally (it
is plain bash + filesystem, no systemd/network/npm involved, so it can run
for real under pytest).
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
ROLLBACK_SCRIPT = REPO_ROOT / "deploy" / "coord-web-rollback.sh"


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
    minute WILL overlap a slow build) and must never MUTATE ~/.coord-venv."""
    mode = BUILD_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "coord-web-dist-build.sh must be executable"
    text = BUILD_SCRIPT.read_text()
    assert "flock" in text
    code_lines = [
        line for line in text.splitlines() if not line.strip().startswith("#")
    ]
    # The #1543 hazard: nothing may install/upgrade the shared venv
    # coord-agent/coord-serve also ExecStart from -- that's what silently
    # killed headless workers before. #1560's read-only `coord` binary
    # resolution (resolve_coord_bin) is allowed to NAME ~/.coord-venv (it
    # only ever execs the already-installed binary for a scratch,
    #127.0.0.1-only health-check instance -- see the module docstring), so
    # the ban is scoped to actual install/upgrade verbs, not the bare path.
    assert not any("pip install" in line for line in code_lines)
    assert not any("agent update" in line for line in code_lines)
    assert not any("--upgrade" in line for line in code_lines)


def test_dist_build_script_health_checks_before_publish() -> None:
    """#1560's actual fix: a release must be booted and probed on a scratch,
    127.0.0.1-only port BEFORE the live symlink ever points at it -- the
    difference between "npm build exited 0" and "the page actually loads"
    the issue calls out. Uses --fixture (#1538): deterministic, no DB/fleet/
    network, so the probe can't race or corrupt the real coord.db that the
    ALREADY-RUNNING production coord-web/coord-serve/coord-agent are using."""
    text = BUILD_SCRIPT.read_text()
    assert "health_check_release" in text
    assert "--fixture" in text
    assert 'HEALTH_CHECK_HOST="127.0.0.1"' in text
    # Must never bind the scratch probe anywhere but loopback.
    assert "HEALTH_CHECK_HOST=\"0.0.0.0\"" not in text
    # Probes both surfaces the issue names: the SPA root and the API.
    assert 'id="root"' in text
    assert "/api/pipeline" in text
    # A release is only mv'd into $RELEASES_DIR and deleted-on-failure,
    # published-on-success -- never left half-published.
    assert 'rm -rf "$RELEASE_DIR"' in text
    assert "resolve_coord_bin" in text


def test_dist_build_script_rollback_comment_points_at_rollback_script() -> None:
    """The one-liner documented pre-#1560 still works, but the header must
    also point at the safer, phone-typeable script form (#1560)."""
    text = BUILD_SCRIPT.read_text()
    assert "coord-web-rollback.sh" in text


def test_rollback_script_exists_and_is_executable() -> None:
    """#1560 acceptance: "the revert command is documented and reachable
    without this issue in hand" -- a real installable script, not just a
    comment to copy-paste under stress."""
    mode = ROLLBACK_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "coord-web-rollback.sh must be executable"


def test_rollback_script_never_touches_other_deploy_lanes() -> None:
    """#1560 acceptance: coord-serve (7435) and coord-agent (7433) must be
    untouched by the revert, same as the forward deploy."""
    text = ROLLBACK_SCRIPT.read_text()
    assert "7435" not in text or "coord-serve (7435)" in text
    assert "7433" not in text or "coord-agent (7433)" in text
    code_lines = [
        line for line in text.splitlines() if not line.strip().startswith("#")
    ]
    assert not any("systemctl" in line for line in code_lines), (
        "coord-web-rollback.sh must not restart/touch any service -- the "
        "atomic symlink swap is the entire recovery action"
    )
    assert not any(".coord-venv" in line for line in code_lines)
    assert not any("pip install" in line for line in code_lines)


def test_rollback_script_publishes_atomically() -> None:
    """Same rename(2)-over-symlink pattern as the forward publish in
    coord-web-dist-build.sh -- no window where $LIVE_LINK is missing."""
    text = ROLLBACK_SCRIPT.read_text()
    assert 'ln -sfn "$TARGET" "$LIVE_LINK.new"' in text
    assert 'mv -Tf "$LIVE_LINK.new" "$LIVE_LINK"' in text
