#!/usr/bin/env bash
#
# coord-web-dist-build.sh — build the React webapp from the `coord-web`
# repo's merged `main` into a directory `coord web --dist` serves, WITHOUT
# touching ~/.coord-venv (#1543; retargeted at `coord-web` by #2470 once the
# webapp moved out of THIS repo into its own, epic #2002 — see
# docs/ADR_COORD_WEB_DIST.md for the decision. Before #2470 this built
# `coord/dashboard/webapp` out of a `claude-coordinator` checkout; the
# mechanism below is otherwise unchanged, only which repo it points at).
#
# Why this exists: coord-web, coord-agent and coord-serve all `ExecStart`
# from the SAME ~/.coord-venv, so "ship a webapp change" used to mean
# `pip install --upgrade code-coordinator` on that venv — which also
# upgrades the board daemon and the agent runtime on that host, and
# `coord agent update` is already known to kill running headless workers
# (see the #1543 issue body). This script decouples the two: it keeps a
# DEDICATED clone of the `coord-web` repo (never an operator's own
# `~/src/coord-web` dev checkout's working tree — same "never the checkout
# someone else is using" rule this script has always applied to the repo it
# builds from), builds it from merged main into a fresh, timestamped release
# directory, and atomically repoints a symlink at it. `coord web --dist
# ~/coord-web-dist` (see deploy/coord-web.service) always points at that
# symlink.
#
# #2009 (epic #2002) — WHICH repo this builds changed, nothing else did.
# Until the split, `$BASE_CHECKOUT` was a checkout of *claude-coordinator*
# and the build ran in its `coord/dashboard/webapp/` subdirectory. The
# webapp now lives in its own `coord-web` repo, so `$REPO_NAME` defaults to
# `coord-web` and the build runs at that repo's ROOT (`$WEBAPP_SUBDIR`,
# empty by default, is the seam for a repo that nests it again). Per
# docs/ADR_COORD_WEB_DIST.md (#2004) that redirection is the WHOLE of the
# change: the fetch-then-build-in-a-dedicated-worktree shape, the
# health-check-on-a-scratch-port before cutover (#1560), the atomic symlink
# publish, the rollback script and its anti-flap sentinel, and the heartbeat
# file (#2122) all carry over untouched, because none of them ever depended
# on which repo the source came from.
#
# The health check boots a real `coord web --fixture` and needs a board
# fixture JSON for it — `coord-web` vendored its own copy of the one
# `e2e/fixtureServer.ts` already uses (post-#2005 split, commit 7e0bf5b), so
# this reads `$WEBAPP_CHECKOUT/e2e/fixtures/board-pipeline-basic.json` and
# has no dependency on a claude-coordinator checkout being present. Set
# `$HEALTH_CHECK_FIXTURE` directly to override.
#
# NO RESTART of coord-web is required after the first build. Starlette's
# StaticFiles/FileResponse re-resolve the directory on every request (see
# coord/dashboard/server.py's `webapp_dist` seam), and the symlink swap below
# is a single atomic rename(2) — so a merge lands on the live dashboard the
# next time this script runs, with no process restart, no dropped
# /ws/terminal PTY connections, and no dependency on release cadence. The
# ONE time a restart (or a fresh `coord web` start) is required is the very
# first install, before any release directory exists yet — run this script
# once, THEN start/enable coord-web.
#
# Safe to run unattended on a timer (see coord-web-dist-build.timer): it
# never touches ~/.coord-venv or $BASE_CHECKOUT's working tree (only
# `git fetch`/`rev-parse` run there — the actual build happens in a
# SEPARATE, DEDICATED `git worktree`, $WEBAPP_CHECKOUT, so it can't collide
# with whatever branch an operator has $BASE_CHECKOUT parked on — same
# reasoning as `coord drive`'s worktree isolation), or any running
# coord-agent/coord-serve/coord-web process. A `flock` guards against
# overlapping runs; a same-SHA re-run is a fast no-op.
#
# Install: $BASE_CHECKOUT (default ~/src/coord-web) must be a real, already-
# cloned `coord-web` checkout before the first run — this script only
# fetches/rev-parses/worktrees off it, it never `git clone`s one into
# existence. Use a checkout dedicated to this deploy lane if the operator
# also keeps their own `coord-web` dev checkout elsewhere on this box.
#
#   git clone <coord-web-remote> ~/src/coord-web   # one-time, if not already there
#   cp deploy/coord-web-dist-build.sh ~/.local/bin/   # or run in place
#   chmod +x ~/.local/bin/coord-web-dist-build.sh
#   ~/.local/bin/coord-web-dist-build.sh               # first build, before
#                                                       # coord-web is started
#   mkdir -p ~/.config/systemd/user
#   cp deploy/coord-web-dist-build.service deploy/coord-web-dist-build.timer \
#       ~/.config/systemd/user/
#   systemctl --user daemon-reload
#   systemctl --user enable --now coord-web-dist-build.timer
#
# Health-check before cutover (#1560): before a build is ever published, it
# is booted as a SCRATCH `coord web` instance on 127.0.0.1:$HEALTH_CHECK_PORT
# — same code path production uses, `--fixture` sourced so it needs no DB, no
# fleet, no network and touches no live state (see coord/dashboard/fixture.py)
# — and probed for `/` (200, real SPA markup, not the legacy fallback page)
# and `/api/pipeline` (200, parses as a non-empty JSON array). Only a release
# that passes both gets `mv`'d into $RELEASES_DIR and symlinked live; a
# failing release is deleted, never published, and the CURRENT live release
# is untouched — see "Why fail-closed, not auto-revert" below.
#
# Rollback (one command — pairs with #1560): repoint the live symlink at the
# previous release directory, still on disk under $RELEASES_DIR:
#
#   ~/.local/bin/coord-web-rollback.sh
#
# or, equivalently, by hand:
#
#   ln -sfn "$(ls -dt ~/.coord-web-releases/*/ | sed -n 2p)" ~/coord-web-dist
#
# (lists release dirs newest-first, picks the SECOND newest — i.e. the one
# before the current live release — and repoints the symlink at it; no
# restart needed, same as a forward build.) Because a release only ever
# lands in $RELEASES_DIR after passing the health check above, "second
# newest directory on disk" and "last known GOOD release" are the same
# thing — a failed build is deleted, not left there to be rolled back into.
# See deploy/coord-web-rollback.sh and docs/PHONE_WEBAPP.md for the
# script form (safer to type over ssh from a phone under stress) and the
# full recovery runbook + timed drill transcript.
#
# NOT durable on its own against THIS timer: fixing/reverting a bad commit
# on origin/main realistically takes longer than this timer's 10min cadence
# (#2122), so a rollback alone would get silently republished (and undone)
# on the very next tick. That's what
# the "Rollback-sentinel guard" below is for — coord-web-rollback.sh writes
# $RELEASES_DIR/.rollback-blocked-sha naming the bad SHA, and this script
# refuses to republish exactly that SHA until main moves past it or the
# sentinel is cleared by hand. See that block, and coord-web-rollback.sh's
# own header, for the full mechanism.
#
# Why fail-closed, not auto-revert-after-publish: the health check above
# runs on a scratch port BEFORE the symlink swap, so there is no window
# where a broken bundle is ever live — "auto-revert" would mean detecting
# breakage AFTER cutover and repointing back, which is strictly weaker (some
# nonzero time live broken) for a check this script can already make
# BEFORE cutover. A bug the fixture-based check cannot catch (e.g. a
# JS-only runtime error that still serves 200 HTML) would need the manual
# `coord-web-rollback.sh` above; that path is exercised in the #1560
# acceptance transcript in docs/PHONE_WEBAPP.md.
#
# See docs/PHONE_WEBAPP.md and docs/AGENT_OPERATIONS.md for the full runbook.

set -uo pipefail

# #2470: tracks the `coord-web` repo's main, not this repo's — see the
# header. $BASE_CHECKOUT is read-only from this script's point of view (only
# `git fetch`/`rev-parse`/`worktree add` ever touch it, never `checkout`), so
# an operator's own `coord-web` dev checkout at the default path is safe in
# principle, but a checkout dedicated to this deploy lane is still the
# documented install step — see the header's "Install" section.
SRC_ROOT="${SRC_ROOT:-$HOME/src}"
# #2009: the webapp's repo, not claude-coordinator's. See the header.
REPO_NAME="${REPO_NAME:-coord-web}"
BASE_CHECKOUT="${BASE_CHECKOUT:-$SRC_ROOT/$REPO_NAME}"
BRANCH="${BRANCH:-main}"
# Path from the webapp repo's root to the directory holding package.json.
# Empty for `coord-web` (its root IS the webapp root); this used to be the
# hard-coded `coord/dashboard/webapp`, and stays configurable so a future
# monorepo layout needs an env var rather than an edit here.
WEBAPP_SUBDIR="${WEBAPP_SUBDIR:-}"

# A worktree DEDICATED to this script — never the same directory an operator
# or `coord drive` worktree might be using, and never checked out over a
# branch someone else parked there.
WEBAPP_CHECKOUT="${WEBAPP_CHECKOUT:-$HOME/.coord-web-checkout}"
RELEASES_DIR="${RELEASES_DIR:-$HOME/.coord-web-releases}"
LIVE_LINK="${LIVE_LINK:-$HOME/coord-web-dist}"
KEEP_RELEASES="${KEEP_RELEASES:-3}"
LOCK_FILE="${LOCK_FILE:-$HOME/.coord-web-dist-build.lock}"
# Sentinel written by coord-web-rollback.sh (#1560) naming the SHA an
# operator just rolled back FROM. See the "Rollback-sentinel guard" block
# below: without this, this timer re-publishes that exact SHA on its very
# next tick (up to 10min later, #2122) whenever origin/$BRANCH hasn't moved
# past it yet — silently undoing a manual rollback for precisely the bug class
# (client-side-only JS runtime errors) the manual path exists to catch. See
# coord-web-rollback.sh's header and docs/PHONE_WEBAPP.md's "Rollback" and
# "Recovery" sections for the full story.
BLOCKED_SHA_FILE="${BLOCKED_SHA_FILE:-$RELEASES_DIR/.rollback-blocked-sha}"

# Heartbeat (#2122): written on EVERY invocation, whether or not there was
# anything to build — proof the timer is still firing, independent of the
# journal. The up-to-date path below (by far the common case, now that the
# timer polls instead of logging on a fixed short cadence) deliberately
# stays SILENT in the journal, so this file is the only place that answers
# "did this last actually run?" without grepping systemd's own fire history.
# `coord.health.checks.deploy_lane_facts.probe_webapp_build_heartbeat` reads
# it to distinguish "up to date" from "has not run since <time>" (a dead
# trigger — timer disabled, wedged, or erroring before it gets here).
HEARTBEAT_FILE="${HEARTBEAT_FILE:-$RELEASES_DIR/.last-run-at}"

# ── Health-check-before-cutover config (#1560) ──────────────────────────────
# 127.0.0.1-only, never $HEALTH_CHECK_HOST=0.0.0.0 — this is a throwaway
# probe instance, not a second public dashboard.
HEALTH_CHECK_HOST="127.0.0.1"
HEALTH_CHECK_PORT="${HEALTH_CHECK_PORT:-18434}"
HEALTH_CHECK_TIMEOUT_SECS="${HEALTH_CHECK_TIMEOUT_SECS:-20}"
# The reference seeded board (#1538) — deterministic, no DB/fleet/network, so
# the scratch instance below never touches ~/.coord/coord.db or races the
# real coord-web/coord-serve/coord-agent processes. Defaults to the copy
# checked into $WEBAPP_CHECKOUT (the `coord-web` SHA being deployed) at
# `e2e/fixtures/board-pipeline-basic.json` — that is where the fixture
# actually lives in the `coord-web` repo (`git ls-tree -r --name-only
# origin/main` has no `tests/` directory at all post-split). NOTE:
# `coord-web`'s own `playwright.acceptance.config.ts` still has a stale
# `DEFAULT_FIXTURE` pointing at the old `tests/fixtures/...` path (a
# leftover `REPO_ROOT` miscalculation from before the repo split — see
# docs/ADR_COORD_WEB_DIST.md) so do NOT use that config as the source of
# truth for this path; this comment documents the real, verified location
# instead (#2470). This is never a claim that the fixture matches the
# INSTALLED `coord`'s server code, which post-#2002-split is versioned
# independently (see docs/ADR_COORD_WEB_DIST.md, "Staleness across the
# split").
HEALTH_CHECK_FIXTURE="${HEALTH_CHECK_FIXTURE:-}"

say() { echo "[$(date -Is)] $*" >&2; }

# Writes $HEARTBEAT_FILE unconditionally — called from every exit path below,
# success or failure alike, and NEVER itself logged (that would defeat the
# point: the up-to-date case must produce zero journal lines while still
# leaving a durable, queryable "I was here" record). $1 is a short status
# token (up-to-date / blocked / error / published); $2, if given, is the SHA
# under consideration. Best-effort: a failure to write it (read-only $HOME,
# races) must never be why this script exits non-zero.
heartbeat() {
  mkdir -p "$RELEASES_DIR" 2>/dev/null || true
  printf '%s %s %s\n' "$(date +%s)" "$1" "${2:-}" > "$HEARTBEAT_FILE" 2>/dev/null || true
}

# Resolves the installed `coord` binary for a READ-ONLY health-check probe —
# never installs, upgrades, or otherwise mutates ~/.coord-venv (see
# tests/test_deploy_coord_web_dist.py's ban on that). Mirrors the fallback
# chain scripts/azure-workers/epic-up.sh / epic-down.sh already use to find
# `coord` across dev boxes (~/.local/bin) and the dellserver production venv.
resolve_coord_bin() {
  local c
  for c in "${COORD_BIN:-}" "$HOME/.coord-venv/bin/coord" "$HOME/.local/bin/coord" "$(command -v coord 2>/dev/null || true)"; do
    if [[ -n "$c" && -x "$c" ]]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  return 1
}

# Boots $1 (a release directory) as a scratch `coord web` instance and probes
# it exactly like a browser hitting the live dashboard would: `/` must be the
# real SPA (not the legacy single-file fallback `--dist` silently serves for
# a missing/empty directory — see coord/commands/lifecycle.py's --dist help),
# and `/api/pipeline` must answer with real, parseable pipeline data. Always
# tears the scratch instance down before returning, pass or fail — a leaked
# listener on $HEALTH_CHECK_PORT would wedge every subsequent run.
health_check_release() {
  local release_dir="$1"
  local coord_bin fixture server_pid log_file waited base ok
  base="http://$HEALTH_CHECK_HOST:$HEALTH_CHECK_PORT"

  if ! coord_bin="$(resolve_coord_bin)"; then
    say "ERROR: no coord binary found (checked \$COORD_BIN, ~/.coord-venv/bin/coord, ~/.local/bin/coord, PATH) — cannot health-check $release_dir"
    return 1
  fi

  fixture="${HEALTH_CHECK_FIXTURE:-$WEBAPP_CHECKOUT/e2e/fixtures/board-pipeline-basic.json}"
  if [[ ! -f "$fixture" ]]; then
    say "ERROR: health-check fixture missing: $fixture — cannot health-check $release_dir"
    return 1
  fi

  log_file="$(mktemp)"
  "$coord_bin" web --host "$HEALTH_CHECK_HOST" --port "$HEALTH_CHECK_PORT" \
      --dist "$release_dir" --fixture "$fixture" \
      >"$log_file" 2>&1 &
  server_pid=$!
  # Belt-and-braces: this fires on every return path below (including the
  # early `return 1`s above happen before server_pid exists, so they're
  # unaffected), so a health check that errors out never leaks the listener.
  trap 'kill "$server_pid" 2>/dev/null; wait "$server_pid" 2>/dev/null; rm -f "$log_file"' RETURN

  ok=1
  waited=0
  while (( waited < HEALTH_CHECK_TIMEOUT_SECS )); do
    if curl -fsS -o /dev/null "$base/api/pipeline" 2>/dev/null; then
      ok=0
      break
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
      say "ERROR: scratch coord web (pid $server_pid) exited before answering — log follows:"
      cat "$log_file" >&2
      return 1
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if [[ "$ok" -ne 0 ]]; then
    say "ERROR: scratch coord web on $base did not answer /api/pipeline within ${HEALTH_CHECK_TIMEOUT_SECS}s — log follows:"
    cat "$log_file" >&2
    return 1
  fi

  local root_body root_status
  root_body="$(mktemp)"
  root_status="$(curl -fsS -o "$root_body" -w '%{http_code}' "$base/" 2>/dev/null)" || root_status="curl-failed"
  if [[ "$root_status" != "200" ]]; then
    say "ERROR: $base/ returned HTTP $root_status (want 200)"
    rm -f "$root_body"
    return 1
  fi
  if ! grep -q 'id="root"' "$root_body"; then
    say "ERROR: $base/ returned 200 but not the SPA (no id=\"root\" marker) — likely the legacy fallback dashboard, meaning $release_dir has no usable dist/"
    rm -f "$root_body"
    return 1
  fi
  rm -f "$root_body"

  if ! curl -fsS "$base/api/pipeline" 2>/dev/null | python3 -c 'import json, sys; data = json.load(sys.stdin); sys.exit(0 if isinstance(data, list) and len(data) > 0 else 1)'; then
    say "ERROR: $base/api/pipeline did not return a non-empty JSON array"
    return 1
  fi

  say "health check passed: $base/ serves the SPA, /api/pipeline answers"
  return 0
}

# ── Single-instance guard ───────────────────────────────────────────────────
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  say "another coord-web-dist-build is already running — skipping"
  exit 0
fi

if [[ ! -d "$BASE_CHECKOUT/.git" ]]; then
  say "ERROR: $BASE_CHECKOUT is not a git checkout (SRC_ROOT=$SRC_ROOT, REPO_NAME=$REPO_NAME)"
  heartbeat error
  exit 1
fi

if ! git -C "$BASE_CHECKOUT" fetch origin "$BRANCH" --quiet; then
  say "ERROR: git fetch origin $BRANCH failed in $BASE_CHECKOUT"
  heartbeat error
  exit 1
fi
NEW_SHA="$(git -C "$BASE_CHECKOUT" rev-parse "origin/$BRANCH")" || {
  say "ERROR: git rev-parse origin/$BRANCH failed in $BASE_CHECKOUT"
  heartbeat error
  exit 1
}
if [[ -z "$NEW_SHA" ]]; then
  say "ERROR: resolved an empty SHA for origin/$BRANCH — refusing to proceed"
  heartbeat error
  exit 1
fi

# ── Up-to-date check — cheap, SILENT no-op when nothing merged since last
# run (#2122): this is by far the common case at this timer's cadence, and a
# journal line here is exactly what buried the host's log — see this
# script's header. The heartbeat write below is the ONLY record of this
# tick; it deliberately does not go through `say`/stderr/the journal.
CURRENT_RELEASE="none"
if [[ -L "$LIVE_LINK" ]]; then
  CURRENT_RELEASE="$(basename "$(readlink -f "$LIVE_LINK")")"
  if [[ "$CURRENT_RELEASE" == "$NEW_SHA" ]]; then
    heartbeat up-to-date "$NEW_SHA"
    exit 0
  fi
fi

# ── Rollback-sentinel guard (#1560) ─────────────────────────────────────────
# The up-to-date check above only short-circuits when $NEW_SHA is already
# live. It does NOT protect an operator who just ran coord-web-rollback.sh:
# after a rollback, $CURRENT_RELEASE is the GOOD sha they rolled back TO, so
# $NEW_SHA (still the bad commit, since fixing/reverting main realistically
# takes longer than this timer's 10min cadence, #2122) no longer matches it,
# and this script would otherwise rebuild and republish the exact bad commit
# the operator just rolled back away from — silently, within about 10
# minutes, undoing their recovery. Refuse instead, until either main moves
# past that SHA or the operator explicitly clears the sentinel.
if [[ -f "$BLOCKED_SHA_FILE" ]]; then
  BLOCKED_SHA="$(<"$BLOCKED_SHA_FILE")"
  BLOCKED_SHA="${BLOCKED_SHA//[$'\t\r\n ']/}"
  if [[ -n "$BLOCKED_SHA" && "$NEW_SHA" == "$BLOCKED_SHA" ]]; then
    say "REFUSING to build/publish $NEW_SHA: this is the exact SHA an operator rolled back FROM"
    say "(sentinel: $BLOCKED_SHA_FILE, written by coord-web-rollback.sh). Publishing it now would"
    say "silently undo that rollback within about 10 minutes of them performing it, with no other warning."
    say "Fix or revert the bad commit on origin/$BRANCH, or pause this timer entirely while you work on it:"
    say "  systemctl --user stop coord-web-dist-build.timer"
    say "  systemctl --user start coord-web-dist-build.timer   # resume once main is fixed"
    say "If you are certain $NEW_SHA is actually fine and the rollback was a false alarm, clear the"
    say "sentinel to allow it again:"
    say "  rm $BLOCKED_SHA_FILE"
    heartbeat blocked "$NEW_SHA"
    exit 1
  fi
  if [[ -n "$BLOCKED_SHA" ]]; then
    say "origin/$BRANCH ($NEW_SHA) has moved past the previously-blocked SHA $BLOCKED_SHA — clearing sentinel $BLOCKED_SHA_FILE"
    rm -f "$BLOCKED_SHA_FILE"
  fi
fi

# ── Bootstrap or fast-forward the dedicated worktree ───────────────────────
if [[ ! -d "$WEBAPP_CHECKOUT" ]]; then
  say "creating dedicated worktree at $WEBAPP_CHECKOUT"
  if ! git -C "$BASE_CHECKOUT" worktree add --detach "$WEBAPP_CHECKOUT" "origin/$BRANCH" --quiet; then
    say "ERROR: git worktree add failed"
    heartbeat error "$NEW_SHA"
    exit 1
  fi
else
  if ! git -C "$WEBAPP_CHECKOUT" checkout --detach "$NEW_SHA" --quiet; then
    say "ERROR: git checkout --detach $NEW_SHA failed in $WEBAPP_CHECKOUT"
    heartbeat error "$NEW_SHA"
    exit 1
  fi
fi

say "building $NEW_SHA"
RELEASE_DIR="$RELEASES_DIR/$NEW_SHA"
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASES_DIR"

# #2009: `coord-web`'s repo root IS the webapp root, so $WEBAPP_SUBDIR is
# empty by default and this collapses to $WEBAPP_CHECKOUT.
WEBAPP_DIR="$WEBAPP_CHECKOUT${WEBAPP_SUBDIR:+/$WEBAPP_SUBDIR}"
if ! ( cd "$WEBAPP_DIR" && npm ci --no-audit --no-fund && npm run build ); then
  say "ERROR: npm build failed for $NEW_SHA — live dashboard unchanged (still $CURRENT_RELEASE)"
  heartbeat error "$NEW_SHA"
  exit 1
fi

if [[ ! -f "$WEBAPP_DIR/dist/index.html" ]]; then
  say "ERROR: build produced no dist/index.html — refusing to publish $NEW_SHA"
  heartbeat error "$NEW_SHA"
  exit 1
fi

if ! mv "$WEBAPP_DIR/dist" "$RELEASE_DIR"; then
  say "ERROR: could not move built dist/ into $RELEASE_DIR — live dashboard unchanged (still $CURRENT_RELEASE)"
  heartbeat error "$NEW_SHA"
  exit 1
fi

# ── Health-check before cutover (#1560) ─────────────────────────────────────
# $RELEASE_DIR only ever survives past this point if it passes — see the
# "Why fail-closed" note atop this file. A failing release is deleted here,
# never left in $RELEASES_DIR, so the rollback script's "second-newest
# directory on disk" always resolves to the last known GOOD release.
say "health-checking $NEW_SHA on $HEALTH_CHECK_HOST:$HEALTH_CHECK_PORT before cutover"
if ! health_check_release "$RELEASE_DIR"; then
  say "ERROR: health check failed for $NEW_SHA — refusing to publish, live dashboard unchanged (still $CURRENT_RELEASE)"
  rm -rf "$RELEASE_DIR"
  heartbeat error "$NEW_SHA"
  exit 1
fi

# ── Atomic publish: create the new symlink under a temp name, then rename(2)
# it over the live name. rename() on a symlink is a single syscall — there is
# no window where $LIVE_LINK is missing or half-updated.
#
# Every step here is checked, same as every other step above: a heartbeat of
# "published" is a claim that $LIVE_LINK now actually points at $RELEASE_DIR,
# not just that the commands to make it so were issued. `set -uo pipefail`
# alone would NOT catch a failing `ln`/`mv` here (no `-e`), and a silent
# failure would leave the live symlink on the OLD release while the
# heartbeat — the one surface built to catch a dead/failed publish — reports
# a fresh success. See the #2122 review that flagged this.
if ! ln -sfn "$RELEASE_DIR" "$LIVE_LINK.new"; then
  say "ERROR: ln -sfn $RELEASE_DIR $LIVE_LINK.new failed — refusing to publish, live dashboard unchanged (still $CURRENT_RELEASE)"
  rm -f "$LIVE_LINK.new"
  heartbeat error "$NEW_SHA"
  exit 1
fi
if ! mv -Tf "$LIVE_LINK.new" "$LIVE_LINK"; then
  say "ERROR: mv -Tf $LIVE_LINK.new $LIVE_LINK failed — live dashboard unchanged (still $CURRENT_RELEASE)"
  rm -f "$LIVE_LINK.new"
  heartbeat error "$NEW_SHA"
  exit 1
fi
# Verify post-hoc, not just that the commands exited 0: confirm the live
# symlink actually resolves to the release we just built before claiming
# success (belt-and-braces against any exit-code/effect mismatch, e.g. an
# `mv` that races an external actor touching $LIVE_LINK).
if [[ "$(readlink -f "$LIVE_LINK")" != "$RELEASE_DIR" ]]; then
  say "ERROR: $LIVE_LINK does not resolve to $RELEASE_DIR after publish — live dashboard state uncertain (was $CURRENT_RELEASE)"
  heartbeat error "$NEW_SHA"
  exit 1
fi
say "published $NEW_SHA -> $LIVE_LINK (no coord-web restart needed)"
heartbeat published "$NEW_SHA"

# ── Prune old releases, keep the newest $KEEP_RELEASES ─────────────────────
# shellcheck disable=SC2012
ls -dt "$RELEASES_DIR"/*/ 2>/dev/null | tail -n "+$((KEEP_RELEASES + 1))" | while read -r old; do
  say "pruning old release $old"
  rm -rf "$old"
done
