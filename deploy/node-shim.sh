#!/usr/bin/env bash
# coord node shim — resolve Node at RUN time, never at install time (#1678).
#
# Installed by install-agent.sh as ~/.local/bin/coord-node-shim, with
# node/npm/npx symlinked at it. That directory is ALREADY on the coord agent
# unit's PATH (deploy/coord-agent.service), so this file needs no change to
# the PATH mechanism #1671 shipped — it only makes Node resolvable through a
# directory that is already there, for the agent and for every worker it
# spawns (#402: a worker's PATH is the agent's, venv stripped).
#
# Why a shim rather than another PATH entry: nvm installs into a
# VERSION-STAMPED directory (~/.nvm/versions/node/vX.Y.Z/bin). Baking that
# into the unit works exactly until the next `nvm install`, at which point
# the `browser` capability silently goes unmet again and webapp Test stages
# resume the invisible 30s refusal loop — a fresh instance of the very bug
# this shim closes. Re-resolving on each invocation makes a Node version
# bump a no-op.
#
# Dispatch is by argv[0]: whatever name you invoke it under is the binary it
# execs from the resolved Node bin directory.
#
# tests/test_node_shim.py asserts (a) this file embeds no vX.Y.Z literal and
# (b) install-agent.sh ships a byte-identical copy.

set -uo pipefail

self="${0##*/}"
shim_dir="$(cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)" || shim_dir=""
nvm_dir="${NVM_DIR:-$HOME/.nvm}"
versions_dir="$nvm_dir/versions/node"

resolved=""

# 1. nvm's `default` alias, when it names an installed version outright.
#    (It may instead hold a symbolic name such as `node` or `lts/*`; those
#    fall through to the newest-installed scan below, which is how nvm
#    itself resolves `default -> node`.)
if [ -r "$nvm_dir/alias/default" ]; then
    want="$(tr -d '[:space:]' < "$nvm_dir/alias/default" 2>/dev/null)"
    if [ -n "$want" ]; then
        for cand in "$want" "v$want"; do
            if [ -x "$versions_dir/$cand/bin/node" ]; then
                resolved="$versions_dir/$cand/bin"
                break
            fi
        done
    fi
fi

# 2. Otherwise the highest installed version. `sort -V` is GNU coreutils and
#    recent BSD; fall back to lexical order where it is unsupported.
if [ -z "$resolved" ] && [ -d "$versions_dir" ]; then
    installed="$(ls -1 "$versions_dir" 2>/dev/null | sort -V 2>/dev/null)"
    [ -n "$installed" ] || installed="$(ls -1 "$versions_dir" 2>/dev/null | sort)"
    while IFS= read -r cand; do
        [ -n "$cand" ] || continue
        [ -x "$versions_dir/$cand/bin/node" ] && resolved="$versions_dir/$cand/bin"
    done <<EOF
$installed
EOF
fi

if [ -n "$resolved" ] && [ -x "$resolved/$self" ]; then
    exec "$resolved/$self" "$@"
fi

# 3. Last resort: a system install elsewhere on PATH. Skip this shim's own
#    directory so a missing Node exits honestly instead of exec-looping.
IFS=':' read -r -a _shim_path_dirs <<< "${PATH:-}"
for dir in "${_shim_path_dirs[@]}"; do
    [ -n "$dir" ] || continue
    real="$(cd -- "$dir" 2>/dev/null && pwd -P)" || continue
    [ -n "$shim_dir" ] && [ "$real" = "$shim_dir" ] && continue
    if [ -x "$real/$self" ]; then
        exec "$real/$self" "$@"
    fi
done

echo "coord node shim: no '$self' found (NVM_DIR=$nvm_dir, and none on \$PATH)" >&2
exit 127
