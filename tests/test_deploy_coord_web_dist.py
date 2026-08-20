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

Also covers the rollback-sentinel guard added in response to #1560 review:
coord-web-dist-build.timer fires every ~1min, and fixing/reverting a bad
commit on `main` realistically takes longer than that -- so a bare rollback
alone is not durable, the timer would rebuild and silently republish the
exact SHA an operator just rolled back away from on its very next tick.
`test_build_script_refuses_to_republish_a_just_rolled_back_from_sha` and
`test_build_script_clears_sentinel_once_main_moves_past_the_blocked_sha`
below drive coord-web-dist-build.sh's REAL git plumbing (fetch/rev-parse/
worktree add) against a fully local, offline scratch git repo -- the guard
sits before any npm/network step, so these run for real under pytest with
no node/network dependency, same spirit as test_deploy_coord_web_rollback.py.
"""

from __future__ import annotations

import configparser
import os
import re
import stat
import subprocess
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


def test_dist_build_timer_cadence_is_ten_minutes() -> None:
    """#2122: the original 1-minute cadence fired 53 times/hour and logged
    an identical "nothing to build" line on every one of them, burying the
    host journal. Retuned to 10 minutes -- see the timer's own header for
    the measured merge-frequency evidence (webapp/-touching merges land at
    most every ~39min in this repo's last 30 days) that justifies staying
    well clear of that floor while cutting run volume ~9x."""
    unit = _parse_unit(BUILD_TIMER)
    assert unit.get("Timer", "OnUnitActiveSec") == "10min"


def test_dist_build_timer_timeout_stays_below_its_own_cadence() -> None:
    """TimeoutStartSec in the paired .service must stay a genuine ceiling
    BELOW the timer's own cadence -- a wedged build that runs into the next
    tick would defeat the flock's single-instance guard's whole purpose."""
    service_unit = _parse_unit(BUILD_SERVICE)
    timer_unit = _parse_unit(BUILD_TIMER)
    timeout_secs = int(service_unit.get("Service", "TimeoutStartSec"))
    cadence = timer_unit.get("Timer", "OnUnitActiveSec")
    assert cadence.endswith("min")
    cadence_secs = int(cadence[: -len("min")]) * 60
    assert timeout_secs < cadence_secs


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


def test_dist_build_script_defaults_to_the_coord_web_repo() -> None:
    """#2470: the build must track the dedicated `coord-web` repo's `main`,
    not `coord/dashboard/webapp/` inside THIS repo (docs/ADR_COORD_WEB_DIST.md)
    -- pin the actual default env-var values, not just prose in a comment."""
    text = BUILD_SCRIPT.read_text()
    assert 'REPO_NAME="${REPO_NAME:-coord-web}"' in text
    assert 'REPO_NAME="${REPO_NAME:-code-coordinator}"' not in text


def test_dist_build_script_builds_at_the_webapp_checkout_root() -> None:
    """#2470: `coord-web` IS the webapp package now -- `npm ci`/`npm run
    build` must run at $WEBAPP_CHECKOUT's own root (honoring the pre-existing
    $WEBAPP_SUBDIR seam, empty by default), not a hard-coded
    coord/dashboard/webapp subdirectory of it (that subdirectory only ever
    existed inside `claude-coordinator`, the pre-split monorepo)."""
    text = BUILD_SCRIPT.read_text()
    assert 'WEBAPP_DIR="$WEBAPP_CHECKOUT${WEBAPP_SUBDIR:+/$WEBAPP_SUBDIR}"' in text
    code_lines = [
        line for line in text.splitlines() if not line.strip().startswith("#")
    ]
    assert not any("coord/dashboard/webapp" in line for line in code_lines)


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


def test_health_check_fixture_default_resolves_under_webapp_checkout(
    tmp_path: Path,
) -> None:
    """#2470 review fix: retargeting $WEBAPP_CHECKOUT at a `coord-web`
    checkout silently broke the health-check fixture default -- it used to
    read `$WEBAPP_CHECKOUT/tests/fixtures/board-pipeline-basic.json`, which
    was correct when $WEBAPP_CHECKOUT was a worktree of THIS repo, but
    `coord-web`'s `origin/main` has no `tests/` directory at all
    (confirmed via `git ls-tree -r --name-only origin/main`); the fixture
    actually lives at `e2e/fixtures/board-pipeline-basic.json`. A missing
    fixture makes `health_check_release` fail closed (see
    test_dist_build_script_health_checks_before_publish), which means
    coord-web-dist-build.timer would NEVER successfully publish on a real
    host -- exactly the bug #2470 exists to fix. This drives the actual
    parameter-expansion line from the script (extracted verbatim, not
    retyped) against a scratch directory shaped like a real `coord-web`
    checkout, so a future edit that reintroduces the stale
    `tests/fixtures/...` default fails this test instead of silently
    breaking every publish."""
    text = BUILD_SCRIPT.read_text()
    fixture_lines = [
        line
        for line in text.splitlines()
        if line.strip().startswith('fixture="${HEALTH_CHECK_FIXTURE:-')
    ]
    assert len(fixture_lines) == 1, fixture_lines
    fixture_expr = fixture_lines[0].strip()

    # Guard against the stale, pre-#2470-retarget-fix default reappearing.
    assert "tests/fixtures/board-pipeline-basic.json" not in text

    webapp_checkout = tmp_path / "webapp-checkout"
    fixture_dir = webapp_checkout / "e2e" / "fixtures"
    fixture_dir.mkdir(parents=True)
    fixture_file = fixture_dir / "board-pipeline-basic.json"
    fixture_file.write_text("{}")

    env = dict(os.environ)
    env.pop("HEALTH_CHECK_FIXTURE", None)
    env["WEBAPP_CHECKOUT"] = str(webapp_checkout)

    result = subprocess.run(
        ["bash", "-c", f'{fixture_expr}\necho "$fixture"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    resolved = result.stdout.strip()
    assert resolved == str(fixture_file)
    assert Path(resolved).is_file(), (
        "resolved default fixture path must actually exist on disk in a "
        "real coord-web checkout layout"
    )


def test_dist_build_script_rollback_comment_points_at_rollback_script() -> None:
    """The one-liner documented pre-#1560 still works, but the header must
    also point at the safer, phone-typeable script form (#1560)."""
    text = BUILD_SCRIPT.read_text()
    assert "coord-web-rollback.sh" in text


def test_dist_build_script_has_rollback_sentinel_guard() -> None:
    """#1560 review fix: the up-to-date check alone is NOT durable against
    an operator's manual rollback. After a rollback, the live SHA is the
    GOOD one, so origin/main's tip (still the BAD commit -- fixing/
    reverting main realistically takes longer than this timer's 1min
    cadence) no longer matches it, and the up-to-date short-circuit does
    not fire. Without a guard, the next tick would rebuild and silently
    republish the exact SHA the operator just rolled back away from. Pin
    that the guard exists, reads the same sentinel path
    coord-web-rollback.sh writes, and tells the operator how to pause the
    timer -- see test_build_script_refuses_to_republish_a_just_rolled_back_from_sha
    below for the behavioural version of this same guard."""
    text = BUILD_SCRIPT.read_text()
    assert "BLOCKED_SHA_FILE" in text
    assert ".rollback-blocked-sha" in text
    assert "REFUSING to build/publish" in text
    assert "coord-web-dist-build.timer" in text
    assert "systemctl --user stop coord-web-dist-build.timer" in text


def test_rollback_script_exists_and_is_executable() -> None:
    """#1560 acceptance: "the revert command is documented and reachable
    without this issue in hand" -- a real installable script, not just a
    comment to copy-paste under stress."""
    mode = ROLLBACK_SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "coord-web-rollback.sh must be executable"


def test_rollback_and_build_scripts_share_the_same_sentinel_default() -> None:
    """The handoff between the two scripts (#1560) only works if they agree
    on where the sentinel lives without either side needing an explicit env
    var set -- pin both defaults resolve to the identical path expression."""
    sentinel_default = (
        'BLOCKED_SHA_FILE="${BLOCKED_SHA_FILE:-$RELEASES_DIR/.rollback-blocked-sha}"'
    )
    assert sentinel_default in BUILD_SCRIPT.read_text()
    assert sentinel_default in ROLLBACK_SCRIPT.read_text()


def test_rollback_script_writes_the_sentinel_and_warns_about_the_timer() -> None:
    """#1560 review fix: a bare symlink swap is not enough -- the operator
    must be warned, in the script's own output, that the build timer will
    try to republish the bad SHA again within about a minute unless main is
    fixed or the timer is paused."""
    text = ROLLBACK_SCRIPT.read_text()
    assert 'printf \'%s\\n\' "$BLOCKED_SHA" > "$BLOCKED_SHA_FILE"' in text
    assert "coord-web-dist-build.timer fires again" in text
    assert "systemctl --user stop coord-web-dist-build.timer" in text


def test_rollback_script_never_touches_other_deploy_lanes() -> None:
    """#1560 acceptance: coord-serve (7435) and coord-agent (7433) must be
    untouched by the revert, same as the forward deploy. Every CODE line
    (not comment) that mentions either port must be one of the
    informational `say "..."` lines confirming they were untouched --
    nothing that actually curls, restarts, kills, or otherwise reaches for
    a process bound to 7435/7433. A future edit that added, say, a
    `curl http://localhost:7435/...` health probe would trip this even
    though "coord-serve (7435)" still appears elsewhere in the file
    (the failure mode a purely-substring assertion would miss)."""
    text = ROLLBACK_SCRIPT.read_text()
    code_lines = [
        line for line in text.splitlines() if not line.strip().startswith("#")
    ]
    port_lines = [line for line in code_lines if "7435" in line or "7433" in line]
    assert port_lines, "expected the script to report on 7435/7433 at all"
    for line in port_lines:
        assert line.strip().startswith('say "'), (
            "code line reaches for port 7435/7433 outside the informational "
            f"'not touched' message: {line!r}"
        )
    assert not any(
        re.match(r"^\s*systemctl\b", line) for line in code_lines
    ), (
        "coord-web-rollback.sh must not restart/touch any service -- the "
        "atomic symlink swap is the entire recovery action"
    )
    assert not any(".coord-venv" in line for line in code_lines)
    assert not any("pip install" in line for line in code_lines)


def _init_git_repo_with_commit(path: Path, *, message: str) -> str:
    """Create a minimal git repo with a single commit on `main` and return
    its SHA. Used to drive coord-web-dist-build.sh's real git plumbing
    (fetch/rev-parse/worktree add) against a fully local, offline "origin"
    -- no network, no GitHub, no npm required to exercise the
    rollback-sentinel guard, which sits before any of that."""
    path.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=path, check=True, capture_output=True, text=True
        )

    run("init", "-q")
    # Force the branch name regardless of the ambient git's
    # init.defaultBranch config -- coord-web-dist-build.sh's default
    # $BRANCH is "main".
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (path / "README.md").write_text(message)
    run("add", ".")
    run("commit", "-q", "-m", message)
    return run("rev-parse", "HEAD").stdout.strip()


def _clone_as_base_checkout(origin_repo: Path, dest: Path) -> None:
    subprocess.run(
        ["git", "clone", "-q", str(origin_repo), str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_build_script_refuses_to_republish_a_just_rolled_back_from_sha(
    tmp_path: Path,
) -> None:
    """The exact bug the #1560 review flagged: after coord-web-rollback.sh
    repoints $LIVE_LINK at the GOOD release and writes the sentinel naming
    the BAD sha, origin/main's tip is still that same bad sha (main hasn't
    moved -- fixing it realistically takes longer than a minute), so the
    up-to-date check does not fire. This drives the real script's git
    plumbing against a fully local, offline origin repo; the guard sits
    before the worktree bootstrap/npm build, so this exits fast with no
    network or node dependency."""
    origin_repo = tmp_path / "origin-repo"
    bad_sha = _init_git_repo_with_commit(origin_repo, message="bad commit")

    base_checkout = tmp_path / "base-checkout"
    _clone_as_base_checkout(origin_repo, base_checkout)

    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    good_release = releases_dir / "sha-good"
    good_release.mkdir()
    (good_release / "index.html").write_text('<div id="root">good</div>')
    live_link = tmp_path / "live"
    live_link.symlink_to(good_release)

    blocked_sha_file = releases_dir / ".rollback-blocked-sha"
    blocked_sha_file.write_text(bad_sha + "\n")

    webapp_checkout = tmp_path / "webapp-checkout"  # must never get created

    env = dict(os.environ)
    env.update(
        {
            "BASE_CHECKOUT": str(base_checkout),
            "BRANCH": "main",
            "WEBAPP_CHECKOUT": str(webapp_checkout),
            "RELEASES_DIR": str(releases_dir),
            "LIVE_LINK": str(live_link),
            "LOCK_FILE": str(tmp_path / "build.lock"),
        }
    )

    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0, result.stderr
    assert "REFUSING to build/publish" in result.stderr
    assert bad_sha in result.stderr
    assert not webapp_checkout.exists(), (
        "must refuse before ever touching the dedicated build worktree"
    )
    # The rollback itself must not have been undone.
    assert live_link.resolve() == good_release.resolve()
    # Sentinel is left in place -- main still hasn't moved past the bad SHA.
    assert blocked_sha_file.read_text().strip() == bad_sha


def test_build_script_clears_sentinel_once_main_moves_past_the_blocked_sha(
    tmp_path: Path,
) -> None:
    """Once a fix actually lands on main, the sentinel must not permanently
    wedge deploys -- the guard should clear it and let the normal build
    proceed (which then fails for an unrelated, expected reason in this
    test: the scratch origin repo is not a real `coord-web` checkout with
    anything npm-buildable at its root -- this test only cares that the
    guard released its grip)."""
    origin_repo = tmp_path / "origin-repo"
    bad_sha = _init_git_repo_with_commit(origin_repo, message="bad commit")
    (origin_repo / "README.md").write_text("fixed")
    subprocess.run(
        ["git", "add", "."], cwd=origin_repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "fix the bad commit"],
        cwd=origin_repo,
        check=True,
        capture_output=True,
        text=True,
    )

    base_checkout = tmp_path / "base-checkout"
    _clone_as_base_checkout(origin_repo, base_checkout)

    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    good_release = releases_dir / "sha-good"
    good_release.mkdir()
    (good_release / "index.html").write_text('<div id="root">good</div>')
    live_link = tmp_path / "live"
    live_link.symlink_to(good_release)

    blocked_sha_file = releases_dir / ".rollback-blocked-sha"
    blocked_sha_file.write_text(bad_sha + "\n")

    webapp_checkout = tmp_path / "webapp-checkout"

    env = dict(os.environ)
    env.update(
        {
            "BASE_CHECKOUT": str(base_checkout),
            "BRANCH": "main",
            "WEBAPP_CHECKOUT": str(webapp_checkout),
            "RELEASES_DIR": str(releases_dir),
            "LIVE_LINK": str(live_link),
            "LOCK_FILE": str(tmp_path / "build.lock"),
        }
    )

    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert "clearing sentinel" in result.stderr, result.stderr
    assert not blocked_sha_file.exists()


def test_up_to_date_run_is_silent_and_writes_a_heartbeat(tmp_path: Path) -> None:
    """#2122 acceptance #1: with main unchanged, the script must not emit a
    log line -- assert on the actual journal-equivalent (subprocess stderr,
    what systemd would forward to the journal), not on some internal flag.
    This is the exact fix for the noise the issue is about: at the old
    1-minute cadence this path fired 53 times/hour, each one logging an
    identical "nothing to build" line. Acceptance #4 requires a surface
    that still distinguishes "up to date" from "has not run since <time>"
    once that logging is gone -- the heartbeat file is that surface, and
    this pins that it is written on exactly this path."""
    origin_repo = tmp_path / "origin-repo"
    sha = _init_git_repo_with_commit(origin_repo, message="only commit")

    base_checkout = tmp_path / "base-checkout"
    _clone_as_base_checkout(origin_repo, base_checkout)

    releases_dir = tmp_path / "releases"
    releases_dir.mkdir()
    live_release = releases_dir / sha
    live_release.mkdir()
    (live_release / "index.html").write_text('<div id="root">x</div>')
    live_link = tmp_path / "live"
    live_link.symlink_to(live_release)

    webapp_checkout = tmp_path / "webapp-checkout"  # must never get created
    heartbeat_file = releases_dir / ".last-run-at"

    env = dict(os.environ)
    env.update(
        {
            "BASE_CHECKOUT": str(base_checkout),
            "BRANCH": "main",
            "WEBAPP_CHECKOUT": str(webapp_checkout),
            "RELEASES_DIR": str(releases_dir),
            "LIVE_LINK": str(live_link),
            "LOCK_FILE": str(tmp_path / "build.lock"),
        }
    )

    result = subprocess.run(
        ["bash", str(BUILD_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == "", (
        "up-to-date tick must not log anything -- got: " + result.stderr
    )
    assert not webapp_checkout.exists()

    assert heartbeat_file.exists(), "heartbeat must be written even on a no-op tick"
    fields = heartbeat_file.read_text().split()
    assert len(fields) == 3, heartbeat_file.read_text()
    epoch_str, status, heartbeat_sha = fields
    assert float(epoch_str) > 0
    assert status == "up-to-date"
    assert heartbeat_sha == sha


def test_heartbeat_default_path_matches_the_health_check_default() -> None:
    """The build script's $HEARTBEAT_FILE default and
    coord.health.checks.deploy_lane_facts's default must resolve to the
    identical path without either side needing an explicit env var / config
    override -- same "share the default, don't just share the docs"
    convention as test_rollback_and_build_scripts_share_the_same_sentinel_default
    above."""
    from coord.health.checks.deploy_lane_facts import _DEFAULT_WEBAPP_BUILD_HEARTBEAT

    heartbeat_default = (
        'HEARTBEAT_FILE="${HEARTBEAT_FILE:-$RELEASES_DIR/.last-run-at}"'
    )
    assert heartbeat_default in BUILD_SCRIPT.read_text()
    # $RELEASES_DIR itself defaults to ~/.coord-web-releases (see
    # RELEASES_DIR="${RELEASES_DIR:-$HOME/.coord-web-releases}" above) --
    # the health-check default must expand to that same joined path.
    assert _DEFAULT_WEBAPP_BUILD_HEARTBEAT == "~/.coord-web-releases/.last-run-at"


def test_rollback_script_publishes_atomically() -> None:
    """Same rename(2)-over-symlink pattern as the forward publish in
    coord-web-dist-build.sh -- no window where $LIVE_LINK is missing."""
    text = ROLLBACK_SCRIPT.read_text()
    assert 'ln -sfn "$TARGET" "$LIVE_LINK.new"' in text
    assert 'mv -Tf "$LIVE_LINK.new" "$LIVE_LINK"' in text
