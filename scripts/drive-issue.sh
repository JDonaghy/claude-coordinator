#!/usr/bin/env bash
#
# drive-issue.sh — drive ONE issue from dispatch to merge without a human or an
# agent watching it.
#
#   scripts/drive-issue.sh [options] <repo> <issue>
#
# The pipeline is Work → Test → Review → Merge (pipeline.default_gates).  coord
# now automates all of it (#1426): the `coord serve` tick loop reconciles and
# enqueues, and the `coord-notify.timer` (5 min, on the daemon host) posts
# completions, auto-dispatches the Test-stage smoke assignment
# (`dispatch_pending_smoke`), dispatches reviews, and runs the review → fix →
# re-review auto-loop.  One thing is still missing, and this script supplies
# it:
#
#   NOTHING SEQUENCES THE STAGES FOR A SINGLE ISSUE.  `coord wait` is
#   per-assignment (and reads the LOCAL dispatched ledger, so it does not
#   work from a thin client at all).  → This script is a resumable state
#   machine over the daemon's board: it dispatches the WORK assignment, then
#   OBSERVES Test/Review/Merge — coord dispatches all three itself — nudging
#   `coord notify` (--notify) when nothing has changed for --stall minutes.
#
# #1426: this script used to run scripts/coord-test-runner.sh ITSELF, in a
# scratch worktree, and record the verdict with `coord test --passed|--fail`.
# That was a workaround for two bugs, both now fixed at the source:
#   - `dispatch_smoke` silently no-op'd for almost every repo/diff: it
#     required a `capability_rules` prefix match even for a plain `type=work`
#     completion with a configured `test_command`, so only `tui/`-ish rules
#     ever fired. It now dispatches for any `type="work"` completion with a
#     real command configured, matched rule or not — a capability-rule miss
#     just means "no EXTRA hardware required", not "skip".
#   - `dispatch_smoke` was only ever called from `reconcile()`'s per-item
#     loop, and the only sanctioned caller of `reconcile()` is the
#     human-invoked `coord resume` — so a thin-client setup driven purely by
#     `coord-notify.timer` never ran the Test stage at all. `notify.run()`
#     now calls `dispatch_pending_smoke` too, the same shape as review
#     dispatch.
# With both fixed, the Test stage is a normal dispatched, board-visible,
# capability-routed assignment — same as Review and Merge — so this script
# only needs to OBSERVE it, exactly like it already does for Review and
# Merge. `--skip-test` remains as an explicit override for a genuinely
# untestable diff; it records `skipped` directly, no dispatch involved.
#
# A FAILING TEST IS A LOOP ITERATION, NOT A DEAD END.  On a genuine test
# failure this script runs `coord fix` — a coordinator command, not a local
# subprocess — which dispatches a headless follow-up worker on the SAME
# branch with the model escalated and the failure quoted in its briefing.
# The loop re-tests and repeats, bounded by --max-fix-rounds.
#
# Everywhere coord ALREADY has a path, this script observes rather than acts —
# in particular it never dispatches the Test-stage smoke assignment (coord's
# own `dispatch_pending_smoke` does) or a REVIEW fix (the notify timer's
# auto-loop already does) — two drivers racing to dispatch the same thing is
# exactly the 2026-06-07 duplicate-fix-worker incident (#476/#477).
#
# Every command it runs is a normal `coord` command.  Re-running it on the same
# issue is safe and resumes from wherever the board actually is.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_TOOL="$HERE/coord_issue_state.py"

# ── defaults ─────────────────────────────────────────────────────────────────

MACHINE=""
MODEL=""
DO_PLAN=0
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
  --briefing-file FILE  REPLACES the entire auto-generated briefing for the
                        work dispatch — it is NOT an addendum. The issue
                        body, project rules and file scope are all dropped,
                        so the worker only ever sees this file. To ADD
                        guidance while keeping the real briefing, use
                        `coord context add --pin <repo> <issue> '<note>'`,
                        which prepends to the top of every briefing (#603).
  --plan                Run a read-only plan stage first and auto-approve it
                        (coord assign --plan-only → coord approve-plan).
  --max-fix-rounds N    Headless `coord fix` rounds on a failing test suite
                        (default 3). Each round continues the SAME branch with
                        the model escalated. Unaffected by WHERE the Test
                        stage runs (#1426): coord dispatches it itself, onto
                        a capability-matched machine, via
                        scripts/coord-test-runner.sh (or the repo's
                        configured `test_command`) — see coordinator.yml's
                        `smoke_tests`. This script only observes the verdict.
  --skip-test           Record the Test gate as `skipped` directly — no
                        dispatch, no subprocess. Use only for genuinely
                        untestable diffs.
  --repo-path PATH      Local checkout used for branch/merge verification
                        (git fetch + rev-list against origin). Default:
                        ~/src/<repo>.
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

SCRATCH="${TMPDIR:-/tmp}/coord-drive-issue-$(id -u)"
mkdir -p "$SCRATCH"
RUN_LOG="$SCRATCH/${REPO}-${ISSUE}.log"

# PER-ISSUE lock. Two drivers on DIFFERENT issues are fine and now allowed;
# two drivers on the SAME issue are not — they would double-dispatch work,
# double-record verdicts, and fight over one scratch worktree.
#
# Everything a run touches is already per-issue (worktree, run log, test
# report, state stderr). The three things that were genuinely shared are
# handled explicitly: the board cache is now written atomically as one file
# (coord_issue_state.py), merges are serialized on their own lock below, and
# CARGO_TARGET_DIR is left shared on purpose — cargo takes its own build lock,
# so a second run blocks briefly instead of corrupting, and shares the cache.
LOCK="$SCRATCH/lock-${REPO}-${ISSUE}"
HOLDER="$SCRATCH/holder-${REPO}-${ISSUE}"
exec 9>"$LOCK"
if ! flock -n 9; then
    holder="$(cat "$HOLDER" 2>/dev/null || echo "another run")"
    die "already driving $REPO #$ISSUE ($holder).
   A second driver on the SAME issue would double-dispatch work and corrupt
   its worktree. Other issues can be driven concurrently.
   Lock file: $LOCK" 2
fi
printf '%s #%s (pid %s)\n' "$REPO" "$ISSUE" "$$" >"$HOLDER"

# Merges are serialized on THIS HOST even when the runs themselves are
# parallel. $SCRATCH (and so this lock) is per-machine — it does NOT serialize
# a driver here against a driver on another host. That used to matter a lot:
# the daemon's POST /merge handler installed a process-global `redirect_stdout`
# to capture output, so concurrent merge requests from *any* two hosts could
# swap each other's buffers (a documented incident where a --dry-run reported
# a merge that another request had actually performed). #1400 fixed that at
# the source — the daemon now serializes the whole `/merge` critical section
# behind an internal lock, so overlapping requests from different hosts
# genuinely queue there and never cross-talk. This local flock is now
# belt-and-braces for same-host callers rather than the only thing preventing
# fleet-wide cross-talk; it still earns its keep by keeping this host's own
# queue submissions ordered (two branches rebasing onto a moving main at once
# is how pile-ups start) and by failing fast/locally instead of piling up
# blocked requests on the daemon.
# The lock is held ONLY around `coord merge`, so work/test/review still overlap
# freely across issues — the expensive stages stay parallel.
MERGE_LOCK="$SCRATCH/merge.lock"

# Release the per-issue lock on ANY exit — including Ctrl-C and SIGTERM.
#
# #1426: this script no longer builds its own scratch worktree for the Test
# stage (coord dispatches that itself, see the header comment), so there is
# no worktree left here to clean up on exit — the #618 orphaned-worktree
# failure mode this trap used to guard against lived entirely in that local
# test run.
cleanup() {
    local rc=$?
    rm -f "$HOLDER"
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

# ── stage: test (coord-dispatched, #1426 — this script only observes) ───────

# `--skip-test` is the one Test-stage action this script still takes itself:
# an explicit human override for a genuinely untestable diff. It is a direct
# `coord test --skipped` call, not a subprocess — everything else (dispatch,
# running the suite, recording passed/failed) is coord's own
# `dispatch_smoke`/`dispatch_pending_smoke`, triggered by `coord serve`'s tick
# loop or `coord notify` (see the header comment). The main loop below just
# polls `WORK_TEST_STATE` and waits.
record_test_skip() {
    local aid="$1"
    log "TEST: --skip-test → recording 'skipped'"
    coord test --skipped --reason "drive-issue.sh --skip-test" "$aid" \
        2>&1 | tee -a "$RUN_LOG"
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
    log "MERGE: coord merge --only $aid --method $MERGE_METHOD (waiting for merge lock)"
    # flock serializes this against every other driver; -w bounds the wait so a
    # wedged peer cannot hang this run forever.
    if ! flock -w 1800 "$MERGE_LOCK" \
            coord merge --only "$aid" --method "$MERGE_METHOD" 2>&1 | tee -a "$RUN_LOG"; then
        warn "coord merge returned non-zero (or the merge lock timed out) — re-checking next poll"
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
log "  test command   : ${REPO_TEST_COMMAND:-<none configured>} (coord dispatches this itself — #1426; this script observes)"
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
    # #1426: coord dispatches this stage itself now (dispatch_smoke via the
    # `coord serve` tick loop or `coord notify` — see the header comment) onto
    # a capability-matched machine; this script only observes
    # `WORK_TEST_STATE`, exactly like it already does for REVIEW below.
    case "${WORK_TEST_STATE:-}" in
        "")
            if [[ "$SKIP_TEST" -eq 1 ]]; then
                record_test_skip "$WORK_AID"
                sleep 5; continue
            fi
            # Waiting for coord to dispatch the Test stage itself
            # (dispatch_smoke / dispatch_pending_smoke). The stall detector
            # above already nudges `coord notify` (--notify) after
            # --stall minutes of no state change — no need to force it here
            # on every poll.
            sleep "$POLL"; continue
            ;;
        running)
            log "TEST: in progress on a capability-matched machine"
            sleep "$POLL"; continue
            ;;
        failed)
            if (( FIX_ROUNDS >= MAX_FIX_ROUNDS )); then
                die "test still failing after $FIX_ROUNDS fix round(s) — stopping.
   Reason: ${WORK_TEST_REASON:-none recorded}
   Inspect: coord log $WORK_AID --machine ${WORK_MACHINE:-$MACHINE}
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
