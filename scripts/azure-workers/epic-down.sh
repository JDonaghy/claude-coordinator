#!/usr/bin/env bash
# Tear down an epic's worker VM, safely.
#
#   ./epic-down.sh --epic 1537
#
# Order matters. Deleting the resource group under a running worker loses any
# work that has not been pushed, so: stop routing NEW work, wait for in-flight
# work to finish, deregister, and only then delete.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Subscription-specific IDs live outside the repo, alongside coord's other
# state. Override with EPIC_ENV for a second subscription.
EPIC_ENV="${EPIC_ENV:-$HOME/.coord/epic.env}"
[[ -f "$EPIC_ENV" ]] || { echo "missing $EPIC_ENV — populate it from bootstrap-shared.sh output" >&2; exit 1; }
# shellcheck source=/dev/null
source "$EPIC_ENV"

# A placeholder left unfilled would otherwise surface as an opaque Azure error
# several minutes and one running VM later.
for _v in SUBSCRIPTION_ID KEY_VAULT_URI KEY_VAULT_RESOURCE_ID IDENTITY_RESOURCE_ID \
          IDENTITY_CLIENT_ID PRIVATE_DNS_ZONE_ID SOURCE_IMAGE_ID; do
    if [[ "${!_v:-}" == *"<"* || -z "${!_v:-}" ]]; then
        echo "$_v is unset or still a placeholder in $EPIC_ENV" >&2; exit 1
    fi
done
unset _v

EPIC=""; MACHINE=""; FORCE=0; DRAIN_TIMEOUT=3600
DAEMON_HOST="${DAEMON_HOST:-dellserver}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --epic)          EPIC="$2"; shift 2 ;;
        --machine)       MACHINE="$2"; shift 2 ;;
        --drain-timeout) DRAIN_TIMEOUT="$2"; shift 2 ;;
        --force)         FORCE=1; shift ;;   # skip the drain wait, destroy anyway
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$EPIC" ]] || { echo "usage: $0 --epic <n> [--force]" >&2; exit 2; }

MACHINE="${MACHINE:-azure-epic${EPIC}}"
RG="rg-coord-epic${EPIC}"
log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

az group show -n "$RG" -o none 2>/dev/null || { echo "$RG does not exist — nothing to do"; exit 0; }

# --------------------------------------------------------------------------
log "1/5  stop routing new work to $MACHINE"
# `coord pause` explicitly does NOT cancel in-flight assignments -- that is what
# makes it the right call here. Run it on the daemon host so it is unambiguous
# which config and board are in play.
ssh "$DAEMON_HOST" bash -euo pipefail -s -- "$MACHINE" <<'REMOTE' || {
MACHINE="$1"
COORD=""
for c in "${COORD_BIN:-}" "$HOME/.coord-venv/bin/coord" "$HOME/.local/bin/coord" "$(command -v coord 2>/dev/null)"; do
    [[ -n "$c" && -x "$c" ]] && { COORD="$c"; break; }
done
[[ -n "$COORD" ]] || { echo "coord not found on this host" >&2; exit 1; }
"$COORD" pause "$MACHINE"
REMOTE
    echo "  pause FAILED — new work may still route to $MACHINE. Investigate before continuing." >&2
    exit 1
}
echo "  paused"

# --------------------------------------------------------------------------
log "2/5  drain in-flight work"
if [[ $FORCE -eq 1 ]]; then
    echo "  --force: skipping drain. Unpushed work on this VM will be LOST."
else
    # The agent's own /health reports `active` = count of RUNNING assignments.
    # That is the authoritative per-machine signal; the board can lag it.
    deadline=$(( SECONDS + DRAIN_TIMEOUT ))
    while true; do
        health="$(curl -fsS --max-time 5 "http://${MACHINE}:7433/health" 2>/dev/null || true)"
        if [[ -z "$health" ]]; then
            echo "  agent unreachable — assuming already down"; break
        fi
        active="$(jq -r '.active // 0' <<<"$health")"
        [[ "$active" == "0" ]] && { echo "  idle (active=0)"; break; }
        (( SECONDS < deadline )) || {
            echo "  still $active assignment(s) running after ${DRAIN_TIMEOUT}s." >&2
            echo "  Re-run with --force to destroy anyway, or 'coord stop <id>' first." >&2
            exit 1
        }
        printf '  active=%s, waiting...\n' "$active"; sleep 30
    done

    # Interactive tmux sessions are invisible to /health's assignment count --
    # a testing or merge agent someone is driving by hand would be killed
    # silently. Check before destroying.
    if sessions="$(coord sessions --remote --json 2>/dev/null)" \
       && [[ -n "$sessions" ]] \
       && jq -e --arg m "$MACHINE" 'if type=="array" then any(.[]; (.machine // "")==$m) else false end' <<<"$sessions" >/dev/null 2>&1; then
        echo "  WARNING: live interactive session(s) on $MACHINE:" >&2
        jq -r --arg m "$MACHINE" '.[] | select((.machine // "")==$m) | "    \(.name // .session // "?")"' <<<"$sessions" >&2
        echo "  Finish or detach them, or re-run with --force." >&2
        exit 1
    fi
fi

# --------------------------------------------------------------------------
log "3/5  deregister from coordinator.yml"
REMOTE_HELPER="$(ssh "$DAEMON_HOST" 'mktemp /tmp/coordinator-machine.XXXXXX.py')"
trap 'ssh "$DAEMON_HOST" "rm -f $REMOTE_HELPER" 2>/dev/null || true' EXIT
scp -q "$HERE/coordinator-machine.py" "${DAEMON_HOST}:${REMOTE_HELPER}"
ssh "$DAEMON_HOST" bash -euo pipefail -s -- "$MACHINE" "$REMOTE_HELPER" <<'REMOTE'
MACHINE="$1"; HELPER="$2"
CFG="$HOME/.coord/coordinator.yml"
# #1887: same fix as epic-up.sh's registration step -- resolve the symlink
# into the coord-settings checkout (#1832) before writing, so `mv` lands on
# the real, version-controlled file instead of replacing the symlink itself.
CFG="$(readlink -f "$CFG")"
TMP="$(mktemp "${CFG}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

python3 "$HELPER" --file "$CFG" --out "$TMP" remove --name "$MACHINE"

# Same parser the daemon uses -- see the note in epic-up.sh about why
# `coord config --config` is not a valid check here.
# Resolve coord explicitly. `ssh host 'cmd'` runs a NON-login shell, and coord
# lives in a venv that is only on PATH for interactive logins -- so
# `command -v coord` finds nothing over ssh. ~/.coord-venv is what the daemon's
# own ExecStart uses, so its interpreter is the parser the daemon parses with.
resolve_coord() {
    local c
    for c in "${COORD_BIN:-}" "$HOME/.coord-venv/bin/coord" "$HOME/.local/bin/coord" "$(command -v coord 2>/dev/null)"; do
        [[ -n "$c" && -x "$c" ]] && { echo "$c"; return 0; }
    done
    return 1
}
COORD="$(resolve_coord)" || { echo "coord not found on this host" >&2; exit 1; }
PYBIN="$(dirname "$COORD")/python"
[[ -x "$PYBIN" ]] || PYBIN="$(head -1 "$COORD" | sed 's|^#!||')"
"$PYBIN" - "$TMP" "$MACHINE" <<'PYEOF'
import sys
from pathlib import Path
from coord.config import load
cfg = load(Path(sys.argv[1]))
names = [m.name for m in cfg.machines]
if sys.argv[2] in names:
    sys.exit(f"validation failed: {sys.argv[2]} still present after removal")
if not names:
    sys.exit("validation failed: removal emptied the machines list")
print(f"  validated: {len(names)} machines remain")
PYEOF

chmod --reference="$CFG" "$TMP"
mv "$TMP" "$CFG"
trap - EXIT
echo "  coordinator.yml updated"

# #1887: same reasoning as epic-up.sh -- deregistering is also a content
# change inside coord-settings when #1832's symlink applies. Surface the
# exact commit rather than leaving the checkout silently dirty.
if git_root="$(git -C "$(dirname "$CFG")" rev-parse --show-toplevel 2>/dev/null)"; then
    echo "  NOTE: $CFG lives in $git_root -- commit the change there, e.g.:"
    echo "    git -C $git_root add $(realpath --relative-to="$git_root" "$CFG") && git -C $git_root commit -m 'coord: deregister $MACHINE'"
fi
REMOTE

# --------------------------------------------------------------------------
log "4/5  delete $RG"
# Takes the VM, OS disk, NIC, NSG, VNet, NAT Gateway, public IP, the Key Vault
# private endpoint and its DNS zone link -- everything billable.
az group delete -n "$RG" --yes --no-wait
echo "  deletion started (async)"

# --------------------------------------------------------------------------
log "5/5  done"
cat <<EOF

  $MACHINE deregistered; $RG deleting.

  The tailnet node removes itself: it joined with ephemeral=true, so Tailscale
  drops it on shutdown. If it lingers as 'offline' for more than a few minutes,
  remove it by hand in the admin console.

  Verify:  az group show -n $RG   (should 404 shortly)
           coord status           (should no longer list $MACHINE)

EOF
