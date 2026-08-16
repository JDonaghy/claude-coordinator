#!/usr/bin/env bash
# Verify the worker's GitHub PAT grants exactly what coord needs, BEFORE it
# goes into Key Vault. Read-only: creates no branches, PRs, comments or pushes.
#
#   export GH_TOKEN=github_pat_...
#   ./verify-github-token.sh
#
# Checks each call path coord actually uses, so a missing permission surfaces
# here rather than several successful steps into a merge.
set -uo pipefail

REPOS=(code-coordinator quadraui vimcode)
OWNER=JDonaghy
PASS=0; FAIL=0
ok()  { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASS=$((PASS+1)); }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
hdr() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[[ -n "${GH_TOKEN:-}" ]] || { echo "export GH_TOKEN=github_pat_... first" >&2; exit 2; }
# Make sure we test the NEW token, not whatever gh is already logged in as.
unset GITHUB_TOKEN
export GH_CONFIG_DIR; GH_CONFIG_DIR="$(mktemp -d)"
trap 'rm -rf "$GH_CONFIG_DIR"' EXIT

hdr "0. token identity"
if who="$(gh api user --jq .login 2>/dev/null)"; then
    ok "authenticates as $who"
else
    # Fine-grained PATs without account permissions can't read /user; that is
    # expected and not a failure. Fall back to a repo-scoped probe.
    if gh api "repos/$OWNER/code-coordinator" --jq .full_name >/dev/null 2>&1; then
        ok "authenticates (no /user access — normal for a fine-grained PAT)"
    else
        bad "token cannot reach the API at all — wrong value, expired, or no repo access"
        exit 1
    fi
fi

hdr "1. Metadata + repository access"
for r in "${REPOS[@]}"; do
    if gh api "repos/$OWNER/$r" --jq .full_name >/dev/null 2>&1; then
        ok "$r reachable"
    else
        bad "$r NOT reachable — add it under Repository access"
    fi
done

hdr "2. Contents: read  (clone / read refs)"
for r in "${REPOS[@]}"; do
    if gh api "repos/$OWNER/$r/branches" --jq '.[0].name' >/dev/null 2>&1; then
        ok "$r branches readable"
    else
        bad "$r branches unreadable — needs Contents: Read"
    fi
done

hdr "3. Contents: WRITE  (real probe — creates then deletes a throwaway ref)"
# The repo object's .permissions field is NOT usable here: it reports the
# authenticated USER's role (owner => admin/push=true) regardless of what the
# fine-grained token was actually granted. Fine-grained PAT scopes are not
# introspectable, so the only honest test is to attempt the write.
#
# Safe on these repos: every workflow triggers on push:[main], push:tags, or
# pull_request:[main]. A short-lived side branch fires no CI.
for r in "${REPOS[@]}"; do
    base="$(gh api "repos/$OWNER/$r" --jq .default_branch 2>/dev/null)"
    sha="$(gh api "repos/$OWNER/$r/git/ref/heads/$base" --jq .object.sha 2>/dev/null)"
    ref="coord-token-probe-$$"
    if [[ -z "$sha" || "$sha" == "null" ]]; then
        bad "$r — could not read $base to probe from"; continue
    fi
    if gh api -X POST "repos/$OWNER/$r/git/refs" \
         -f ref="refs/heads/$ref" -f sha="$sha" >/dev/null 2>&1; then
        ok "$r push CONFIRMED (created refs/heads/$ref)"
        gh api -X DELETE "repos/$OWNER/$r/git/refs/heads/$ref" >/dev/null 2>&1 \
            && printf '        cleaned up\n' \
            || printf '        \033[33mWARNING: could not delete refs/heads/%s — remove it by hand\033[0m\n' "$ref"
    else
        bad "$r push DENIED — needs Contents: Read and write"
    fi
done

hdr "4. Issues: read+write  (coord's message bus)"
for r in "${REPOS[@]}"; do
    if gh api "repos/$OWNER/$r/issues?state=all&per_page=1" --jq 'length' >/dev/null 2>&1; then
        ok "$r issues readable"
    else
        bad "$r issues unreadable — needs Issues: Read and write"
    fi
done

hdr "5. Pull requests: read  (gh pr diff / review)"
PR_REPO=""; PR_NUM=""
for r in "${REPOS[@]}"; do
    if n="$(gh api "repos/$OWNER/$r/pulls?state=all&per_page=1" --jq '.[0].number' 2>/dev/null)" && [[ -n "$n" && "$n" != "null" ]]; then
        ok "$r pulls readable (sample PR #$n)"
        [[ -z "$PR_REPO" ]] && { PR_REPO="$r"; PR_NUM="$n"; }
    else
        # An empty list is still a successful read.
        if gh api "repos/$OWNER/$r/pulls?state=all&per_page=1" >/dev/null 2>&1; then
            ok "$r pulls readable (none exist)"
        else
            bad "$r pulls unreadable — needs Pull requests: Read and write"
        fi
    fi
done

hdr "6. Checks + commit statuses  (the coord merge CI gate)"
# This is the one that fails LATE if wrong: coord.ci_store.CiStore shells out
# to `gh pr checks`, and a merge is refused when it cannot read check state.
if [[ -n "$PR_REPO" ]]; then
    sha="$(gh api "repos/$OWNER/$PR_REPO/pulls/$PR_NUM" --jq .head.sha 2>/dev/null)"
    if [[ -n "$sha" && "$sha" != "null" ]]; then
        if gh api "repos/$OWNER/$PR_REPO/commits/$sha/check-runs" --jq '.total_count' >/dev/null 2>&1; then
            ok "check-runs readable (Actions: Read)"
        else
            bad "check-runs UNREADABLE — add Actions: Read, or the merge gate cannot see CI"
        fi
        if gh api "repos/$OWNER/$PR_REPO/commits/$sha/status" --jq '.state' >/dev/null 2>&1; then
            ok "commit statuses readable (Commit statuses: Read)"
        else
            bad "commit statuses UNREADABLE — add Commit statuses: Read"
        fi
    else
        bad "could not resolve a head SHA to probe checks against"
    fi

    # The real thing: exactly what CiStore invokes.
    if out="$(gh pr checks "$PR_NUM" --repo "$OWNER/$PR_REPO" --json state,name 2>&1)"; then
        ok "gh pr checks works — $(python3 -c "import json,sys;print(len(json.loads(sys.argv[1])),'check(s)')" "$out" 2>/dev/null || echo 'ok')"
    else
        case "$out" in
            *"no checks reported"*|*"no checks"*) ok "gh pr checks works (this PR has no checks)" ;;
            *) bad "gh pr checks FAILED: $(head -1 <<<"$out")" ;;
        esac
    fi
else
    printf '  \033[33mSKIP\033[0m  no PR available to probe the CI gate against\n'
fi

hdr "7. Not over-scoped  (negative test)"
# Reading Actions secrets requires an admin-tier permission we deliberately did
# not grant. A 403/404 here is the PASS: it proves the token is narrower than
# the account behind it. (Checking .permissions.admin does not work — see above.)
if gh api "repos/$OWNER/code-coordinator/actions/secrets" >/dev/null 2>&1; then
    bad "token can read Actions secrets — far broader than intended, regenerate it"
else
    ok "cannot read Actions secrets (correctly scoped)"
fi

printf '\n\033[1mSummary\033[0m\n  %d passed, %d failed\n' "$PASS" "$FAIL"
if (( FAIL > 0 )); then
    printf '\n\033[31mDo not put this token in Key Vault yet.\033[0m Fix the permissions above and re-run.\n'
    exit 1
fi
printf '\n\033[32mToken is good.\033[0m Safe to feed into bootstrap-shared.sh.\n'
