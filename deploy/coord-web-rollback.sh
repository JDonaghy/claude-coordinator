#!/usr/bin/env bash
#
# coord-web-rollback.sh — one-command last-known-good rollback for the live
# `coord web` deploy (#1560, pairs with #1543's coord-web-dist-build.sh).
#
# What it does: repoints ~/coord-web-dist (the symlink coord-web.service
# serves from — see deploy/coord-web.service / docs/PHONE_WEBAPP.md) at the
# release directory that was live just BEFORE the current one, still on disk
# under ~/.coord-web-releases. No restart of coord-web is required — same as
# a forward publish, Starlette re-resolves the served directory on every
# request (coord/dashboard/server.py's `webapp_dist` seam).
#
# Why "second newest directory on disk" is safe to trust as "last known
# GOOD": coord-web-dist-build.sh only ever moves a release into
# ~/.coord-web-releases AFTER it passes the pre-cutover health check (#1560)
# — a failed build/health-check is deleted, never left there. So every
# directory this script can see was, at some point, actually served live.
#
# Written to be reachable and typeable UNDER STRESS, from a phone ssh client,
# without this issue or any docs in hand:
#
#   ssh <dellserver> ~/.local/bin/coord-web-rollback.sh
#
# See docs/PHONE_WEBAPP.md ("Recovery: rolling back a bad deploy") for the
# full runbook and a timed drill transcript.
#
# NOT durable against coord-web-dist-build.timer on its own: that timer
# re-fires every ~10min (deploy/coord-web-dist-build.timer, #2122) and
# re-resolves origin/main, which — because fixing/reverting a bad commit on
# main realistically takes longer than that — will still be the exact SHA
# just rolled back FROM. Without a guard, the timer would rebuild that same
# bad commit, pass it through the identical health check it passed the
# first time (a client-side-only JS runtime error is exactly the class of
# bug that check cannot catch — see coord-web-dist-build.sh's "Why
# fail-closed" note), and republish it live, silently undoing this rollback
# within about 10 minutes of when it ran. To close that gap, this script writes a
# sentinel ($RELEASES_DIR/.rollback-blocked-sha) naming the bad SHA, and
# coord-web-dist-build.sh refuses to build/publish that exact SHA again
# until main moves past it or the sentinel is cleared by hand — see its
# "Rollback-sentinel guard" block. If you want the timer to simply not run
# at all while you fix main, pause it directly:
#
#   systemctl --user stop coord-web-dist-build.timer
#   systemctl --user start coord-web-dist-build.timer   # resume when ready
#
# Install (one-time, alongside coord-web-dist-build.sh):
#   cp deploy/coord-web-rollback.sh ~/.local/bin/
#   chmod +x ~/.local/bin/coord-web-rollback.sh

set -uo pipefail

RELEASES_DIR="${RELEASES_DIR:-$HOME/.coord-web-releases}"
LIVE_LINK="${LIVE_LINK:-$HOME/coord-web-dist}"
# Must match coord-web-dist-build.sh's default exactly — this is the
# handoff point between the two scripts (#1560).
BLOCKED_SHA_FILE="${BLOCKED_SHA_FILE:-$RELEASES_DIR/.rollback-blocked-sha}"

say() { echo "[$(date -Is)] $*" >&2; }

# shellcheck disable=SC2012
mapfile -t RELEASES < <(ls -dt "$RELEASES_DIR"/*/ 2>/dev/null | sed 's:/*$::')

if (( ${#RELEASES[@]} < 2 )); then
  say "ERROR: fewer than 2 releases on disk under $RELEASES_DIR (found ${#RELEASES[@]}) — nothing to roll back to."
  say "This is expected right after the very first deploy; it is not recoverable by this script until a second release has been built."
  exit 1
fi

CURRENT="none"
if [[ -L "$LIVE_LINK" ]]; then
  CURRENT="$(readlink -f "$LIVE_LINK")"
  CURRENT="${CURRENT%/}"
fi

TARGET=""
for r in "${RELEASES[@]}"; do
  if [[ "$r" != "$CURRENT" ]]; then
    TARGET="$r"
    break
  fi
done

if [[ -z "$TARGET" ]]; then
  # Every entry matched $CURRENT (or $LIVE_LINK was absent) — fall back to
  # "the newest release that isn't literally what we just resolved as
  # current", i.e. the second array entry, same as the documented one-liner.
  TARGET="${RELEASES[1]}"
fi

if [[ "$TARGET" == "$CURRENT" ]]; then
  say "ERROR: could not find a release on disk different from the current live one ($CURRENT) — refusing to no-op rollback."
  exit 1
fi

if [[ ! -f "$TARGET/index.html" ]]; then
  say "ERROR: candidate rollback target $TARGET has no index.html — refusing to publish a broken release."
  say "On disk under $RELEASES_DIR: ${RELEASES[*]}"
  exit 1
fi

say "rolling back: $CURRENT -> $TARGET"

# Atomic publish — identical pattern to coord-web-dist-build.sh's forward
# publish: symlink under a temp name, then rename(2) over the live name. No
# window where $LIVE_LINK is missing or half-updated.
ln -sfn "$TARGET" "$LIVE_LINK.new"
mv -Tf "$LIVE_LINK.new" "$LIVE_LINK"

say "done: $LIVE_LINK -> $TARGET (no coord-web restart needed)"
say "coord-serve (7435) and coord-agent (7433) were not touched by this script."

if [[ "$CURRENT" != "none" ]]; then
  BLOCKED_SHA="$(basename "$CURRENT")"
  printf '%s\n' "$BLOCKED_SHA" > "$BLOCKED_SHA_FILE"
  say "WARNING: coord-web-dist-build.timer fires again within about 10 minutes. It will refuse to"
  say "auto-republish $BLOCKED_SHA (the SHA you just rolled back FROM) thanks to the sentinel"
  say "just written to $BLOCKED_SHA_FILE — but that protection is scoped to that exact SHA: if"
  say "origin/main has ALREADY moved past it with more bad commits behind it, the timer will"
  say "build and publish those instead, and this script cannot protect you from that."
  say "Fix or revert the bad commit on main as soon as you can. If you'd rather the timer not run"
  say "at all until you have, pause it:"
  say "  systemctl --user stop coord-web-dist-build.timer"
  say "  systemctl --user start coord-web-dist-build.timer   # resume when ready"
fi
