#!/usr/bin/env bash
# Spin up an ephemeral worker VM for one epic and register it with the fleet.
#
#   ./epic-up.sh --epic 1537 --repos claude-coordinator,quadraui
#
# Everything lives in a per-epic resource group so epic-down.sh can delete it
# wholesale. Requires bootstrap-shared.sh to have run once, and epic.env to
# hold the IDs it printed.
set -euo pipefail

log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

# Comma-separated capability list helper: append $2 to the CSV in $1 unless
# already present. Pure string logic -- no ssh, no side effects -- so the
# #1799 opencode-detection path below is unit-testable without a live VM.
add_capability_if_missing() {
    local csv="$1" cap="$2"
    # `read -ra` (not `local -a caps=($csv)`) so a capability string
    # containing a shell glob character can't trigger pathname expansion --
    # current callers only ever pass plain identifiers, but this way that
    # stays true by construction rather than by convention.
    local -a caps
    IFS=',' read -ra caps <<< "$csv"
    local c
    for c in "${caps[@]}"; do
        [[ "$c" == "$cap" ]] && { echo "$csv"; return 0; }
    done
    if [[ -z "$csv" ]]; then
        echo "$cap"
    else
        echo "${csv},${cap}"
    fi
}

# #1799: whether the machine that was JUST provisioned actually has the
# opencode CLI on PATH for the `coord` user -- the account the daemon
# dispatches work as. Two things a naive `ssh <tailnet-name> 'command -v
# opencode'` gets wrong (both caught in review):
#
#   1. Wrong/no SSH user. `coordUser` ("coord") has no `authorized_keys` of
#      its own -- only the Bicep template's `adminUsername` (default
#      "azureuser", a distinct break-glass account) gets one populated from
#      `sshPublicKey`. Connecting with no explicit user authenticates as the
#      *operator's local OS username*, which has no account on the VM at
#      all, so the connection fails closed. Same fix build-worker-image.sh
#      already uses for its builder VM: `ssh ... ${ADMIN_USER}@${IP}`,
#      plus `StrictHostKeyChecking=accept-new` since this is the first SSH
#      to a brand-new host with no known_hosts entry (the /health poll in
#      step 2/5 proves tailnet reachability over HTTP, not SSH).
#   2. Wrong PATH even once connected. provision-worker.sh installs opencode
#      only into ~coord/.opencode/bin (symlinked into ~coord/.local/bin),
#      and its own prereq check only ever sees that via `as_coord`
#      (`sudo -u coord -H bash -lc "$*"` -- a LOGIN shell as `coord`).
#      Mirror that exactly here: sudo from the admin user into a `coord`
#      login shell rather than checking the admin user's own PATH.
#      `sudo -n` fails fast instead of hanging on a password prompt
#      BatchMode can't answer, if passwordless sudo is ever missing.
#
# Deliberately NOT a hardcoded `CAPABILITIES` default -- that was correct
# the day #1777 wrote it and wrong the day the image started shipping
# opencode (the exact drift this issue is about; see also #1800's
# golden-image staleness issue). Checking the actual machine means the
# capability tracks the image instead of a flag that can go stale again.
detect_opencode_capability() {
    local machine="$1"
    local admin_user="${2:-${ADMIN_USER:-azureuser}}"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 \
        "${admin_user}@${machine}" \
        'sudo -n -u coord -H bash -lc "command -v opencode >/dev/null 2>&1"'
}

# Pull one field out of a gallery image-version resource ID:
#   /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Compute/
#     galleries/<gallery>/images/<imageDef>/versions/<version>
# Pure string parsing, no az call -- kept separate from
# report_image_provenance so it's trivially unit-testable.
parse_image_id() {
    local id="$1" field="$2"
    case "$field" in
        rg)      sed -n 's#.*/resourceGroups/\([^/]*\)/.*#\1#p' <<<"$id" ;;
        gallery) sed -n 's#.*/galleries/\([^/]*\)/.*#\1#p' <<<"$id" ;;
        image)   sed -n 's#.*/images/\([^/]*\)/.*#\1#p' <<<"$id" ;;
        version) sed -n 's#.*/versions/\([^/]*\)$#\1#p' <<<"$id" ;;
    esac
}

# #1800: name the image version and publish date being deployed from, so a
# stale SOURCE_IMAGE_ID pin is visible at provision time instead of inferred
# later from missing software. Warns (does not fail) when a newer version
# exists in the gallery -- a deliberate pin is legitimate, but an accidental
# one should be loud.
report_image_provenance() {
    local id="$1"
    local ver rg gallery imgdef published newest
    ver="$(parse_image_id "$id" version)"
    rg="$(parse_image_id "$id" rg)"
    gallery="$(parse_image_id "$id" gallery)"
    imgdef="$(parse_image_id "$id" image)"

    published="$(az sig image-version show --ids "$id" \
        --query publishingProfile.publishedDate -o tsv 2>/dev/null || true)"
    echo "  image     ${ver:-unknown} (published ${published:-unknown})"

    if [[ -n "$gallery" && -n "$imgdef" ]]; then
        newest="$(az sig image-version list -g "$rg" --gallery-name "$gallery" \
            --gallery-image-definition "$imgdef" --query "[].name" -o tsv 2>/dev/null \
            | sort -V | tail -1)"
        if [[ -n "$newest" && -n "$ver" && "$newest" != "$ver" ]]; then
            echo "  WARNING: SOURCE_IMAGE_ID pins $ver but $newest is newer in $gallery/$imgdef." >&2
            echo "  If that's deliberate, ignore this. Otherwise fix SOURCE_IMAGE_ID in \$EPIC_ENV" >&2
            echo "  (or re-run build-worker-image.sh without --no-update-env)." >&2
        fi
    fi
}

main() {
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

EPIC=""; REPOS=""; CAPABILITIES="rust,python"; MACHINE=""
# The golden image bakes every repo at $REPO_ROOT/<name> (#1799) -- passed
# through to coordinator-machine.py so the generated entry gets a
# repo_paths: mapping without the caller spelling each repo's path out.
REPO_ROOT="~/src"
# The Bicep template's break-glass admin account -- the only one whose
# authorized_keys gets populated from sshPublicKey (see
# detect_opencode_capability above). Override if EASY_AZURE_DIR's template
# ever pins a different adminUsername.
ADMIN_USER="${ADMIN_USER:-azureuser}"
# Must match a family you hold quota in. D8as_v5 is 0/0 on this subscription;
# D8as_v7 is the same 8 vCPU / 32 GiB in a family that has cores.
VM_SIZE="Standard_D8as_v7"; MAX_WORKERS=2; READY_TIMEOUT=900; PAUSED=0
DAEMON_HOST="${DAEMON_HOST:-dellserver}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --epic)         EPIC="$2"; shift 2 ;;
        --repos)        REPOS="$2"; shift 2 ;;
        --capabilities) CAPABILITIES="$2"; shift 2 ;;
        --repo-root)    REPO_ROOT="$2"; shift 2 ;;
        --machine)      MACHINE="$2"; shift 2 ;;
        --vm-size)      VM_SIZE="$2"; shift 2 ;;
        --max-workers)  MAX_WORKERS="$2"; shift 2 ;;
        # Register the machine but leave it closed to new work. Use for a
        # first boot: an unproven VM that reports healthy is otherwise
        # immediately eligible for any dispatch in its repos.
        --paused)       PAUSED=1; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$EPIC" && -n "$REPOS" ]] || { echo "usage: $0 --epic <n> --repos <a,b>" >&2; exit 2; }

MACHINE="${MACHINE:-azure-epic${EPIC}}"
RG="rg-coord-epic${EPIC}"

# --------------------------------------------------------------------------
# The Bicep module ships in the easy-azure repo; these scripts ship in
# code-coordinator. Keep EASY_AZURE_DIR in epic.env pointing at that checkout.
TEMPLATE="${EASY_AZURE_DIR:-$HOME/src/easy-azure}/modules/coord-worker-vm/main.bicep"
[[ -f "$TEMPLATE" ]] || { echo "template not found: $TEMPLATE (set EASY_AZURE_DIR in $EPIC_ENV)" >&2; exit 1; }

# A node already holding this hostname makes Tailscale assign "<name>-1" to the
# new VM, and the health poll below waits the full 15 minutes on a name that
# resolves to the STALE node. Catch it before paying for a VM. Most often a
# leftover from a previous run whose ephemeral self-removal did not fire.
if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
    if tailscale status 2>/dev/null | awk '{print $2}' | grep -qx "$MACHINE"; then
        echo "A tailnet node named '$MACHINE' already exists:" >&2
        tailscale status 2>/dev/null | awk -v m="$MACHINE" '$2==m' >&2
        echo >&2
        echo "Tailscale would name the new VM '${MACHINE}-1' and this script would" >&2
        echo "poll '$MACHINE' — the stale node — until it times out." >&2
        echo "Remove it at https://login.tailscale.com/admin/machines, then re-run." >&2
        exit 1
    fi
else
    echo "  note: tailscale unavailable locally — skipping the hostname collision check" >&2
fi

log "1/5  deploy $RG"
report_image_provenance "$SOURCE_IMAGE_ID"
az group create -n "$RG" -l "${LOCATION:-eastus}" -o none
az deployment group create -g "$RG" --name "worker-${EPIC}" \
    --template-file "$TEMPLATE" \
    --parameters \
        prefix=coord environment=prod name="epic${EPIC}" \
        owner="${OWNER}" costCenter="${COST_CENTER}" \
        machineName="$MACHINE" \
        sourceImageId="$SOURCE_IMAGE_ID" \
        identityResourceId="$IDENTITY_RESOURCE_ID" \
        identityClientId="$IDENTITY_CLIENT_ID" \
        keyVaultUri="$KEY_VAULT_URI" \
        keyVaultResourceId="$KEY_VAULT_RESOURCE_ID" \
        privateDnsZoneId="$PRIVATE_DNS_ZONE_ID" \
        sshPublicKey="$(cat "${SSH_PUBLIC_KEY_FILE:-$HOME/.ssh/id_ed25519.pub}")" \
        gitEmail="${GIT_EMAIL}" \
        vmSize="$VM_SIZE" \
    -o none

# From here on the VM exists and is costing money. Any failure must say so.
cleanup_hint() {
    local rc=$?
    (( rc == 0 )) || {
        echo >&2
        echo "  epic-up failed AFTER deploying $RG — the VM is running and billing." >&2
        echo "  Tear it down with:  ./epic-down.sh --epic $EPIC --force" >&2
    }
    return $rc
}
trap cleanup_hint EXIT

NAT_IP="$(az deployment group show -g "$RG" -n "worker-${EPIC}" \
    --query properties.outputs.natGatewayPublicIp.value -o tsv)"
echo "machine=$MACHINE  egress=$NAT_IP"

# --------------------------------------------------------------------------
log "2/5  wait for the agent (image boot + cloud-init, typically 2-4 min)"
# Poll the agent's own /health over the tailnet. It reports `active` (running
# assignment count) and `machine`, so a matching `machine` field is proof that
# cloud-init got all the way through install-agent.sh -- stronger than a ping.
deadline=$(( SECONDS + READY_TIMEOUT ))
until health="$(curl -fsS --max-time 5 "http://${MACHINE}:7433/health" 2>/dev/null)" \
      && [[ "$(jq -r .machine <<<"$health")" == "$MACHINE" ]]; do
    (( SECONDS < deadline )) || {
        echo >&2
        echo "agent never came up within ${READY_TIMEOUT}s." >&2
        # If the node landed under a different tailnet name, say so plainly --
        # otherwise this looks like a boot failure when it is a naming clash.
        actual="$(tailscale status 2>/dev/null | awk -v m="$MACHINE" '$2 ~ "^"m"-[0-9]+$" {print $2}' | head -1)"
        [[ -n "$actual" ]] && {
            echo "NOTE: a node joined as '$actual', not '$MACHINE' — a stale node holds the name." >&2
            echo "      Delete it in the Tailscale admin console, then re-run." >&2
        }
        # SSH also rides the tailnet, so it is useless when the join is what failed.
        # run-command goes via the Azure control plane and always works.
        echo "debug (works even with no tailnet):" >&2
        echo "  az vm run-command invoke -g $RG -n vm-coord-prod-${EPIC} --command-id RunShellScript \\" >&2
        echo "    --scripts 'cloud-init status --long; systemctl status coord-secrets --no-pager | tail -20; tailscale status'" >&2
        exit 1
    }
    printf '.'; sleep 10
done
echo
jq -r '"  version=\(.version // "?")  capabilities=\(.capabilities|join(","))  repos=\(.repos|join(","))"' <<<"$health"

# --------------------------------------------------------------------------
log "2b/5  detect capabilities actually present on $MACHINE"
if detect_opencode_capability "$MACHINE" "$ADMIN_USER"; then
    CAPABILITIES="$(add_capability_if_missing "$CAPABILITIES" "provider:opencode")"
    echo "  opencode CLI found on $MACHINE -- advertising provider:opencode"
else
    echo "  opencode CLI not found on $MACHINE -- not advertising provider:opencode" >&2
fi

# --------------------------------------------------------------------------
log "3/5  register in coordinator.yml on $DAEMON_HOST"
# The daemon host's ~/.coord/coordinator.yml is the real config. Editing the
# local copy would be pointless: on a thin client that file is a CACHE that is
# re-fetched from GET /config and overwritten wholesale.
REMOTE_HELPER="$(ssh "$DAEMON_HOST" 'mktemp /tmp/coordinator-machine.XXXXXX.py')"
trap 'ssh "$DAEMON_HOST" "rm -f $REMOTE_HELPER" 2>/dev/null || true' EXIT
scp -q "$HERE/coordinator-machine.py" "${DAEMON_HOST}:${REMOTE_HELPER}"

ssh "$DAEMON_HOST" bash -euo pipefail -s -- \
    "$MACHINE" "$CAPABILITIES" "$REPOS" "$MAX_WORKERS" "$REMOTE_HELPER" "$REPO_ROOT" <<'REMOTE'
MACHINE="$1"; CAPS="$2"; REPOS="$3"; MAXW="$4"; HELPER="$5"; REPO_ROOT="$6"
CFG="$HOME/.coord/coordinator.yml"
# #1887: ~/.coord/coordinator.yml is a symlink into the version-controlled
# coord-settings checkout on hosts that adopted #1832. `mv` replaces the
# SYMLINK itself, not its target, so writing straight to $CFG would silently
# leave the fleet running a disconnected, untracked regular file -- the
# exact failure #1832 exists to prevent. readlink -f is a no-op when $CFG is
# already a plain file, so this is safe on hosts without the symlink too.
CFG="$(readlink -f "$CFG")"
TMP="$(mktemp "${CFG}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

python3 "$HELPER" --file "$CFG" --out "$TMP" add \
    --name "$MACHINE" --host "$MACHINE" \
    --capabilities "$CAPS" --repos "$REPOS" --repo-root "$REPO_ROOT" --max-workers "$MAXW"

# Validate with coord's OWN parser, via the interpreter that runs coord.
#
# NOT `coord config --config` -- that goes through the client layer, which on a
# thin client silently ignores --config and reports the daemon's config instead
# (verified: it "succeeds" against a deliberately invalid file). Calling
# coord.config.load directly is the same code path the daemon uses.
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
"$PYBIN" - "$TMP" "$MACHINE" "$REPOS" <<'PYEOF'
import sys
from pathlib import Path
from coord.config import load
cfg = load(Path(sys.argv[1]))
names = [m.name for m in cfg.machines]
if sys.argv[2] not in names:
    sys.exit(f"validation failed: {sys.argv[2]} absent after edit. Parsed: {names}")
print(f"  validated: {len(names)} machines, including {sys.argv[2]}")

# #1799: epic-up.sh used to end with a "ready" banner for a machine that
# could not accept a single dispatch -- `coord.dispatch.dispatch` refuses
# with "No repo_path configured" before it ever reaches the provider/TOS
# gates. Reuse that exact precondition here (Machine.repo_path) instead of
# waiting for a real `coord assign` against a live issue to discover it --
# this is the repo_path half of what a `coord assign --dry-run` would have
# caught; the equivalent provider-capability half is exactly what step 2b/5
# (detect_opencode_capability) exists to get right before this point.
machine = next(m for m in cfg.machines if m.name == sys.argv[2])
repos = [r for r in sys.argv[3].split(",") if r]
missing = [r for r in repos if machine.repo_path(r) is None]
if missing:
    sys.exit(
        f"validation failed: {sys.argv[2]} has no repo_path for {missing} -- "
        f"coord assign would refuse with 'No repo_path configured'. "
        f"coordinator-machine.py should have derived these from --repos."
    )
print(f"  dispatchable: repo_path present for {repos}")
PYEOF

# Atomic. `coord serve` reloads on mtime change (#1081) -- no restart, and no
# need to drain sessions first. A rename means it never sees a torn file.
chmod --reference="$CFG" "$TMP"
mv "$TMP" "$CFG"
trap - EXIT
echo "  coordinator.yml updated"

# #1887: registering a machine is a CONTENT change inside coord-settings (when
# #1832's symlink applies) -- leaving that checkout dirty and unmentioned is
# its own trap: the next `git pull` there looks clean and quietly discards
# this edit. Print the exact command rather than committing on the
# operator's behalf (an operator mid-edit of coordinator.yml elsewhere in the
# same checkout should not have an unrelated commit made for them).
if git_root="$(git -C "$(dirname "$CFG")" rev-parse --show-toplevel 2>/dev/null)"; then
    echo "  NOTE: $CFG lives in $git_root -- commit the change there, e.g.:"
    echo "    git -C $git_root add $(realpath --relative-to="$git_root" "$CFG") && git -C $git_root commit -m 'coord: register $MACHINE'"
fi
REMOTE

# --------------------------------------------------------------------------
if (( PAUSED )); then
    log "3b/5  pause $MACHINE (registered, closed to new work)"
    ssh "$DAEMON_HOST" bash -euo pipefail -s -- "$MACHINE" <<'REMOTE'
MACHINE="$1"
COORD=""
for c in "${COORD_BIN:-}" "$HOME/.coord-venv/bin/coord" "$HOME/.local/bin/coord" "$(command -v coord 2>/dev/null)"; do
    [[ -n "$c" && -x "$c" ]] && { COORD="$c"; break; }
done
[[ -n "$COORD" ]] || { echo "coord not found on this host" >&2; exit 1; }
"$COORD" pause "$MACHINE"
REMOTE
    echo "  paused — run 'coord unpause $MACHINE' when you're satisfied it works"
fi

# --------------------------------------------------------------------------
log "4/5  confirm the daemon picked it up"
# The reload is mtime-triggered on the daemon's NEXT request, so a fixed sleep
# races it and warns spuriously. Poll instead -- each `coord status` is itself
# a request, so it drives the reload it is checking for.
for _i in $(seq 1 10); do
    if coord status --machine "$MACHINE" 2>/dev/null | grep -q "$MACHINE"; then
        echo "  daemon sees $MACHINE"
        break
    fi
    if (( _i == 10 )); then
        echo "  WARNING: daemon still does not list $MACHINE after ~30s." >&2
        echo "  A malformed config is swallowed by the daemon (last-good kept, warning logged)," >&2
        echo "  so check: ssh $DAEMON_HOST 'journalctl --user -u coord-serve -n 30'" >&2
    fi
    sleep 3
done

# --------------------------------------------------------------------------
log "5/5  ready"
cat <<EOF

  machine   $MACHINE
  repos     $REPOS
  caps      $CAPABILITIES
  egress IP $NAT_IP
  image     $(parse_image_id "$SOURCE_IMAGE_ID" version)
  teardown  ./epic-down.sh --epic $EPIC
  state     $( ((PAUSED)) && echo "PAUSED — no work will route here until 'coord unpause $MACHINE'" || echo "active — eligible for dispatch")

  Cost accrues until teardown -- roughly \$0.40/hr all-in at $VM_SIZE.

EOF
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
