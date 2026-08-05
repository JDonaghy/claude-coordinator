#!/usr/bin/env bash
#
# coord-web-dist-build.sh — build the React webapp from merged `main` into a
# directory `coord web --dist` serves, WITHOUT touching ~/.coord-venv (#1543).
#
# Why this exists: coord-web, coord-agent and coord-serve all `ExecStart`
# from the SAME ~/.coord-venv, so "ship a webapp change" used to mean
# `pip install --upgrade claude-coordinator` on that venv — which also
# upgrades the board daemon and the agent runtime on that host, and
# `coord agent update` is already known to kill running headless workers
# (see the #1543 issue body). This script decouples the two: it keeps an
# independent worktree of this repo, builds `coord/dashboard/webapp` from
# merged main into a fresh, timestamped release directory, and atomically
# repoints a symlink at it. `coord web --dist ~/coord-web-dist` (see
# deploy/coord-web.service) always points at that symlink.
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
# never touches ~/.coord-venv, ~/src/claude-coordinator's own checkout (it
# uses a DEDICATED `git worktree`, so it can't collide with whatever branch
# an operator has that checkout parked on — same reasoning as `coord drive`'s
# worktree isolation), or any running coord-agent/coord-serve/coord-web
# process. A `flock` guards against overlapping runs; a same-SHA re-run is a
# fast no-op.
#
# Install:
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
# Rollback (one command — pairs with #1560): repoint the live symlink at the
# previous release directory, still on disk under $RELEASES_DIR:
#
#   ln -sfn "$(ls -dt ~/.coord-web-releases/*/ | sed -n 2p)" ~/coord-web-dist
#
# (lists release dirs newest-first, picks the SECOND newest — i.e. the one
# before the current live release — and repoints the symlink at it; no
# restart needed, same as a forward build.)
#
# See docs/PHONE_WEBAPP.md and docs/AGENT_OPERATIONS.md for the full runbook.

set -uo pipefail

SRC_ROOT="${SRC_ROOT:-$HOME/src}"
REPO_NAME="${REPO_NAME:-claude-coordinator}"
BASE_CHECKOUT="${BASE_CHECKOUT:-$SRC_ROOT/$REPO_NAME}"
BRANCH="${BRANCH:-main}"

# A worktree DEDICATED to this script — never the same directory an operator
# or `coord drive` worktree might be using, and never checked out over a
# branch someone else parked there.
WEBAPP_CHECKOUT="${WEBAPP_CHECKOUT:-$HOME/.coord-web-checkout}"
RELEASES_DIR="${RELEASES_DIR:-$HOME/.coord-web-releases}"
LIVE_LINK="${LIVE_LINK:-$HOME/coord-web-dist}"
KEEP_RELEASES="${KEEP_RELEASES:-3}"
LOCK_FILE="${LOCK_FILE:-$HOME/.coord-web-dist-build.lock}"

say() { echo "[$(date -Is)] $*" >&2; }

# ── Single-instance guard ───────────────────────────────────────────────────
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  say "another coord-web-dist-build is already running — skipping"
  exit 0
fi

if [[ ! -d "$BASE_CHECKOUT/.git" ]]; then
  say "ERROR: $BASE_CHECKOUT is not a git checkout (SRC_ROOT=$SRC_ROOT, REPO_NAME=$REPO_NAME)"
  exit 1
fi

if ! git -C "$BASE_CHECKOUT" fetch origin "$BRANCH" --quiet; then
  say "ERROR: git fetch origin $BRANCH failed in $BASE_CHECKOUT"
  exit 1
fi
NEW_SHA="$(git -C "$BASE_CHECKOUT" rev-parse "origin/$BRANCH")" || {
  say "ERROR: git rev-parse origin/$BRANCH failed in $BASE_CHECKOUT"
  exit 1
}
if [[ -z "$NEW_SHA" ]]; then
  say "ERROR: resolved an empty SHA for origin/$BRANCH — refusing to proceed"
  exit 1
fi

# ── Up-to-date check — cheap no-op when nothing merged since last run ──────
CURRENT_RELEASE="none"
if [[ -L "$LIVE_LINK" ]]; then
  CURRENT_RELEASE="$(basename "$(readlink -f "$LIVE_LINK")")"
  if [[ "$CURRENT_RELEASE" == "$NEW_SHA" ]]; then
    say "up to date at $NEW_SHA — nothing to build"
    exit 0
  fi
fi

# ── Bootstrap or fast-forward the dedicated worktree ───────────────────────
if [[ ! -d "$WEBAPP_CHECKOUT" ]]; then
  say "creating dedicated worktree at $WEBAPP_CHECKOUT"
  if ! git -C "$BASE_CHECKOUT" worktree add --detach "$WEBAPP_CHECKOUT" "origin/$BRANCH" --quiet; then
    say "ERROR: git worktree add failed"
    exit 1
  fi
else
  if ! git -C "$WEBAPP_CHECKOUT" checkout --detach "$NEW_SHA" --quiet; then
    say "ERROR: git checkout --detach $NEW_SHA failed in $WEBAPP_CHECKOUT"
    exit 1
  fi
fi

say "building $NEW_SHA"
RELEASE_DIR="$RELEASES_DIR/$NEW_SHA"
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASES_DIR"

WEBAPP_DIR="$WEBAPP_CHECKOUT/coord/dashboard/webapp"
if ! ( cd "$WEBAPP_DIR" && npm ci --no-audit --no-fund && npm run build ); then
  say "ERROR: npm build failed for $NEW_SHA — live dashboard unchanged (still $CURRENT_RELEASE)"
  exit 1
fi

if [[ ! -f "$WEBAPP_DIR/dist/index.html" ]]; then
  say "ERROR: build produced no dist/index.html — refusing to publish $NEW_SHA"
  exit 1
fi

if ! mv "$WEBAPP_DIR/dist" "$RELEASE_DIR"; then
  say "ERROR: could not move built dist/ into $RELEASE_DIR — live dashboard unchanged (still $CURRENT_RELEASE)"
  exit 1
fi

# ── Atomic publish: create the new symlink under a temp name, then rename(2)
# it over the live name. rename() on a symlink is a single syscall — there is
# no window where $LIVE_LINK is missing or half-updated.
ln -sfn "$RELEASE_DIR" "$LIVE_LINK.new"
mv -Tf "$LIVE_LINK.new" "$LIVE_LINK"
say "published $NEW_SHA -> $LIVE_LINK (no coord-web restart needed)"

# ── Prune old releases, keep the newest $KEEP_RELEASES ─────────────────────
# shellcheck disable=SC2012
ls -dt "$RELEASES_DIR"/*/ 2>/dev/null | tail -n "+$((KEEP_RELEASES + 1))" | while read -r old; do
  say "pruning old release $old"
  rm -rf "$old"
done
