#!/usr/bin/env bash
# Strip every identity from the builder VM, then hand it to waagent for
# deprovisioning. Run as the LAST thing on the builder, over SSH; it kills
# your own session at the end by design.
#
#   sudo ./scrub-and-generalize.sh
#
# The two scrubs that actually matter:
#
#   tailscaled.state -- baking it clones one node identity into every VM the
#   image produces. They fight over the same tailnet node, and `--ephemeral`
#   removal on shutdown deletes it out from under the others.
#
#   ~/.claude credentials -- the image is stored in a gallery and used to
#   stamp out VMs. Anything authenticated here is authenticated everywhere,
#   forever, with no rotation path short of a rebuild.
set -euo pipefail

COORD_USER="${COORD_USER:-coord}"
COORD_HOME="$(getent passwd "$COORD_USER" | cut -d: -f6)"
[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }
log() { printf '\n=== %s ===\n' "$*"; }

log "1/5  tailscale identity"
tailscale logout 2>/dev/null || true
systemctl stop tailscaled 2>/dev/null || true
rm -rf /var/lib/tailscale/*
systemctl disable tailscaled 2>/dev/null || true

log "2/5  credentials + per-machine coord state"
rm -rf "$COORD_HOME/.claude" "$COORD_HOME/.claude.json" \
       "$COORD_HOME/.config/gh" \
       "$COORD_HOME/.config/systemd/user/coord-agent.service" \
       "$COORD_HOME/.git-credentials" "$COORD_HOME/.netrc" \
       "$COORD_HOME/.ssh" \
       "$COORD_HOME/.bash_history" "$COORD_HOME/.python_history"
# ~/.coord holds coordinator.yml / client.toml / coord.db -- all per-machine.
# The venv (~/.coord-venv) is the expensive artifact and MUST survive.
rm -rf "$COORD_HOME/.coord"
rm -f  /root/.bash_history
find /home -maxdepth 2 -name '.bash_history' -delete 2>/dev/null || true

# The agent unit is written at boot by install-agent.sh --machine <name>;
# baking one hardcodes a machine name into every VM.
sudo -u "$COORD_USER" -H systemctl --user disable coord-agent 2>/dev/null || true

log "3/5  host identity"
rm -f /etc/ssh/ssh_host_*
truncate -s 0 /etc/machine-id           # truncate, do not delete: systemd needs the file
rm -f /var/lib/dbus/machine-id
cloud-init clean --logs --seed 2>/dev/null || true
rm -rf /var/lib/cloud/instances/*
rm -rf /var/log/journal/* /var/log/waagent.log

log "4/5  verify nothing identity-bearing survived"
leaked=0
for path in /var/lib/tailscale/tailscaled.state "$COORD_HOME/.claude/.credentials.json" \
            "$COORD_HOME/.config/gh/hosts.yml" /etc/ssh/ssh_host_rsa_key; do
    [[ -e "$path" ]] && { echo "  LEAK: $path still present"; leaked=1; }
done
[[ -s /etc/machine-id ]] && { echo "  LEAK: /etc/machine-id is non-empty"; leaked=1; }
# The venv is what makes this image worth building -- fail loudly if the scrub ate it.
[[ -x "$COORD_HOME/.coord-venv/bin/coord" ]] || { echo "  BROKEN: ~/.coord-venv destroyed"; leaked=1; }
[[ -d "$COORD_HOME/src/code-coordinator" ]] || { echo "  BROKEN: ~/src clones destroyed"; leaked=1; }
[[ $leaked -eq 0 ]] || { echo "SCRUB FAILED -- do not generalize" >&2; exit 1; }
echo "  clean"

log "5/5  waagent deprovision"
# +user removes the PROVISIONING user (azureuser) and its home. /home/coord is
# a separately-created account and is left alone -- that is the entire reason
# provision-worker.sh does not build as azureuser.
waagent -deprovision+user -force

cat <<'EOF'

Builder is deprovisioned. This SSH session is now on a dead machine.
From your workstation:

  az vm deallocate  -g <rg> -n <builder>
  az vm generalize  -g <rg> -n <builder>
  az sig image-version create ...   # see build-worker-image.sh

EOF
