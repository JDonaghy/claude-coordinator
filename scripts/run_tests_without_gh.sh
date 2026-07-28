#!/usr/bin/env bash
#
# run_tests_without_gh.sh — the #1484 enumeration/regression check: run a
# command (default: the full pytest suite) with `gh` made unresolvable on
# PATH, no matter which directory it actually lives in on this host.
#
#   scripts/run_tests_without_gh.sh [-- ] [command...]
#
# Why not just `PATH=/usr/bin:/bin ...`? That only excludes `gh` on hosts
# where it happens to live outside those two directories. Two machines in
# this fleet (dellserver, precision) have it at /usr/bin/gh — the literal
# recipe would silently include it there and defeat the point. This script
# instead walks each directory already on $PATH and, for any directory that
# actually contains a `gh` binary, substitutes a throwaway shadow directory
# holding a symlink to every OTHER entry in it — every other directory on
# $PATH (notably an active venv's bin/, e.g. python -> python3, a relative
# symlink that must be resolved from within its own directory to pick up
# venv's pyvenv.cfg) is left untouched, in its original position, so nothing
# about how the rest of the toolchain resolves changes. So the exclusion is
# real regardless of where `gh` is installed (apt, linuxbrew, snap, …) or
# where it is absent, without disturbing any other PATH entry.
#
# #1484: found via the #1472 elitebook incident — a worker's systemd PATH
# excluded the linuxbrew-installed `gh`, so ~98 tests that inject every other
# dependency still reached a live `gh` subprocess and either crashed
# (`FileNotFoundError`) or silently produced host-dependent results. This
# script is both the reproduction the fix was verified against and the CI
# guard (.github/workflows/test.yml's `no-gh-on-path` job) that keeps it from
# regressing invisibly.
#
# Exit codes: whatever the wrapped command exits with; 2 if `gh` is somehow
# still resolvable after exclusion (the guard itself is broken — fail loud).

set -euo pipefail

_shadow_root="$(mktemp -d)"
trap 'rm -rf "$_shadow_root"' EXIT

_new_path=""
_i=0
IFS=':' read -ra _dirs <<< "$PATH"
for d in "${_dirs[@]}"; do
    if [[ -d "$d" && -e "$d/gh" ]]; then
        _i=$((_i + 1))
        shadow="$_shadow_root/$_i"
        mkdir -p "$shadow"
        for f in "$d"/*; do
            [[ -e "$f" ]] || continue
            name="$(basename "$f")"
            [[ "$name" == "gh" ]] && continue
            ln -s "$f" "$shadow/$name" 2>/dev/null || true
        done
        d="$shadow"
    fi
    _new_path="${_new_path:+$_new_path:}$d"
done

export PATH="$_new_path"

if command -v gh >/dev/null 2>&1; then
    echo "run_tests_without_gh.sh: gh is STILL resolvable at $(command -v gh) after exclusion — the guard itself is broken" >&2
    exit 2
fi

if [[ $# -eq 0 ]]; then
    set -- python -m pytest tests/ -q
fi

exec "$@"
