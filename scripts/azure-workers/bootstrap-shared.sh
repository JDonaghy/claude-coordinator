#!/usr/bin/env bash
# ONE-TIME setup of the long-lived resources every epic worker shares:
# Key Vault, the managed identity that reads it, the private DNS zone, and the
# three secrets. Run once; epic-up.sh then needs no privileged setup at all.
#
#   ./bootstrap-shared.sh --rg rg-coord-shared --vault kv-coord-prod
#
# Idempotent — safe to re-run to rotate secrets or repair a missing piece.
set -euo pipefail

RG=""; VAULT=""; LOCATION="eastus"; IDENTITY="id-coord-worker"
DNS_ZONE="privatelink.vaultcore.azure.net"
# Purge protection is the secure default, but it is IRREVERSIBLE: the vault
# cannot be fully deleted and its (globally unique) name is reserved for the
# retention period. Worth disabling only while still shaking the setup out.
PURGE_PROTECTION=1

while [[ $# -gt 0 ]]; do
    case $1 in
        --rg)       RG="$2"; shift 2 ;;
        --vault)    VAULT="$2"; shift 2 ;;
        --location) LOCATION="$2"; shift 2 ;;
        --identity) IDENTITY="$2"; shift 2 ;;
        --no-purge-protection) PURGE_PROTECTION=0; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ -n "$RG" && -n "$VAULT" ]] || { echo "usage: $0 --rg <rg> --vault <vault-name>" >&2; exit 2; }

log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
SUB="$(az account show --query id -o tsv)"
MY_IP="$(curl -fsS https://api.ipify.org)"

log "0/6  target"
az account show --query '{sub:name, id:id}' -o tsv
echo "vault=$VAULT  rg=$RG  region=$LOCATION"

# --------------------------------------------------------------------------
log "1/6  resource group"
az group create -n "$RG" -l "$LOCATION" -o none

# --------------------------------------------------------------------------
log "2/6  key vault"
# Chicken-and-egg: with public access fully Disabled you could not write the
# secrets from here either. So: default-deny firewall with YOUR IP allowlisted
# for management, and workers reach it over the private endpoint that
# coord-worker-vm creates per epic. The vault is not open to the internet.
if ! az keyvault show -n "$VAULT" -g "$RG" -o none 2>/dev/null; then
    PP_ARGS=()
    if (( PURGE_PROTECTION )); then
        PP_ARGS=(--enable-purge-protection true)
        echo "purge protection ON — this vault name is reserved for 90 days even if deleted"
    else
        echo "purge protection OFF — vault is fully deletable (re-create with it on for production)"
    fi
    az keyvault create -n "$VAULT" -g "$RG" -l "$LOCATION" \
        --enable-rbac-authorization true \
        "${PP_ARGS[@]}" \
        --retention-days 90 \
        --public-network-access Enabled \
        --default-action Deny \
        --bypass AzureServices -o none
fi
az keyvault network-rule add -n "$VAULT" -g "$RG" --ip-address "$MY_IP" -o none 2>/dev/null || true
echo "firewall allows $MY_IP for management; workers use the private endpoint"

VAULT_ID="$(az keyvault show -n "$VAULT" -g "$RG" --query id -o tsv)"
VAULT_URI="$(az keyvault show -n "$VAULT" -g "$RG" --query properties.vaultUri -o tsv)"

# You need data-plane rights to write the secrets below. RBAC, not access
# policies — this vault uses --enable-rbac-authorization.
ME="$(az ad signed-in-user show --query id -o tsv)"
az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
    --role "Key Vault Secrets Officer" --scope "$VAULT_ID" -o none 2>/dev/null || true

# --------------------------------------------------------------------------
log "3/6  user-assigned managed identity"
# Deliberately user-assigned: a system-assigned identity would mint a new
# principal and require a new role assignment for EVERY ephemeral VM. This one
# is created once, granted once, and attached to every worker.
az identity create -n "$IDENTITY" -g "$RG" -l "$LOCATION" -o none 2>/dev/null || true
ID_RESOURCE_ID="$(az identity show -n "$IDENTITY" -g "$RG" --query id -o tsv)"
ID_CLIENT_ID="$(az identity show -n "$IDENTITY" -g "$RG" --query clientId -o tsv)"
ID_PRINCIPAL_ID="$(az identity show -n "$IDENTITY" -g "$RG" --query principalId -o tsv)"

log "4/6  grant identity read access to the vault"
az role assignment create \
    --assignee-object-id "$ID_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
    --role "Key Vault Secrets User" --scope "$VAULT_ID" -o none 2>/dev/null || true
echo "Key Vault Secrets User granted (read-only — the worker cannot write secrets)"

# --------------------------------------------------------------------------
log "5/6  private DNS zone"
az network private-dns zone create -g "$RG" -n "$DNS_ZONE" -o none 2>/dev/null || true
DNS_ZONE_ID="$(az network private-dns zone show -g "$RG" -n "$DNS_ZONE" --query id -o tsv)"
# Per-epic VNet links are created by the module (dns-link.bicep) and deleted
# with the epic's resource group.

# --------------------------------------------------------------------------
log "6/6  secrets"
# Prompted, never taken as argv — a CLI arg lands in shell history and in the
# process table where any local user can read it.
set_secret() { # set_secret <name> <prompt>
    local name="$1" prompt="$2" current value
    current="$(az keyvault secret show --vault-name "$VAULT" -n "$name" --query id -o tsv 2>/dev/null || true)"
    if [[ -n "$current" ]]; then
        read -r -p "  $name already set. Replace? [y/N] " ans
        [[ "${ans,,}" == "y" ]] || { echo "  keeping existing $name"; return; }
    fi
    read -r -s -p "  $prompt: " value; echo
    [[ -n "$value" ]] || { echo "  empty — skipped"; return; }
    az keyvault secret set --vault-name "$VAULT" -n "$name" --value "$value" -o none
    echo "  $name stored"
}

echo "Four secrets. Leave any blank to skip."
set_secret anthropic-api-key      "Anthropic API key (sk-ant-...)"
set_secret github-token           "GitHub fine-grained PAT (repo contents+PR write)"
set_secret tailscale-oauth-secret "Tailscale OAuth client secret (auth_keys scope, tag:coord-worker)"
# Verified non-interactive: opencode reads OPENCODE_API_KEY straight from the
# process environment for the "opencode" (Zen) provider -- no auth.json
# needed, confirmed against the real 1.18.11 binary (docs/OPENCODE_VERIFICATION.md).
# Only needed if this epic dispatches opencode workers -- leave blank otherwise.
# NOTE (#1777 hand-off): this secret lands in Key Vault, but nothing on the
# worker side pulls it into the environment yet -- coord-secrets (cloud-init,
# easy-azure repo) fetches only the three secrets above today. Extending it
# to also export OPENCODE_API_KEY is out of scope here (easy-azure is not in
# this repo's tree); see docs/EPHEMERAL_WORKERS.md for the open hand-off.
set_secret opencode-api-key       "OpenCode Zen API key (OPENCODE_API_KEY, opencode.ai/zen)"

# --------------------------------------------------------------------------
log "done — put these in epic.env"
cat <<EOF

SHARED_RG=$RG
KEY_VAULT_NAME=$VAULT
KEY_VAULT_URI=$VAULT_URI
KEY_VAULT_RESOURCE_ID=$VAULT_ID
IDENTITY_RESOURCE_ID=$ID_RESOURCE_ID
IDENTITY_CLIENT_ID=$ID_CLIENT_ID
PRIVATE_DNS_ZONE_ID=$DNS_ZONE_ID
SUBSCRIPTION_ID=$SUB

Note: the vault firewall allows $MY_IP. If your home IP changes, re-run this
script (or 'az keyvault network-rule add') or secret management will 403 --
worker boots are unaffected, they go via the private endpoint.
EOF
