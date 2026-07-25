#!/usr/bin/env bash
#
# drive-issue.sh — drive ONE issue from dispatch to merge without a human or an
# agent watching it.
#
#   scripts/drive-issue.sh [options] <repo> <issue>
#
# The pipeline is Work → Test → Review → Merge (pipeline.default_gates).  coord
# already automates most of it: the `coord serve` tick loop reconciles and
# enqueues, and the `coord-notify.timer` (5 min, on the daemon host) posts
# completions, dispatches reviews, and runs the review → fix → re-review
# auto-loop.  Two things are missing, and this script supplies them:
#
#   1. THE TEST GATE HAS NO HEADLESS PRODUCER.  `dispatch_smoke` is disabled
#      (smoke_tests.auto_queue), is only called from `reconcile()` (which only
#      `coord resume` runs — the notify timer has no smoke counterpart), only
#      fires when a `capability_rules` prefix matches, and falls back to
#      `echo ... && false` for a repo with no `test_command`.  So headless work
#      lands `done` with `test_state=NULL` and, because
#      `default_gates: [test, review, merge]` holds review until a
#      passed/skipped verdict, the review never dispatches.  Observed live:
#      #1348/#1349 sat done/test=NULL/review=pending for 12.8 hours.
#      → The Test gate here runs scripts/coord-test-runner.sh in a scratch
#        worktree and records the verdict with `coord test --passed|--fail`.
#
#   2. NOTHING SEQUENCES THE STAGES FOR A SINGLE ISSUE.  `coord wait` is
#      per-assignment (and reads the LOCAL dispatched ledger, so it does not
#      work from a thin client at all).  → This script is a resumable state
#      machine over the daemon's board.
#
# A FAILING TEST IS A LOOP ITERATION, NOT A DEAD END.  On a genuine failure the
# gate records `--fail` (which mirrors to the legacy `smoke_test` field and
# stores the report where `coord fix` looks for it) and then runs `coord fix`,
# which dispatches a headless follow-up worker on the SAME branch with the
# model escalated and the failure quoted in its briefing.  The loop re-tests
# and repeats, bounded by --max-fix-rounds.
#
# Everywhere coord ALREADY has a path, this script observes rather than acts —
# in particular it never dispatches a REVIEW fix, because the notify timer's
# auto-loop already does, and two drivers racing is exactly the 2026-06-07
# duplicate-fix-worker incident (#476/#477).
#
# Every command it runs is a normal `coord` command.  Re-running it on the same
# issue is safe and resumes from wherever the board actually is.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_TOOL="$HERE/coord_issue_state.py"
TEST_RUNNER="$HERE/coord-test-runner.sh"

# ── defaults ─────────────────────────────────────────────────────────────────

MACHINE=""
MODEL=""
DO_PLAN=0
TEST_COMMAND=""
MAX_FIX_ROUNDS=3
SKIP_TEST=0
REPO_PATH=""
POLL=60
MAX_WORK_RETRIES=1
DEADLINE_MINS=240
STALL_MINS=20
DRIVE_NOTIFY=0
DO_MERGE=1
MERGE_METHOD="rebase"
DRY_RUN=0
BRIEFING_FILE=""
ACCEPT_ADVISORY=0
FORCE_REVIEW=0

usage() {
    # Print the header comment block: every line from #2 up to (not including)
    # the first line that is not a comment.
    awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "${BASH_SOURCE[0]}"
    cat <<'EOF'

Options:
  --machine NAME        Machine for the work dispatch (default: least-loaded
                        unpaused machine that hosts the repo).
  --model TIER          Model tier (haiku|sonnet|opus). Default: models.default.
  --briefing-file FILE  Extra briefing text for the work dispatch.
  --plan                Run a read-only plan stage first and auto-approve it
                        (coord assign --plan-only → coord approve-plan).
  --test-command CMD    Override the Test gate. By default the gate runs
                        scripts/coord-test-runner.sh, which path-routes
                        (coord/** → pytest, tui/** → cargo test), links the
                        quadraui path dep, and filters known flakes.
  --max-fix-rounds N    Headless `coord fix` rounds on a failing test suite
                        (default 3). Each round continues the SAME branch with
                        the model escalated.
  --skip-test           Record the Test gate as `skipped` instead of running
                        anything. Use only for genuinely untestable diffs.
  --repo-path PATH      Local checkout to build the scratch worktree from.
                        Default: ~/src/<repo>.
  --poll SECS           Board poll interval (default 60).
  --max-work-retries N  `coord retry` attempts on a failed work stage (default 1).
  --deadline MINS       Give up after this long (default 240).
  --stall MINS          Warn (and nudge, with --notify) after this long with no
                        state change (default 20).
  --notify              Also run `coord notify` under flock when stalled, to
                        cut the 5-minute timer latency. OFF by default: see the
                        "two drivers" warning above. To make this fully safe,
                        the systemd unit must take the same lock —
                        ExecStart=/usr/bin/flock ~/.coord/notify.lock %h/.coord-venv/bin/coord notify ...
  --accept-advisory     Proceed when the work row is ADVISORY but its branch
                        demonstrably carries commits. Needed while #1357 is
                        open: every Python-only headless assignment in this
                        repo is falsely downgraded DONE→ADVISORY.
  --force-review        Explicitly request the review for work completed in an
                        INTERACTIVE session. coord's #555 guard never
                        auto-dispatches one for provider=claude-pty, so without
                        this the run stops at preflight rather than stalling.
  --no-merge            Stop after the review approves; do not merge.
  --merge-method M      rebase|squash|merge (default rebase).
  --dry-run             Print the resolved plan and current state, then exit.
  -h, --help            This help.

Exit codes:
  0  merged (verified against the remote default branch)
  0  review approved, with --no-merge
  1  a stage reached a terminal failure a script cannot resolve
  2  bad usage / configuration
  3  deadline exceeded
EOF
}

log()  { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '%s  !! %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf '%s  ✗ %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit "${2:-1}"; }

# ── arg parsing ──────────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --machine)          MACHINE="$2"; shift 2 ;;
        --model)            MODEL="$2"; shift 2 ;;
        --briefing-file)    BRIEFING_FILE="$2"; shift 2 ;;
        --plan)             DO_PLAN=1; shift ;;
        --test-command)     TEST_COMMAND="$2"; shift 2 ;;
        --max-fix-rounds)   MAX_FIX_ROUNDS="$2"; shift 2 ;;
        --skip-test)        SKIP_TEST=1; shift ;;
        --repo-path)        REPO_PATH="$2"; shift 2 ;;
        --poll)             POLL="$2"; shift 2 ;;
        --max-work-retries) MAX_WORK_RETRIES="$2"; shift 2 ;;
        --deadline)         DEADLINE_MINS="$2"; shift 2 ;;
        --stall)            STALL_MINS="$2"; shift 2 ;;
        --notify)           DRIVE_NOTIFY=1; shift ;;
        --no-merge)         DO_MERGE=0; shift ;;
        --merge-method)     MERGE_METHOD="$2"; shift 2 ;;
        --accept-advisory)  ACCEPT_ADVISORY=1; shift ;;
        --force-review)     FORCE_REVIEW=1; shift ;;
        --dry-run)          DRY_RUN=1; shift ;;
        -h|--help)          usage; exit 0 ;;
        -*)                 die "unknown option: $1" 2 ;;
        *)                  break ;;
    esac
done

[[ $# -eq 2 ]] || { usage >&2; die "expected <repo> <issue>" 2; }
REPO="$1"
ISSUE="$2"
[[ "$ISSUE" =~ ^[0-9]+$ ]] || die "issue must be a number, got: $ISSUE" 2

command -v coord >/dev/null || die "coord not on PATH — activate the venv" 2
[[ -f "$STATE_TOOL" ]] || die "missing $STATE_TOOL" 2
[[ -x "$TEST_RUNNER" ]] || die "missing or non-executable $TEST_RUNNER" 2

SCRATCH="${TMPDIR:-/tmp}/coord-drive-issue-$(id -u)"
mkdir -p "$SCRATCH"
RUN_LOG="$SCRATCH/${REPO}-${ISSUE}.log"

# One issue at a time. Concurrent runs would race for the merge queue and can
# produce exactly the conflicting-branch pile-up this is meant to avoid; they
# would also fight over the shared CARGO_TARGET_DIR.
LOCK="$SCRATCH/drive-issue.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
    holder="$(cat "$SCRATCH/drive-issue.holder" 2>/dev/null || echo "another run")"
    die "already driving $holder — one issue at a time.
   Wait for it to finish, or override the lock file at $LOCK" 2
fi
printf '%s #%s (pid %s)\n' "$REPO" "$ISSUE" "$$" >"$SCRATCH/drive-issue.holder"

# Clean up the test worktree on ANY exit — including Ctrl-C and SIGTERM. A
# worktree left registered in the base checkout makes the NEXT run's `git
# worktree add` fail on an already-registered path (the #618 orphaned-worktree
# failure mode), so this is not merely tidiness.
CURRENT_WT=""
cleanup() {
    local rc=$?
    rm -f "$SCRATCH/drive-issue.holder"
    if [[ -n "$CURRENT_WT" ]]; then
        local base="${REPO_PATH:-$HOME/src/$REPO}"
        git -C "$base" worktree remove --force "$CURRENT_WT" 2>/dev/null || true
        git -C "$base" worktree prune 2>/dev/null || true
    fi
    exit $rc
}
trap cleanup EXIT INT TERM

# ── state helpers ────────────────────────────────────────────────────────────

# Populates the WORK_*/REVIEW_*/MERGE_*/REPO_* variables in this shell.
#
# stderr is captured to a FILE, never merged into stdout: the result is
# eval'd, so letting a diagnostic message reach stdout would eval it as shell.
refresh_state() {
    local out rc=0
    local err="$SCRATCH/${REPO}-${ISSUE}-state.err"
    out="$(python3 "$STATE_TOOL" "$REPO" "$ISSUE" 2>"$err")" || rc=$?
    if (( rc != 0 )); then
        warn "state read failed (rc=$rc): $(tail -n 2 "$err" 2>/dev/null)"
        return 1
    fi
    eval "$out"
    return 0
}

# A compact fingerprint of everything the state machine branches on. Used to
# detect a stall (no transition) as distinct from "still working".
state_fingerprint() {
    printf '%s|%s|%s|%s|%s|%s|%s|%s' \
        "${WORK_AID:-}" "${WORK_STATUS:-}" "${WORK_TEST_STATE:-}" \
        "${WORK_REVIEW_STATE:-}" "${WORK_REVIEW_ITER:-}" \
        "${REVIEW_STATUS:-}" "${REVIEW_VERDICT:-}" "${MERGE_STATUS:-}"
}

run_notify() {
    [[ "$DRIVE_NOTIFY" -eq 1 ]] || return 0
    log "nudging: coord notify (flock ~/.coord/notify.lock)"
    flock -w 300 "$HOME/.coord/notify.lock" coord notify >>"$RUN_LOG" 2>&1 || \
        warn "coord notify returned non-zero (see $RUN_LOG)"
}

# ── stage: work dispatch ─────────────────────────────────────────────────────

dispatch_plan() {
    local machine="$1"
    log "PLAN: coord assign --plan-only $machine $REPO $ISSUE"
    local args=(assign --plan-only "$machine" "$REPO" "$ISSUE")
    [[ -n "$MODEL" ]] && args+=(--model "$MODEL")
    coord "${args[@]}" 2>&1 | tee -a "$RUN_LOG"
}

approve_plan() {
    log "PLAN: approved → coord approve-plan $PLAN_AID"
    coord approve-plan "$PLAN_AID" 2>&1 | tee -a "$RUN_LOG"
}

dispatch_work() {
    local machine="$1"
    log "WORK: coord assign $machine $REPO $ISSUE"
    local args=(assign "$machine" "$REPO" "$ISSUE")
    [[ -n "$MODEL" ]] && args+=(--model "$MODEL")
    [[ -n "$BRIEFING_FILE" ]] && args+=(--briefing-file "$BRIEFING_FILE")
    coord "${args[@]}" 2>&1 | tee -a "$RUN_LOG"
}

# ── stage: test (script-owned) ───────────────────────────────────────────────

# Runs the branch's tests and records the Test-gate verdict.
#
# The worktree is built in scratch, NEVER under ~/.coord/worktrees — the
# daemon's hourly orphan sweep deletes worktrees with no live session and would
# pull this one out from under a running suite.
#
# Returns 0 when the gate is now passed/skipped, 1 when it recorded a failure.
run_test_gate() {
    local aid="$1" branch="$2"

    if [[ "$SKIP_TEST" -eq 1 ]]; then
        log "TEST: --skip-test → recording 'skipped'"
        coord test --skipped --reason "drive-issue.sh --skip-test" "$aid" \
            2>&1 | tee -a "$RUN_LOG"
        return 0
    fi

    local base="${REPO_PATH:-$HOME/src/$REPO}"
    [[ -d "$base/.git" ]] || die "not a git checkout: $base (pass --repo-path)"

    local wt_parent="$SCRATCH/wt/${REPO}-${ISSUE}"
    # The worktree must sit in its OWN directory, because coord-test-runner.sh
    # needs to drop a `quadraui` symlink as its SIBLING to satisfy
    # tui/Cargo.toml's `path = "../../quadraui/quadraui"`.
    local wt="$wt_parent/$REPO"
    rm -rf "$wt_parent"
    mkdir -p "$wt_parent"

    log "TEST: fetching $branch into $wt"
    git -C "$base" fetch --quiet origin "$branch" || die "could not fetch origin/$branch"
    # Drop any registration left by a previous run that was killed before its
    # cleanup ran — otherwise `worktree add` refuses the path as already used.
    git -C "$base" worktree prune 2>/dev/null || true
    # --detach so this never fights a checkout of the same branch elsewhere.
    git -C "$base" worktree add --quiet --detach "$wt" FETCH_HEAD \
        || die "git worktree add failed"
    CURRENT_WT="$wt"

    local out="$SCRATCH/${REPO}-${ISSUE}-test.out"
    local rc=0
    local runner_args=("$wt" --base-ref "origin/${REPO_DEFAULT_BRANCH:-main}" --report "$out")
    if [[ -n "$TEST_COMMAND" ]]; then
        # Escape hatch: run exactly what the caller asked for instead of the
        # path-routed suites.
        log "TEST: running override: $TEST_COMMAND"
        ( cd "$wt" && bash -lc "$TEST_COMMAND" ) >"$out" 2>&1 || rc=$?
    else
        "$TEST_RUNNER" "${runner_args[@]}" || rc=$?
    fi

    git -C "$base" worktree remove --force "$wt" 2>/dev/null || \
        warn "could not remove worktree $wt — remove it manually"
    CURRENT_WT=""

    # A docs-only diff has nothing to run; that is 'skipped', not 'passed'.
    if [[ $rc -eq 0 ]] && grep -q '^SKIP:' "$out" 2>/dev/null; then
        log "TEST: nothing to test → coord test --skipped $aid"
        coord test --skipped --reason "no test-bearing paths changed" "$aid" \
            2>&1 | tee -a "$RUN_LOG"
        return 0
    fi

    if [[ $rc -eq 0 ]]; then
        log "TEST: PASSED → coord test --passed $aid"
        coord test --passed "$aid" 2>&1 | tee -a "$RUN_LOG"
        return 0
    fi

    log "TEST: FAILED → coord test --fail $aid"
    # --output stores the report at ~/.coord/test_output/<aid>.txt, which is
    # exactly where `coord fix` reads it to build the fix worker's briefing.
    coord test --fail --reason "drive-issue.sh: test suite failed" \
        --output "$out" "$aid" 2>&1 | tee -a "$RUN_LOG"
    return 1
}

# The headless test-failure → fix path.
#
# `coord fix` gates on the assignment's legacy `smoke_test == "fail"` field —
# which `coord test --fail` mirrors from `test_state` — and dispatches a
# follow-up worker with `inherit_branch=True`, so the fix continues the SAME
# branch rather than orphaning it on a fresh one. It also escalates the model
# and quotes the stored test output in the briefing.
#
# This is why a test failure is a loop iteration and not a dead end. (The
# interactive `--fix-of` and `coord bounce` paths are NOT usable here:
# `--fix-of` requires --interactive, and `bounce` needs a request-changes
# REVIEW id, not a failed test.)
dispatch_test_fix() {
    local aid="$1"
    log "TEST: dispatching headless fix on the same branch (coord fix $aid)"
    if ! coord fix "$aid" 2>&1 | tee -a "$RUN_LOG"; then
        die "coord fix $aid failed to dispatch.
   Most likely the assignment's legacy smoke_test field is not 'fail' — that is
   what \`coord fix\` gates on, and only \`coord test --fail\` sets it.
   Check: coord log $aid   /   continue by hand: coord assign --interactive --fix-of $aid"
    fi
}

# ── stage: merge ─────────────────────────────────────────────────────────────

# True when *branch* exists on the remote and carries at least one commit the
# default branch does not. Used to tell a REAL zero-commit advisory apart from
# the #1357 false positive, where the agent downgrades a good DONE over an
# artifact glob that matched nothing.
branch_has_commits() {
    local branch="$1"
    local base="${REPO_PATH:-$HOME/src/$REPO}"
    local target="${REPO_DEFAULT_BRANCH:-main}"
    [[ -d "$base/.git" ]] || return 1
    git -C "$base" fetch --quiet origin "$target" 2>/dev/null || return 1
    git -C "$base" fetch --quiet origin "$branch" 2>/dev/null || return 1
    local n
    n="$(git -C "$base" rev-list --count "origin/${target}..FETCH_HEAD" 2>/dev/null || echo 0)"
    [[ "${n:-0}" -gt 0 ]]
}

# Confirm the branch actually landed on the target.
#
# NOTE: `merge-base --is-ancestor` is the WRONG test here. `coord merge`
# defaults to --method rebase (and supports squash), both of which rewrite the
# commits — so a fully-merged branch's tip SHA is never an ancestor of the
# target. Verified against #1344: merged via PR #1355, two commits on main,
# and `--is-ancestor` still says no.
verify_merged() {
    local branch="$1" target="$2"

    # Primary: ask GitHub. Authoritative for every merge method, and still
    # correct after the merged branch has been deleted from the remote.
    if [[ -n "${REPO_GITHUB:-}" ]] && command -v gh >/dev/null 2>&1; then
        local state
        state="$(gh pr view "$branch" --repo "$REPO_GITHUB" --json state -q .state 2>/dev/null || true)"
        if [[ "$state" == "MERGED" ]]; then
            return 0
        elif [[ -n "$state" ]]; then
            warn "PR for $branch is $state, not MERGED"
            return 1
        fi
    fi

    # Fallback: patch-equivalence. Every commit of a landed branch has an
    # equivalent upstream, which is exactly what `git cherry` marks with '-';
    # a '+' means that commit is genuinely not on the target yet.
    local base="${REPO_PATH:-$HOME/src/$REPO}"
    [[ -d "$base/.git" ]] || return 1
    local vref="refs/remotes/coord-verify/$branch"
    git -C "$base" fetch --quiet origin "$target" 2>/dev/null || return 1
    git -C "$base" fetch --quiet origin "refs/heads/$branch:$vref" 2>/dev/null || return 1
    local unmerged
    unmerged="$(git -C "$base" cherry "origin/$target" "coord-verify/$branch" 2>/dev/null | grep -c '^+' || true)"
    git -C "$base" update-ref -d "$vref" 2>/dev/null || true
    [[ "$unmerged" == "0" ]]
}

# Tolerant on purpose: the first attempt often lands before the daemon's tick
# has run `enqueue_approved_work`, so `--only <aid>` finds no queue entry. That
# is a "try again next poll", not a reason to abort the run — the attempt cap
# in the state machine is what bounds it.
do_merge() {
    local aid="$1"
    log "MERGE: coord merge --only $aid --method $MERGE_METHOD"
    if ! coord merge --only "$aid" --method "$MERGE_METHOD" 2>&1 | tee -a "$RUN_LOG"; then
        warn "coord merge returned non-zero — re-checking the board next poll"
    fi
}

# ── preflight ────────────────────────────────────────────────────────────────

refresh_state || die "could not read board state" 2

if [[ -z "$MACHINE" ]]; then
    MACHINE="${PICKED_MACHINE:-}"
    [[ -n "$MACHINE" ]] || die "no unpaused machine hosts $REPO — pass --machine" 2
fi

log "driving $REPO #$ISSUE"
log "  machine        : $MACHINE"
log "  test command   : ${TEST_COMMAND:-${REPO_TEST_COMMAND:-<none configured>}}"
log "  merge          : $([[ $DO_MERGE -eq 1 ]] && echo "yes ($MERGE_METHOD)" || echo no)"
log "  auto-loop      : $([[ "${AUTO_LOOP:-0}" == "1" ]] && echo "on (coord dispatches review fixes; this script observes)" || echo off)"
log "  test fix rounds: $MAX_FIX_ROUNDS (this script, via coord fix)"
log "  review fix cap : ${MAX_REVIEW_ITERATIONS:-?} (coord's auto-loop)"
log "  notify nudge   : $([[ $DRIVE_NOTIFY -eq 1 ]] && echo "on" || echo "off (relying on the 5-min coord-notify.timer)")"
log "  log            : $RUN_LOG"

if [[ "${AUTO_LOOP:-0}" != "1" ]]; then
    warn "pipeline.auto_loop is OFF — a request-changes review will NOT auto-dispatch a fix."
    warn "This script will report the verdict and stop rather than dispatch one itself."
fi

# INTERACTIVE WORK NEVER GETS AN AUTOMATIC REVIEW.
#
# `dispatch_pending_reviews` carries `and c.provider_name != "claude-pty"`
# (#555): a metered headless review must never silently follow a human-attended
# session. So for work done interactively, the review is not "late" — it is
# never coming, and waiting for it is an infinite stall.
#
# Checked HERE, at preflight, rather than at the review gate: otherwise a run
# burns the full test suite (~6 min) before parking on a wait it can never win.
# That is exactly what happened driving #1357 — test gate passed at 4642/4642,
# then 90 minutes of nothing.
if [[ -n "${WORK_AID:-}" && "${WORK_PROVIDER:-}" == "claude-pty" && -z "${REVIEW_AID:-}" ]]; then
    if [[ "$FORCE_REVIEW" -eq 1 ]]; then
        warn "work $WORK_AID is INTERACTIVE (claude-pty) — no automatic review (#555)."
        warn "--force-review set: this run will request the review explicitly."
    else
        die "work $WORK_AID was completed INTERACTIVELY (provider=claude-pty).
   coord's #555 guard permanently excludes interactive work from automatic
   review dispatch, so waiting for one would stall forever.

   Either drive it unattended:      re-run with --force-review
   or review it human-attended:     coord assign --interactive --review-of $WORK_AID"
    fi
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
    log "current state:"
    python3 "$STATE_TOOL" "$REPO" "$ISSUE" --json
    exit 0
fi

# ── the state machine ────────────────────────────────────────────────────────

START="$(date +%s)"
DEADLINE=$(( START + DEADLINE_MINS * 60 ))
STALL_SECS=$(( STALL_MINS * 60 ))
LAST_FP=""
LAST_CHANGE="$START"
WORK_RETRIES=0
NUDGED=0
MERGE_ATTEMPTS=0
REVIEW_DISPATCHES=0
MAX_MERGE_ATTEMPTS=3
FIX_ROUNDS=0

while true; do
    now="$(date +%s)"
    if (( now > DEADLINE )); then
        warn "deadline of ${DEADLINE_MINS}m exceeded"
        python3 "$STATE_TOOL" "$REPO" "$ISSUE" --json >&2
        exit 3
    fi

    if ! refresh_state; then
        sleep "$POLL"; continue
    fi

    fp="$(state_fingerprint)"
    if [[ "$fp" != "$LAST_FP" ]]; then
        LAST_FP="$fp"; LAST_CHANGE="$now"; NUDGED=0
        log "state: work=${WORK_STATUS:--} test=${WORK_TEST_STATE:--} review=${REVIEW_STATUS:--}/${REVIEW_VERDICT:--} iter=${WORK_REVIEW_ITER:-0} merge=${MERGE_STATUS:--} active=${ACTIVE_COUNT:-0}"
    elif (( now - LAST_CHANGE > STALL_SECS )); then
        if [[ "$NUDGED" -eq 0 ]]; then
            warn "no state change in ${STALL_MINS}m (${ACTIVE_TYPES:-nothing} active)"
            run_notify
            NUDGED=1
            LAST_CHANGE="$now"
        fi
    fi

    # ---- terminal: merged --------------------------------------------------
    if [[ "${WORK_STATUS:-}" == "merged" || "${MERGE_STATUS:-}" == "MERGED" ]]; then
        if [[ -n "${WORK_BRANCH:-}" ]] && verify_merged "$WORK_BRANCH" "${REPO_DEFAULT_BRANCH:-main}"; then
            log "✓ MERGED — $WORK_BRANCH has landed on ${REPO_DEFAULT_BRANCH:-main}"
            exit 0
        fi
        warn "board says merged but $WORK_BRANCH has NOT landed on ${REPO_DEFAULT_BRANCH:-main}"
        warn "verify by hand: git -C ${REPO_PATH:-$HOME/src/$REPO} log --oneline origin/${REPO_DEFAULT_BRANCH:-main}"
        exit 1
    fi

    # ---- something is running: just wait ------------------------------------
    if [[ "${ACTIVE_COUNT:-0}" -gt 0 ]]; then
        sleep "$POLL"; continue
    fi

    # ---- no work yet: plan and/or dispatch ---------------------------------
    if [[ -z "${WORK_AID:-}" ]]; then
        if [[ "$DO_PLAN" -eq 1 ]]; then
            if [[ -z "${PLAN_AID:-}" ]]; then
                dispatch_plan "$MACHINE"; sleep "$POLL"; continue
            fi
            case "${PLAN_STATUS:-}" in
                done)    approve_plan; sleep "$POLL"; continue ;;
                failed)  die "plan assignment $PLAN_AID failed — inspect: coord log $PLAN_AID --machine $MACHINE" ;;
                *)       sleep "$POLL"; continue ;;
            esac
        fi
        dispatch_work "$MACHINE"; sleep "$POLL"; continue
    fi

    # ---- work failed: bounded retry ----------------------------------------
    if [[ "${WORK_STATUS:-}" == "failed" ]]; then
        if (( WORK_RETRIES >= MAX_WORK_RETRIES )); then
            die "work $WORK_AID failed ${WORK_RETRIES} retr(ies) in: ${WORK_FAILURE_REASON:-no reason recorded}
   inspect: coord log $WORK_AID --machine ${WORK_MACHINE:-$MACHINE}"
        fi
        WORK_RETRIES=$(( WORK_RETRIES + 1 ))
        log "WORK: failed → coord retry $WORK_AID (attempt $WORK_RETRIES/$MAX_WORK_RETRIES)"
        coord retry "$WORK_AID" 2>&1 | tee -a "$RUN_LOG" || \
            die "coord retry failed for $WORK_AID"
        sleep "$POLL"; continue
    fi

    # ---- work reached a terminal state that is not 'done' ------------------
    #
    # Every status here is TERMINAL (a non-terminal row would have been caught
    # by the ACTIVE_COUNT wait above), so none of them may fall through to a
    # bare `sleep; continue` — that spins silently until the deadline instead
    # of reporting anything.
    case "${WORK_STATUS:-}" in
        done) ;;
        advisory)
            # #448 downgrade: the agent flagged a zero-commit / stash-miss exit.
            # #1357 makes this a FALSE POSITIVE for every Python-only headless
            # assignment in this repo — claude-coordinator's only artifact glob
            # is `tui/target/debug/coord-tui`, which a Python diff never
            # produces, so #1323's stash-miss check downgrades a perfectly good
            # DONE. Ask git which case this actually is rather than trusting the
            # status.
            if [[ -z "${WORK_BRANCH:-}" ]] || ! branch_has_commits "$WORK_BRANCH"; then
                die "work $WORK_AID exited ADVISORY with no commits on its branch —
   nothing was pushed, so there is nothing to test, review, or merge.
   inspect: coord log $WORK_AID --machine ${WORK_MACHINE:-$MACHINE}"
            fi
            if [[ "$ACCEPT_ADVISORY" -ne 1 ]]; then
                die "work $WORK_AID is ADVISORY, but its branch carries real commits.
   This is the #1357 signature: since v0.4.75 every Python-only headless
   assignment in this repo is downgraded DONE→ADVISORY by an artifact glob
   that a Python diff can never match.
   Proceed anyway with --accept-advisory (and fix #1357 to stop needing it)."
            fi
            warn "ADVISORY with commits present — proceeding per --accept-advisory (#1357)"
            ;;
        cancelled)
            die "work $WORK_AID was cancelled — re-dispatch with: coord assign $MACHINE $REPO $ISSUE --force"
            ;;
        *)
            die "unexpected terminal work status '${WORK_STATUS}' for $WORK_AID —
   refusing to guess. Inspect: coord log $WORK_AID --machine ${WORK_MACHINE:-$MACHINE}"
            ;;
    esac

    # A 'done' row with no branch never pushed anything either.
    if [[ -z "${WORK_BRANCH:-}" ]]; then
        die "work $WORK_AID finished with no branch — nothing was pushed (0-commit advisory).
   inspect: coord log $WORK_AID --machine ${WORK_MACHINE:-$MACHINE}"
    fi

    # ---- TEST gate ---------------------------------------------------------
    case "${WORK_TEST_STATE:-}" in
        "")
            if run_test_gate "$WORK_AID" "$WORK_BRANCH"; then
                sleep 5
            fi
            continue
            ;;
        failed)
            if (( FIX_ROUNDS >= MAX_FIX_ROUNDS )); then
                die "test still failing after $FIX_ROUNDS fix round(s) — stopping.
   Reason: ${WORK_TEST_REASON:-none recorded}
   Report: $SCRATCH/${REPO}-${ISSUE}-test.out
   Continue by hand: coord assign --interactive --fix-of $WORK_AID"
            fi
            FIX_ROUNDS=$(( FIX_ROUNDS + 1 ))
            log "TEST: failed → fix round $FIX_ROUNDS/$MAX_FIX_ROUNDS"
            dispatch_test_fix "$WORK_AID"
            sleep "$POLL"; continue
            ;;
        passed|skipped) ;;  # fall through
        *) warn "unexpected test_state '${WORK_TEST_STATE}'"; sleep "$POLL"; continue ;;
    esac

    # ---- REVIEW gate -------------------------------------------------------
    # coord dispatches the review itself once the test verdict lands (the
    # notify timer's dispatch_pending_reviews).  We only observe.
    case "${REVIEW_VERDICT:-}" in
        approve) ;;  # fall through to merge
        request-changes)
            if (( ${WORK_REVIEW_ITER:-0} >= ${MAX_REVIEW_ITERATIONS:-5} )); then
                die "review requested changes and the fix loop is exhausted
   (${WORK_REVIEW_ITER} rounds, cap ${MAX_REVIEW_ITERATIONS}).
   Findings: coord log $REVIEW_AID
   Continue by hand: coord assign --interactive --fix-of $REVIEW_AID"
            fi
            # The auto-loop dispatches the fix; wait for it to appear.
            sleep "$POLL"; continue
            ;;
        "")
            if [[ "${WORK_REVIEW_STATE:-}" == "done" ]]; then
                die "review $REVIEW_AID finished but recorded NO verdict — the
   REVIEW_VERDICT block failed to parse (#1346/#1348 class).
   Recover: coord post-pending-reviews, or read the transcript directly."
            fi
            # No review row at all. For interactive work that is terminal, not
            # transient (#555) — request one explicitly, once, rather than
            # waiting on a dispatch that will never happen. Preflight already
            # refused this case unless --force-review was given.
            if [[ -z "${REVIEW_AID:-}" && "${WORK_PROVIDER:-}" == "claude-pty" ]]; then
                if [[ "$FORCE_REVIEW" -ne 1 ]]; then
                    die "no review for interactive work $WORK_AID and --force-review not set (#555)."
                fi
                if (( REVIEW_DISPATCHES >= 1 )); then
                    die "requested a review for $WORK_AID but none appeared on the board.
   Check for an eligible reviewer machine: coord status"
                fi
                REVIEW_DISPATCHES=$(( REVIEW_DISPATCHES + 1 ))
                log "REVIEW: requesting explicitly (interactive work, #555)"
                coord review "$WORK_AID" 2>&1 | tee -a "$RUN_LOG" || \
                    die "explicit review dispatch failed for $WORK_AID"
            fi
            sleep "$POLL"; continue
            ;;
        *) warn "unexpected review verdict '${REVIEW_VERDICT}'"; sleep "$POLL"; continue ;;
    esac

    # ---- MERGE -------------------------------------------------------------
    if [[ "$DO_MERGE" -eq 0 ]]; then
        log "✓ review approved — stopping here (--no-merge)"
        log "  merge with: coord merge --only $WORK_AID"
        exit 0
    fi

    case "${MERGE_STATUS:-}" in
        HUMAN_REQUIRED|human_required)
            die "merge entry is HUMAN_REQUIRED: ${MERGE_REASON:-no reason recorded}
   An automated conflict-fix already gave up. Resolve by hand, or override:
     coord merge --only ${MERGE_AID:-$WORK_AID} --override-human-required '<reason>'"
            ;;
        CONFLICT|conflict)
            # coord auto-dispatches a conflict-fix worker and re-enqueues on
            # success; give it room rather than fighting it.
            log "MERGE: conflict — waiting for coord's conflict-fix worker"
            sleep "$POLL"; continue
            ;;
        BLOCKED)
            log "MERGE: blocked — ${MERGE_REASON:-gate not satisfied}; re-checking"
            sleep "$POLL"; continue
            ;;
        *)
            # Cap the attempts: without this, a merge that fails for a reason
            # the board never reflects (so MERGE_STATUS stays empty) would
            # re-run `coord merge` on every poll until the deadline.
            if (( MERGE_ATTEMPTS >= MAX_MERGE_ATTEMPTS )); then
                die "merge attempted $MERGE_ATTEMPTS times without landing.
   Last board state: status='${MERGE_STATUS:-none}' reason='${MERGE_REASON:-none}'
   Inspect the gates: coord merge --plan --repo $REPO"
            fi
            MERGE_ATTEMPTS=$(( MERGE_ATTEMPTS + 1 ))
            log "MERGE: attempt $MERGE_ATTEMPTS/$MAX_MERGE_ATTEMPTS"
            do_merge "${MERGE_AID:-$WORK_AID}"
            sleep "$POLL"; continue
            ;;
    esac
done
