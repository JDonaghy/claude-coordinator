# Shared helpers for the versioned hooks in this directory.  Sourced, not run.
#
# WHY THESE HOOKS EXIST AT ALL: setting `core.hooksPath` REPLACES `.git/hooks`
# wholesale — git stops looking there entirely.  graphify installs post-commit,
# post-checkout, and post-merge into `$GIT_COMMON_DIR/hooks/`, so every one of
# them needs a counterpart here or it is silently disabled.  (Shipping only
# post-checkout killed graphify's commit/merge rebuilds on the operator box for
# about an hour — caught by `coord diagnose --graph` reporting the repo's own
# graph STALE right after a merge.)
#
# Each hook here is a thin shim: skip in linked worktrees, otherwise hand off
# to the machine-local graphify hook, which pins an absolute interpreter path
# and therefore must never be committed.

# Absolute path, whether git handed us a relative one or not.
gfy_abs() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *)  printf '%s\n' "$PWD/$1" ;;
    esac
}

gfy_common_dir() {
    gfy_abs "$(git rev-parse --git-common-dir 2>/dev/null || echo .git)"
}

# True in a linked worktree (its per-worktree git dir differs from the common one).
gfy_is_linked_worktree() {
    _gd=$(gfy_abs "$(git rev-parse --git-dir 2>/dev/null || echo .git)")
    [ "$_gd" != "$(gfy_common_dir)" ]
}

# Hand off to the machine-local hook of the same name, if present.
gfy_chain() {
    _name=$1
    shift
    _local="$(gfy_common_dir)/hooks/$_name"
    if [ -x "$_local" ]; then
        exec "$_local" "$@"
    fi
    exit 0
}
