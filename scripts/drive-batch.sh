#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# drive-batch.sh — sequential unattended drive of INDEPENDENT issues.
#
#   ssh dellserver
#   tmux new -s drive
#   ./drive-batch.sh 1645 1650 1654
#   <Ctrl-b d>   … go to bed
#
# Morning:  tmux attach -t drive     (or: tail -100 ~/drive-*.log)
#
# NOTE ON FAILURE POLICY: these issues are independent, so a failure does NOT
# stop the batch — the remaining issues still run and the failure is recorded
# in the summary. For a DEPENDENT chain (each issue needs the previous one
# MERGED, because the next worker branches off main) set STOP_ON_FAIL=1.
#
# `coord drive-queue` (#1750, docs/DRIVE_QUEUE.md) IS THE NEWER TOOL for most
# of what this script does, and is usually the better default: the queue is
# durable across a crash/reboot (state lives in the board, not a bash loop's
# stack), reorderable and enqueueable live from the TUI or CLI while it runs,
# and drained by a systemd timer instead of a tmux session someone has to
# remember to keep alive. Reach for THIS script instead when you want a
# one-off, strictly-sequential, foreground run you intend to babysit in tmux
# tonight — it has no install step and nothing to leave configured afterward.
# Not yet a full replacement: `coord drive-queue` has no equivalent of
# STOP_ON_FAIL's dependent-chain semantics beyond `--after`, and per #1715 a
# queue longer than ~2 issues on one repo is not yet unattended-safe (see
# docs/DRIVE_QUEUE.md's top section) — this script's per-issue independence
# and single failure summary can still be the safer choice for a long batch
# until that lands.
# ─────────────────────────────────────────────────────────────────────────────

# -e is deliberately NOT set: the loop must survive a failed issue.
set -uo pipefail

# Linux / bash 4+ only. `declare -A`, `date -Is` and `flock` are all
# bash-4/GNU-coreutils/util-linux; macOS ships bash 3.2 and BSD date, and has
# no flock(1) — so on the Mac mini (docs/MAC_MINI.md) every one of those fails,
# and the flock loss silently removes the single-instance guard. Fail loudly
# here rather than halfway through an unattended night.
if (( BASH_VERSINFO[0] < 4 )); then
  echo "FATAL: needs bash 4+ (found $BASH_VERSION). This script is Linux-only;" >&2
  echo "       on macOS: brew install bash util-linux coreutils, or drive from a Linux host." >&2
  exit 2
fi
for _need in flock git python3; do
  command -v "$_need" >/dev/null || { echo "FATAL: '$_need' not found on PATH" >&2; exit 2; }
done

# ── Config ───────────────────────────────────────────────────────────────────
# Resolved to an absolute path, never left to PATH: a non-interactive shell or
# a systemd unit does not source your rc. Prefer the agent venv because that is
# the "pinned, non-editable runner CLI" #1523 asks for — on an agent host an
# editable install would let worker branch churn rewrite the runner's own code
# mid-run.
if [[ -z "${COORD:-}" ]]; then
  for _c in "$HOME/.coord-venv/bin/coord" "$HOME/.coord-cli-venv/bin/coord"; do
    [[ -x "$_c" ]] && COORD="$_c" && break
  done
  COORD="${COORD:-$(command -v coord || true)}"
fi

# Default repo for bare issue numbers. Individual issues may override it with
# `repo#issue` (see below), so a batch can span repos.
REPO="${REPO:-claude-coordinator}"

# No default: dispatching a whole night to the wrong box is expensive, and the
# right box differs per operator. Must be set explicitly.
MACHINE="${MACHINE:-}"

# Where each repo is checked out. Workers create worktrees from here, so it
# must track the repo being driven — NOT a fixed path (that silently
# fast-forwarded and reported the wrong repo when REPO was overridden).
SRC_ROOT="${SRC_ROOT:-$HOME/src}"

# Per-issue wall-clock cap (minutes). 3 issues x 120m = 6h worst case.
#
# 120, not 90, and never 45. MEASURED on the 2026-08-01 batch: a clean
# single-round issue in this repo on sonnet is ~85m end-to-end
# (work ~40m + test ~23m + review ~22m); a review fix round adds roughly
# another cycle. That run used 45 and every one of the five issues was
# abandoned seconds after its work stage landed.
#
# READ THIS BEFORE LOWERING IT. An expired deadline does NOT stop the work —
# it only stops the observer. All five drives that night exited, and the
# fleet carried all five through test and review anyway, approving each one
# 21-133 minutes AFTER its drive was already gone. The loop then started the
# next issue on top of the previous one's still-running test/review stages,
# so the batch was sequential in this script and overlapped on the fleet
# (the smoke stage even landed on elitebook, which was supposed to be
# asleep). A short deadline does not save time; it silently converts a
# sequential run into a concurrent one. See #1660.
DEADLINE="${DEADLINE:-120}"

STOP_ON_FAIL="${STOP_ON_FAIL:-0}"
DRY_RUN="${DRY_RUN:-0}"

# Wait this long before the FIRST dispatch — "start it now, begin at 22:00".
# Accepts `90s`, `30m`, `6h`, or a bare number of seconds; `--delay` overrides.
#
# The wait happens AFTER preflight, not before. Preflight is where a wrong
# MACHINE, a missing checkout or an issue that is not ready gets caught, and
# those must surface while you are still at the keyboard — discovering them
# six hours later is the whole failure this script exists to avoid.
DELAY="${DELAY:-0}"

# Issues come from argv, each `ISSUE` or `REPO#ISSUE`:
#
#   ./drive-batch.sh 1645 1650 1654
#   ./drive-batch.sh 1645 vimcode#611 quadraui#514
#
# Deliberately NOT a hardcoded default. The previous list
# (1527 1624 1658 1633 1353) was left in the file after that batch completed,
# and re-running it would have re-dispatched five already-merged issues.
# A stale list is worse than no list.
#
# Keep it to ~3: at the measured ~85m/issue plus fix rounds, three is a full
# night. Order them to spread file surfaces — two issues touching the same
# module (e.g. #1353 and #1624 both touch merge code) should not be adjacent,
# so the second one's branch does not age against the first one's merge.
usage() {
  cat <<'USAGE'
usage: MACHINE=<name> drive-batch.sh [--delay DURATION] ISSUE|REPO#ISSUE ...
       drive-batch.sh --help

  Drives each issue to completion, one at a time, in the order given.
  A bare ISSUE uses $REPO (default: claude-coordinator).

  --delay DURATION  wait before the FIRST dispatch. `90s`, `30m`, `6h`, or a
                    bare number of seconds. Preflight still runs immediately,
                    so a bad MACHINE or an unready issue fails now rather than
                    after the wait. The single-instance lock is held for the
                    whole delay, so a delayed batch blocks a second one.
                    Ignored under DRY_RUN=1 (reported, not slept).

  env: MACHINE      target machine (REQUIRED — no default)
       DELAY        same as --delay (the flag wins)
       DEADLINE     minutes per issue (default 120; see the note in-file
                    before lowering it — a short deadline does not save
                    time, it makes the run concurrent)
       REPO         default repo for bare issue numbers
       SRC_ROOT     checkout parent dir (default ~/src)
       STOP_ON_FAIL=1  stop after the first failure. Use for a DEPENDENT
                    chain, where each issue needs the previous one MERGED.
       DRY_RUN=1    preflight only, dispatch nothing

  example:  MACHINE=dellserver DEADLINE=120 ./drive-batch.sh 1645 1650 1654
            MACHINE=dellserver ./drive-batch.sh --delay 6h 1645 1650 1654
USAGE
}

# Parse `90s` / `30m` / `6h` / bare-seconds into seconds on stdout.
#
# Strict on purpose: an unparseable duration is FATAL, never silently 0. A
# typo'd --delay that quietly became "no delay" would dispatch a whole batch
# hours early, onto a fleet the operator believed was idle.
parse_duration() {
  local raw="$1" num unit
  if [[ "$raw" =~ ^([0-9]+)([smh]?)$ ]]; then
    num="${BASH_REMATCH[1]}"; unit="${BASH_REMATCH[2]}"
  else
    echo "FATAL: --delay '$raw' is not a duration (use 90s, 30m, 6h, or seconds)" >&2
    return 1
  fi
  case "$unit" in
    h)    echo $(( num * 3600 )) ;;
    m)    echo $(( num * 60 )) ;;
    s|'') echo "$num" ;;
  esac
}

# --help before every other check: it must work with no MACHINE, no coord
# install, and no checkout. Goes to stdout (it was asked for); the error
# paths below send the same text to stderr.
for arg in "$@"; do
  case "$arg" in
    -h|--help|help) usage; exit 0 ;;
  esac
done

# Strip flags out of the positional list; whatever survives is issues.
# Validated here — before the MACHINE/COORD checks — so a malformed --delay
# is reported on its own terms rather than behind an unrelated error.
declare -a POSITIONAL=()
while (( $# )); do
  case "$1" in
    --delay)   [[ $# -ge 2 ]] || { echo "FATAL: --delay needs a value" >&2; exit 64; }
               DELAY="$2"; shift 2 ;;
    --delay=*) DELAY="${1#--delay=}"; shift ;;
    --)        shift; POSITIONAL+=("$@"); break ;;
    -*)        echo "FATAL: unknown option '$1'" >&2; usage >&2; exit 64 ;;
    *)         POSITIONAL+=("$1"); shift ;;
  esac
done
set -- ${POSITIONAL[@]+"${POSITIONAL[@]}"}

DELAY_SECS=$(parse_duration "$DELAY") || exit 64

(( $# )) || { usage >&2; exit 64; }
if [[ -z "$MACHINE" ]]; then
  echo "FATAL: MACHINE is not set — refusing to guess which box to drive." >&2
  usage >&2; exit 64
fi
if [[ -z "$COORD" || ! -x "$COORD" ]]; then
  echo "FATAL: no usable coord binary (tried \$COORD, ~/.coord-venv, ~/.coord-cli-venv, PATH)" >&2
  exit 2
fi

# Split each arg into a parallel (repo, issue) pair. Bash has no nested
# arrays; two arrays indexed together is the idiomatic stand-in.
declare -a ISSUE_REPOS=() ISSUE_NUMS=()
for arg in "$@"; do
  if [[ "$arg" == *"#"* ]]; then
    r="${arg%%#*}"; n="${arg##*#}"
  else
    r="$REPO";     n="$arg"
  fi
  if [[ ! "$n" =~ ^[0-9]+$ ]]; then
    echo "FATAL: '$arg' is not ISSUE or REPO#ISSUE" >&2; usage >&2; exit 64
  fi
  ISSUE_REPOS+=("$r"); ISSUE_NUMS+=("$n")
done
ISSUES=("$@")   # display form, for the banner and the summary

LOG="${LOG:-$HOME/drive-batch-$(date +%Y%m%d-%H%M).log}"
# Stable path for "what happened last night?" — the timestamped file is kept so
# earlier runs aren't clobbered; this just always points at the newest.
LOG_LATEST="$HOME/drive-batch-latest.log"
LOCK="$HOME/.drive-batch.lock"

# ── Helpers ──────────────────────────────────────────────────────────────────
# Where a pipe IS used (the drive call below, so tmux shows live progress),
# the exit status must come from PIPESTATUS[0] — `$?` after a pipe is tee's
# status, not coord's, which silently defeats every failure check here (#1523
# hit exactly this in the bash sequencer).
say() { echo "[$(date -Is)] $*" | tee -a "$LOG" >&2; }
log() { echo "[$(date -Is)] $*" >> "$LOG"; }

hms() { printf '%dh%02dm%02ds' $(($1/3600)) $(($1%3600/60)) $(($1%60)); }

# Count live drive sessions, or 0 if that cannot be determined.
#
# Read the declared machine contract, not the prose. `coord drive-sessions
# --json` emits an array of {repo, issue, session_name, attached} and is
# already consumed by coord-tui (`coord/commands/drive.py:394-398`). The
# human output is NOT parseable: `grep -c .` counts the literal "No live
# drive sessions." as one session — that false positive fired on the
# 2026-08-01 run — and each real row is followed by two "attach with:" /
# "stop with:" continuation lines. #1523 forbids CLI-prose parsing on a
# control path; a --json contract one flag away leaves no excuse.
#
# Degrades to 0 (not a spurious warning) on any older coord without --json.
# NOTE: the failure path must ASSIGN 0, never `|| echo 0` — under `pipefail`
# a failing `$COORD` makes the whole pipeline non-zero even though python
# already printed its own 0, so appending produced "0\n0" and the caller's
# `(( live > 0 ))` died with a syntax error. That silently disabled this
# warning in exactly the degraded case the paragraph above promises to
# handle. The regex guard catches any other non-numeric surprise.
live_drive_sessions() {
  local out
  out=$("$COORD" drive-sessions --json 2>/dev/null | python3 -c '
import json, sys
try:
    print(len(json.load(sys.stdin)))
except Exception:
    print(0)
' 2>/dev/null) || out=0
  [[ "$out" =~ ^[0-9]+$ ]] || out=0
  echo "$out"
}

# ── Single-instance guard ────────────────────────────────────────────────────
# Stops a second invocation stacking on the first (the ad-hoc sequencer once
# ran 4 concurrent drives against a cap of 2 and burned the 5h window from
# 64% -> 87% in 50 minutes).
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another drive-batch is already running (lock: $LOCK) — refusing to stack" >&2
  exit 1
fi

ln -sfn "$LOG" "$LOG_LATEST"

declare -a DONE=() FAILED=() SKIPPED=()

say "=== drive-batch starting ==="
say "log:      $LOG  (also: $LOG_LATEST)"
say "issues:   ${ISSUES[*]}"
say "machine:  $MACHINE   deadline: ${DEADLINE}m/issue   stop_on_fail: $STOP_ON_FAIL"
(( DELAY_SECS > 0 )) && say "delay:    $(hms "$DELAY_SECS") after preflight, before the first dispatch"

# ── Preflight ────────────────────────────────────────────────────────────────
say "coord:    $COORD ($("$COORD" --version 2>&1 | tail -1))"

# The branch to bring this checkout up to date with — NOT assumed to be `main`.
#
# The checkout's own upstream is the authority, and `origin/HEAD` is NOT a
# usable substitute: on this fleet vimcode's `origin/HEAD` says `main` while
# the checkout tracks `origin/develop`, so trusting origin/HEAD merges the
# wrong branch into a develop checkout (caught exactly this way in testing).
# quadraui is develop-tracking too. Only fall back when there is no upstream.
#
# Two known imprecisions, both tolerable ONLY because this is advisory (see
# the caller — nothing here gates dispatch):
#   - it is the upstream of whatever branch the checkout is currently ON, so a
#     base checkout parked on a feature branch resolves to that branch. The
#     caller skips the fast-forward in that case rather than advancing it.
#   - coord itself resolves the default branch from `coordinator.yml`'s
#     repos.<name>.default_branch, a different source that could disagree.
#     Reading the config would be exact; it is not worth a YAML parse in bash
#     for a line that only prints a WARNING.
# Also assumes the remote is named `origin`.
default_branch() {
  local co="$1" up ref
  up=$(git -C "$co" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null) \
    && [[ -n "$up" ]] && { echo "${up#origin/}"; return; }
  ref=$(git -C "$co" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null) \
    && { echo "${ref#refs/remotes/origin/}"; return; }
  for b in main master develop; do
    git -C "$co" rev-parse --verify --quiet "origin/$b" >/dev/null && { echo "$b"; return; }
  done
  echo main
}

# Report each distinct repo's checkout, and opportunistically fast-forward it.
#
# ADVISORY ONLY — nothing here may abort the batch. The worker does NOT branch
# from this checkout's HEAD: both worktree paths (`AgentServer._setup_worktree`
# and `setup_interactive_worktree`) `git fetch origin --prune` and branch from
# the resolved `origin/<default_branch>` SHA (`coord/agent.py:1821`, #255),
# precisely so local checkout state cannot ride into a worker. `coord drive`
# fetches for its own verification too.
#
# So a stray local commit, or a base checkout left parked on a feature branch
# — both recurring here — must NOT take the night down before a single
# dispatch. This used to `exit 2`. The fast-forward is a courtesy to whoever
# opens the checkout in the morning, nothing more.
declare -A SEEN_REPO=()
for r in "${ISSUE_REPOS[@]}"; do
  [[ -n "${SEEN_REPO[$r]:-}" ]] && continue
  SEEN_REPO[$r]=1
  co="$SRC_ROOT/$r"
  if [[ ! -d "$co/.git" ]]; then
    say "WARNING: $co is not a git checkout — worktree creation for $r may fail"
    continue
  fi
  db=$(default_branch "$co")
  cur=$(git -C "$co" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
  git -C "$co" fetch --quiet origin 2>>"$LOG" || say "$r: WARNING: fetch failed (offline?)"
  behind=$(git -C "$co" rev-list --count "HEAD..origin/$db" 2>/dev/null || echo "?")
  if [[ "$cur" != "$db" ]]; then
    say "$r: on '$cur', not '$db' — NOT fast-forwarding (advisory only; workers branch from origin)"
  elif [[ "$behind" != "0" && "$behind" != "?" ]]; then
    if git -C "$co" merge --ff-only "origin/$db" >>"$LOG" 2>&1; then
      say "$r: fast-forwarded $behind commit(s) to origin/$db"
    else
      say "$r: WARNING: $behind behind origin/$db and could not fast-forward (local changes?) — continuing; workers branch from origin anyway"
    fi
  fi
  say "$r: $(git -C "$co" log --oneline -1)  [$cur]"
done

# A live drive from an earlier run would contend for the same machine.
live=$(live_drive_sessions)
(( live > 0 )) && say "WARNING: $live live drive session(s) already exist — check before continuing"

# Validate every issue before dispatching any of them: config, machine, repo
# path and issue state, with nothing actually dispatched.
say "--- preflight dry-run ---"
declare -a RUN_IDX=()
for i in "${!ISSUE_NUMS[@]}"; do
  r="${ISSUE_REPOS[$i]}"; n="${ISSUE_NUMS[$i]}"
  if "$COORD" drive "$r" "$n" --machine "$MACHINE" --dry-run >>"$LOG" 2>&1; then
    say "  $r#$n  ok"
    RUN_IDX+=("$i")
  else
    say "  $r#$n  PREFLIGHT FAILED — skipping (see $LOG)"
    SKIPPED+=("$r#$n")
  fi
done
if (( ${#RUN_IDX[@]} == 0 )); then
  say "FATAL: every issue failed preflight — nothing to drive"; exit 2
fi

if [[ "$DRY_RUN" == "1" ]]; then
  (( DELAY_SECS > 0 )) && say "DRY_RUN=1 — would have delayed $(hms "$DELAY_SECS") here"
  say "DRY_RUN=1 — stopping before the real loop"; exit 0
fi

# ── Delay ────────────────────────────────────────────────────────────────────
# Deliberately AFTER preflight: a wrong MACHINE, a missing checkout or an
# issue that is not ready must fail while the operator is still watching, not
# six hours later against an empty terminal.
#
# The flock is already held (taken above), so a delayed batch blocks a second
# invocation for its whole wait. That is the intended reading — a scheduled
# run is still a run — but it does mean `--delay 6h` reserves the runner.
if (( DELAY_SECS > 0 )); then
  say "delaying $(hms "$DELAY_SECS") — first dispatch at ~$(date -Is -d "+$DELAY_SECS seconds")"
  sleep "$DELAY_SECS"
  say "delay elapsed — starting the batch"

  # Preflight's live-session check is now hours stale, and the hazard it
  # guards (a concurrent drive contending for the same machine) is exactly
  # what a long delay invites. Re-check. Advisory, like the original.
  live=$(live_drive_sessions)
  (( live > 0 )) && say "WARNING: $live live drive session(s) started during the delay — check before continuing"
fi

# ── Main loop ────────────────────────────────────────────────────────────────
batch_start=$SECONDS

for pos in "${!RUN_IDX[@]}"; do
  i="${RUN_IDX[$pos]}"
  r="${ISSUE_REPOS[$i]}"; n="${ISSUE_NUMS[$i]}"
  say "──────── $r#$n starting ────────"
  issue_start=$SECONDS

  # Blocking, foreground, one at a time. Do NOT add --tmux: that detaches each
  # drive and returns immediately, so they would all run concurrently.
  # Merges happen inside drive via `coord merge --only <aid>` — scoped to this
  # one assignment and still gated on CI + approved review + test verdict.
  # Streamed to BOTH the terminal (so an attached tmux shows live progress)
  # and the log. `| tee` would normally make $? the exit status of tee rather
  # than coord — the trap that silently defeated the #1523 sequencer's failure
  # checks — so read coord's own status out of PIPESTATUS[0] explicitly.
  # Do NOT replace this with a bare `rc=$?`.
  "$COORD" drive "$r" "$n" \
      --machine "$MACHINE" \
      --notify \
      --deadline "$DEADLINE" \
      2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}

  elapsed=$(( SECONDS - issue_start ))

  # rc=3 is coord drive's EXIT_DEADLINE (`coord/drive.py:118`). It means the
  # OBSERVER gave up; `coord drive` just returns, so the work is STILL LIVE on
  # the fleet — worker, test and review all keep running.
  #
  # Starting the next issue here is the 2026-08-01 incident exactly: five
  # drives expired, the fleet approved all five 21-133 min later, and the loop
  # had already stacked the next issue on top of each one. Sequential in this
  # script, concurrent on the fleet.
  #
  # So a deadline STOPS THE BATCH unconditionally — it is not a "failure" that
  # STOP_ON_FAIL governs, it is an unknown state with live work attached, and
  # the one thing we must not do is dispatch on top of it. Same "hold, don't
  # kill" posture #1660 requires: the running work is left completely alone.
  if [[ $rc -eq 3 ]]; then
    say "──────── $r#$n DEADLINE after $(hms $elapsed) ────────"
    say "  work is STILL RUNNING on the fleet — not starting the next issue."
    say "  check it with:  $COORD status   /   $COORD merge --dry-run"
    FAILED+=("$r#$n")
    for later in "${RUN_IDX[@]:$((pos + 1))}"; do
      SKIPPED+=("${ISSUE_REPOS[$later]}#${ISSUE_NUMS[$later]}")
    done
    break
  fi

  if [[ $rc -eq 0 ]]; then
    say "──────── $r#$n DONE in $(hms $elapsed) ────────"
    DONE+=("$r#$n")
  else
    say "──────── $r#$n FAILED (exit $rc) after $(hms $elapsed) ────────"
    FAILED+=("$r#$n")
    if [[ "$STOP_ON_FAIL" == "1" ]]; then
      say "STOP_ON_FAIL=1 — abandoning the rest"
      for later in "${RUN_IDX[@]:$((pos + 1))}"; do
        SKIPPED+=("${ISSUE_REPOS[$later]}#${ISSUE_NUMS[$later]}")
      done
      break
    fi
  fi
done

# ── Summary ──────────────────────────────────────────────────────────────────
say "════════ batch finished in $(hms $((SECONDS - batch_start))) ════════"
say "  done:    ${DONE[*]:-none}"
say "  failed:  ${FAILED[*]:-none}"
say "  skipped: ${SKIPPED[*]:-none}"
say ""
say "morning checks:"
say "  $COORD status"
say "  $COORD merge --dry-run     # read the SUMMARY line: a parked entry shows"
say "                             # only in the count (conflict=N), not as a row"
say "  tail -200 $LOG_LATEST"
say "  grep -E '════|────────|FAILED|DONE' $LOG_LATEST   # just the milestones"

# Exit non-zero if ANYTHING did not complete — a skipped issue (preflight
# failure, or abandoned after a deadline/STOP_ON_FAIL) is not a success, and
# an unattended runner has only this code to go on. 2 of 3 issues skipped plus
# 1 success used to exit 0.
(( ${#FAILED[@]} == 0 && ${#SKIPPED[@]} == 0 ))
