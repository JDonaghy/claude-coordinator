# Operating gotchas

Traps that cost a real dispatch, real money, or real lost work. None were
visible from reading the code — every one was found by driving issues through
the pipeline and then diagnosing why something silently did the wrong thing.

Read this before operating the fleet, and before assuming a merged fix is live.

---

## 1. A merged fix is not a live fix

Which deploy step applies is decided by **the file the fix touched**:

| touched | reaches production via |
|---|---|
| `coord/agent.py` (worker prompts, reap, worktree handling) | PyPI release (`vX.Y.Z` tag) **+ `coord agent update --all`** |
| `coord/serve_app.py`, daemon-side reconcile/tick | **restart `coord-serve`** on the daemon host |
| other `coord/**` | live immediately on the coordinator's editable install |
| `tui/**` | **local `cargo build` + copy to `~/.local/bin/coord-tui`** |
| `.githooks/**` | live on the **next `git fetch`**, everywhere `core.hooksPath` is set — no release, no restart (the opposite failure mode; see [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md#the-fifth-surface-githooks--the-opposite-failure-mode)) |
| `deploy/**` (systemd unit files) | **nothing — there is no deploy step.** Manual `cp` to `~/.config/systemd/user/` + `systemctl --user restart` per affected host; see §15 below and [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md#the-sixth-surface-deploy--reviewed-like-code-installed-by-nobody) |

**Exception: dellserver.** The "editable install" row assumes a dev checkout at
`~/src/claude-coordinator`. dellserver's `coord-serve` *and* its `coord` CLI both
run from the same pinned, non-editable `~/.coord-venv` (deliberately — #1418, to
close the editable-drift hazard). So on dellserver, "other `coord/**`" fixes ride
the *same* release + restart path as the daemon row, not an instant one. Verified
against #1491's milestone-#50 exit gate — see
[`MERGE_AUTO_DRAIN_TRUST_BAR.md`](MERGE_AUTO_DRAIN_TRUST_BAR.md) for the full
deploy-status audit this produced.

Not academic. #1394 (worker strands uncommitted work, then cleanup destroys it)
sat merged-but-undeployed, and the very next dispatch — #1402 — hit the
identical bug: **$3.44 and 10 minutes lost to something already fixed on
`main`.** Three separate dispatches were lost to that bug in total.

**When you merge something, immediately ask which row of that table it is in.**

Release steps: [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md#publishing-a-release-pypi).

---

## 2. Restarting anything needs a quiet fleet — and the two restarts are unsafe in *different* ways

The two services have **different victims**, so one check does not cover both.

### `coord agent update` / restarting `coord-agent` kills headless workers

A headless `claude -p` worker is a **subprocess of `coord-agent`**. Restart the
agent and the worker dies mid-task, its assignment flips to `failed`, and any
uncommitted work in its worktree is stranded.

**`coord sessions --remote` will NOT warn you** — it lists *interactive tmux*
sessions only. Headless workers are invisible to it.

This trap was walked straight into while deploying v0.4.77 (the release whose
whole purpose was fixing a different worker-work-loss bug): `coord sessions
--remote` said "No running interactive sessions", `coord agent update --all`
ran, and #1400's in-flight fix worker on elitebook went `running → failed`.

Check for **active assignments**, not sessions:

```bash
# per machine — the authoritative check
curl -s http://<agent-host>:7433/status | python3 -c \
  "import json,sys; print([(a['id'],a['status']) for a in json.load(sys.stdin).get('active',[])])"

# or from the board, for every machine at once
coord status                 # look for running assignments, not just machine health
```

Restart only machines with no active assignment. `coord agent update --machine
<name>` exists precisely so you can update the idle ones and come back for the
busy one.

### Restarting `coord-serve` breaks interactive finalize

An interactive session runs a **finalize backstop on exit** that POSTs to the
daemon to record the branch and terminal status. Restart `coord-serve` while one
is live and that finalize fails — losing the branch, the verdict, or both.

```bash
coord sessions --remote        # the RIGHT check for this one
ssh <daemon-host> 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-serve'
```

### Summary

| restarting | kills | check with |
|---|---|---|
| `coord-agent` (incl. `coord agent update`) | **headless workers** (subprocesses) | agent `/status` active list, or `coord status` |
| `coord-serve` | **interactive finalize** (branch/verdict write) | `coord sessions --remote` |

Doing a full deploy safely means both checks, and often waiting: a machine
running a worker cannot be agent-updated, and a fleet with a live interactive
session cannot have its daemon restarted.

---

## 3. `--briefing` / `--briefing-file` REPLACES the briefing

It is **not** an addendum. `coord assign` hands that text to
`_dispatch_headless` as the *whole* briefing — the issue body, project rules and
file scope are all dropped. The only thing appended anywhere is the freshness
addendum.

A worker given a four-line note this way ran the test suite, had nothing else to
do, and exited. Its closing summary read:

> *"This session's only instruction was to run the test suite in the foreground"*

The issue was never attempted, and the failure looked identical on the board to
the unrelated bug being investigated at the time.

**To ADD guidance while keeping the real briefing**, use the per-issue context
digest (#603), which is injected at the TOP of every briefing for that issue:

```bash
coord context add --pin <repo> <issue> '<one short note>'
```

---

## 4. `merge-base --is-ancestor` is the wrong merge check

`coord merge` defaults to `--method rebase`, which **rewrites commits** — so a
fully-merged branch's tip SHA is *never* an ancestor of the target. Squash has
the same property.

Naive ancestry therefore reports **every successful merge as a failure**.
Confirmed against #1344 / PR #1355: merged, two commits on `main`,
`--is-ancestor` still says no.

Verify with either:

```bash
gh pr view <branch> --json state          # authoritative; survives branch deletion
git cherry origin/<target> <branch>       # '-' = already upstream (patch-equivalent)
```

The same trap appears when judging whether a worktree holds unpushed work: a
branch showing "1 commit ahead" may be fully landed.

---

## 5. Pipeline membership is label-gated

`pipeline.tracked_labels` defaults to `['coord']`. An issue **without** that
label dispatches and runs perfectly normally but is **invisible** in the
coord-tui Pipeline — no card, no stage boxes, no progress.

```bash
coord issue label <repo> <issue> --add coord    # routes through the daemon seam
coord sync                                       # refresh the TUI cache (or press r)
```

Prefer `coord issue label` over raw `gh issue edit` — the latter does not update
the local issue cache, so the TUI keeps showing stale labels until the next sync.

---

## 6. Phantom `running` rows silently distort routing

`_reassign` (behind `coord retry`), `coord plan`'s idle detection, and any
load-based machine picker all derive "busy" from `board.active` rows with
`status == "running"`. Sessions that die without being reaped accumulate there.

Observed: **18 phantom rows aged up to 478 hours** made every capable machine
look busy while `coord status` correctly reported them all idle. `coord retry`
failed fleet-wide with the entirely unhelpful:

```
error: no available machine to retry on
```

`coord resume` is the manual sweep (it runs the interactive reapers). Fixed
automatically by #1396 — but if routing ever behaves inexplicably, check for
phantom rows first.

---

## 7. The recurring structural shape — check this first

Three separate bugs shared one cause, and it will produce more:

> **`reconcile()` accretes behaviour that the sanctioned automatic drivers never
> invoke.**

- `coord notify` mirrors *review* dispatch (`_dispatch_board_pending_reviews`)
  but has **no smoke-dispatch mirror** — so the Test gate had no headless
  producer at all.
- It also has **no interactive-reaper mirror** (#1396).
- The daemon `_tick_loop` calls `reconcile_completed_assignments`, **not** the
  full `reconcile()`.

So behaviour added to `reconcile()` reaches only `coord resume` — which on a
thin-client setup driven by the `coord-notify.timer` may be *never*.

**Assume anything new in `reconcile()` is dead on the automatic path until
proven otherwise**, and check the notify/tick mirrors when a stage mysteriously
never advances.

### 7a. #1616 — the daemon now has a clock, and the contract moved

The fifth instance of the shape above was the worst: `reconcile_completed_assignments`
set `status=done` and stopped *by contract*, and the only thing on this fleet that
ran everything downstream (`finished_at`, the completion comment, the #1076/#1152
test-gate backfill, Test/Review dispatch) was **a live `coord drive`'s stall nudge**
— `coord-notify.timer` is deliberately disabled. Boundaries cost 9 min (#1123) and
47 min (#1122); rows with no drive at all (vimcode#611/#613) waited for a human.

The fix deliberately did **not** widen `reconcile_completed_assignments`. The daemon
`_tick_loop` gained a sibling step:

```
Step 1  reconcile_completed_assignments   (passive; unchanged)
Step 1b _notify_drain_tick -> notify.run_drain   <-- the clock (#1616)
Step 2  enqueue_approved_work
```

`run_drain` is a **scoped** subset of `coord notify`, and the scope is the design:

| side effect | on the daemon's clock? |
|---|---|
| `finished_at`, completion comment, test-gate backfill | yes |
| Test-stage smoke dispatch, review dispatch, orphaned findings | yes (existing gates apply) |
| merge enqueue | already Step 2 |
| **work dispatch** | **no** |
| **fix-round dispatch** (`auto_loop`), stalled-pipeline dispatch | **no** — this is #476/#477 |

A duplicate *review* costs a few dollars; a duplicate *fix-worker* creates conflicting
branches, which is the incident that got the timer disabled. That asymmetry is the
whole argument — do not "simplify" `run_drain` into a call to `notify.run()`.

Knobs and consequences:

- `COORD_NOTIFY_DRAIN_INTERVAL` (default `60`, `0` disables — the escape hatch).
- The whole pass holds `~/.coord/notify.lock` (`coord.filelock.FileLock`, the same
  class `coord drive`'s `run_notify()` takes). The daemon's `/notify` endpoint takes
  it too — before #1616 a drive on a *remote* host held its own local file while the
  real work ran on the daemon, which serialized nothing.
- A `type="review"` `done` still parks in `finalizing` until the drain captures the
  verdict, but that window is now bounded by the interval instead of unbounded (#1610).
- `coord drive`'s stall detector is no longer load-bearing on the happy path. It is a
  stall detector again. (#1593, nudge-fires-once, is a real but separate bug.)

---

## 8. `coordinator.remote.yml` is a cache — your config edit will revert

On a thin client, `coord config` resolves to `~/.coord/coordinator.remote.yml`.
That file is **not** the config. It is a cache
(`coord.client.REMOTE_CONFIG_CACHE`, `client.py:38`) re-fetched from the
daemon's `GET /config` on essentially every thin-client command and overwritten
wholesale.

Edit it and the change disappears — usually within seconds, because the command
you run to *verify* the edit is itself a re-fetch. Both the edit and its
disappearance are silent. When this happened during the #1426 cutover the first
suspect was a concurrently-running agent; the actual culprit was the `coord
config` used to check the work.

Fleet config is edited on the **daemon host** — but not by editing
`~/.coord/coordinator.yml` directly: that path is a **symlink** into the
`coord-settings` checkout, and `vi`/`sed -i`/most editors write-and-rename
over it, silently replacing the symlink with a disconnected regular file
(see section 14). Edit the checkout instead:

```bash
coord sessions --remote        # MUST be empty — restart breaks interactive finalize
ssh <daemon-host> 'vi ~/src/coord-settings/coord/coordinator.yml'
ssh <daemon-host> 'git -C ~/src/coord-settings commit -am "..." && git -C ~/src/coord-settings push'
ssh <daemon-host> 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-serve'
coord config                   # re-caches from the daemon
```

Then read the refreshed local cache back: if it shows the new value, the edit
*and* the daemon reload are both proven in one step. Note that the daemon does
not hot-reload — without the restart the file is changed and nothing uses it.

---

## 9. The unattended driver (`coord drive`, and `scripts/drive-batch.sh`)

`coord drive <repo> <issue>` drives **one** issue Work → Test → Review → Merge
with nothing watching. It is resumable — re-run it on the same issue and it
picks up from wherever the board actually is. `--dry-run` shows state without
touching anything.

> **`scripts/drive-issue.sh` and `scripts/coord_issue_state.py` no longer
> exist.** #1392 ported the bash driver to `coord drive`; both files were
> deleted and this section still described them until 2026-08-01. If you are
> following an older note that names them, it means `coord drive`.

`scripts/drive-batch.sh` is the thin layer above it: drive **several**
independent issues sequentially on one machine, in one tmux session, overnight.
`--help` documents it. It is the hand-rolled prototype of `coord drive-epic`
(#1660) and should be deleted when that lands.

```bash
ssh dellserver
tmux new -s drive          # inside the ssh session, not chained with &&
MACHINE=dellserver ~/src/claude-coordinator/scripts/drive-batch.sh 1645 1650 1654
# Ctrl-b d, then log out; tmux owns the run
```

**Budget ~120 minutes per issue, and never lower it to "save time."** Measured
on the 2026-08-01 batch: a clean single-round issue in this repo on `sonnet` is
**~85 min** end-to-end (work ~40m + test ~23m + review ~22m); a review fix
round roughly doubles it. That run used `--deadline 45` and lost all five.

The trap is what a deadline actually does: **it stops the observer, not the
work.** All five drives exited at 45m; the fleet carried every one of them
through test and review anyway and approved each 21–133 minutes *after* its
drive was gone. The loop then started the next issue on top of the previous
one's live stages — so the batch was sequential in the script and concurrent on
the fleet, and the smoke stage landed on a machine that was supposed to be
asleep. A short deadline does not shorten the night; it silently makes the run
parallel. Design consequence for #1660: an expiry must escalate and stop, never
advance the frontier.

Supporting file: `coord-test-runner.sh` (the Test gate).

**The driver no longer runs the Test stage itself (#1426).** It observes
`test_state` exactly as it already did for Review and Merge. The suite is
dispatched as a real `type="smoke"` assignment by `dispatch_pending_smoke`,
mirrored into `coord notify` so the timer drives it (not only `coord resume` —
see section 7). That makes the stage board-visible and capability-routed, and
is what allows a drive to run from a host with no checkout.

Two config keys are load-bearing for that, both on the **daemon host's**
`coordinator.yml` (see section 8 — editing the thin-client cache does nothing):

- `smoke_tests.auto_queue: true` — defaults to **False**
  (`coord/config.py:151`), and while false `dispatch_pending_smoke` returns
  immediately. With the observer-only driver that means the Test gate has no
  producer at all and a drive waits until its deadline.
- the repo's `test_command` — for claude-coordinator this points at
  `scripts/coord-test-runner.sh`, because the repo is two codebases and a bare
  `pytest` or `cargo test` would run the wrong suite or miss half the diff.

**A systemd user unit's PATH is not a login shell's (#1814).** `coord serve`
runs as a systemd *user* unit, and `coord merge --revalidate` runs its composed
suite inside that daemon process. systemd never sources `~/.profile`, so
`~/.cargo/bin` is absent from the daemon's PATH — `systemctl --user
show-environment` is the environment that matters, not what `ssh` shows you.
Every Rust branch therefore failed revalidation with `cargo: command not found`,
reported as a red suite. The runner now resolves cargo explicitly (PATH →
`$CARGO_HOME/bin/cargo` → `rustup which cargo`) and, when it truly cannot find
one, prints `TOOLCHAIN MISSING` and exits **3** — which `--revalidate` renders
as `INFRASTRUCTURE FAILURE … the suite could not run`, never `SUITE FAILED`. If
you see that line, the branches are unjudged, not bad: fix the daemon's
environment and re-run. `deploy/coord-serve.service` also sets an explicit
`Environment=PATH=` as defence in depth; that line alone is never the fix,
because it repairs one host and leaves the next one silently broken. Sibling
bug, same environment class, different mechanism: #1809.

Note that enabling `auto_queue` also **back-fills**: any completed work row
with no test verdict becomes eligible, so turning it on works through the
existing backlog as capacity frees. Expect smoke assignments for issues you did
not just start.

Known limits, tracked under milestone #49 / epic #1406:

- Interactive (`provider_name="claude-pty"`) work **never** auto-dispatches a
  review (the #555 guard). The driver refuses at preflight; `--force-review`
  requests one explicitly, or use `coord review <aid>`.
- **The failure path is still lightly exercised.** Most drives pass their
  tests, so the `coord fix` loop on a genuine test failure has had few
  end-to-end runs.
- **Review verdicts consumed by the daemon drain did not reach the work row**
  (#1663) — the drive then waited out its full deadline on an issue that was
  already approved. **Fixed in `dbf5ee4`, but it is daemon-side**: per the
  merged≠live rule at the top of this file, it keeps biting until
  `coord-serve` is restarted on the daemon host. Independent of the deadline
  sizing above; both hit the same run.
- **Never edit `drive-batch.sh` while a batch is running.** bash reads a script
  lazily by byte offset, so an in-place edit shifts them under every live
  interpreter and can corrupt a run mid-flight. Copy, edit the copy, swap after.

Resolved since this list was first written: the runner's path routing now
covers other repos and **refuses** rather than recording a false `skipped`
(#1408); the Test stage is board-visible (#1395 `running` marker, then #1426);
merges serialize fleet-wide rather than per-machine (#1400).

**`coord drive-queue` (#1750, [`docs/DRIVE_QUEUE.md`](DRIVE_QUEUE.md)) is the
durable, timer-driven successor to typing `coord drive` by hand or to
`drive-batch.sh`'s bash loop** — it does not replace this section's deadline
trap, it inherits it: an expired `--deadline` still stops only the observer,
and the queue's capacity accounting is deliberately board-state-based (not a
session count) so it does not launch on top of that still-running work. The
operator-visible consequence is that `coord drive-sessions` will show *fewer*
drives than are actually running once a queued drive's observer has hit its
deadline — check `coord drive-queue status` instead. See
[`docs/DRIVE_QUEUE.md`](DRIVE_QUEUE.md) for the full runbook.

## 10. A stuck merge (`NEEDS_ATTENTION`) escalates instead of retrying — the `gh pr merge` escape hatch

`coord drive`'s merge stage (`_decide_merge` in `coord/drive.py`) only retries
`coord merge --only <aid>` for statuses a retry can actually change — an
in-flight `PENDING`/`READY`/`MERGING`, or `CONFLICT` (which retrying really
does drive forward, via the #241 auto-rebase machinery). Anything else —
most commonly `NEEDS_ATTENTION` (what a `HUMAN_REQUIRED`/`SKIPPED` queue entry
surfaces as once the server-side merge plan is in the `/board` payload), or a
status this driver has never seen before — **escalates on the first
encounter** instead of burning `max_merge_attempts` retries on a call that
cannot possibly land (#1505).

Escalating means: the driver writes a board-visible record (`coord escalate
record <repo> <issue> --reason ... --gate k=v ... --command ...`, one row per
issue, replacing any earlier record for the same issue) naming why it
stopped, the gate readings it observed (merge status/reason, review verdict,
test state, PR url), and a **proposed** fix command — then exits with code
`4` (`EXIT_ESCALATED`, distinct from the generic terminal-failure `1`).
**Nothing runs the proposed command automatically** — a human (or the TUI's
Pipeline right-click menu → "Run proposed fix", which shells out to `coord
escalate run <repo> <issue>`) has to explicitly ask for it.

**The sanctioned escape hatch**, when the escalation names a known PR and
everything else — CI, review, patch identity — is already green (this was
the entire #1477 postmortem: the coordinator had every fact it needed and no
way to act on it):

```
gh pr merge <pr-number> --rebase
coord reconcile-merges
```

`gh pr merge` lands the PR directly (bypassing the coordinator's own queue,
which had already given up on this entry); `coord reconcile-merges`
backfills the board so the merge-queue entry, the issue, and any downstream
dependents reflect what GitHub now shows as merged — skipping this step
leaves the board thinking the PR is still open. Read the escalation's own
gate readings first (`coord escalate list --repo <repo>`, or the Pipeline
row's right-click menu) — a `gh pr merge` on a PR that ISN'T actually clean
(a failed check, an unapproved review) just moves the mess onto GitHub. Once
resolved, `coord escalate dismiss <repo> <issue>` clears the record (`coord
escalate run` does this automatically on a zero exit).

## 11. A `smoke_required`/`review_required` merge refusal that contradicts the board's own `test=`/`review=` reading escalates too (#1526)

`coord drive` decides the Test/Review gates are satisfied from
`work_test_state`/`review_verdict` — but `coord merge` enforces a fresher
check (`merge_queue.has_smoke_verdict`/`has_approved_review`: SHA/patch-id
-anchored freshness for smoke, patch-id voiding for a rebased review) and can
refuse for a reason the driver's own view never saw coming — most often a
rebase onto a moved `main` that correctly voids an already-"passed"/"approve"
verdict. Two real overnight stalls (2026-07-27/28, `#1412` and `#1483`) hit
exactly this: the board showed a green `test=`/`review=` line, `coord merge`
printed `smoke_required`/`review_required` into the tmux pane, and the driver
spent its entire `max_merge_attempts` budget re-running the identical
`coord merge --only` — which can never change either side of that
disagreement — before dying without naming which gate blocked it.

`_decide_merge` now detects this divergence (`_merge_gate_divergence` in
`coord/drive.py`) and escalates on the FIRST encounter, exactly like rule #10
above, with a gate-specific proposed command:

```
# smoke divergence:
coord test <work_aid> --passed   # ONLY if the suite genuinely still passes
                                  # against the CURRENT base — otherwise
                                  # dispatch a fresh smoke test

# review divergence:
coord review-reaffirm <work_aid> --reason '<why this delta is safe>'
# or a full re-review:
coord review <work_aid>
```

Neither command runs automatically — same one-key-human-decision posture as
every other escalation. The escalation is also posted as a comment on the
GitHub issue itself (not just the tmux pane and the `coord escalate` board
row), so it survives the drive session ending.

## 12. The graphify graph is silently stale more often than you think — and worktree agents have none at all

Two failures, both invisible from the code, both costing agent time on wrong
answers rather than money.

### Worktree agents are graph-blind by construction

`graphify-out/` is gitignored on purpose — only `graphify-out/.gitignore` is
tracked (the graph is multi-MB, rewritten every commit, and would conflict
across parallel worker branches). So `git worktree add` materialises an *empty*
`graphify-out/`, and `graphify query` resolves `graphify-out/graph.json`
**strictly relative to cwd** — no upward walk, and `query` has no `--graph`
override (only `path`/`explain`/`diagnose` do). Every agent in a worktree —
coord's `~/.coord/worktrees/*`, Claude Code's `.claude/worktrees/*`, review
worktrees — silently falls back to grep.

The fix is `.githooks/post-checkout`, which symlinks each entry of the base
checkout's graph (`graph.json`, `manifest.json`, `cache/`, ...) into the
worktree's `graphify-out/` — it never replaces the directory itself. It lives
in a **hook**, not in coord's dispatch code, because `git worktree add` fires
`post-checkout` with cwd set to the new worktree — so one implementation
covers every creator (coord's two remote `worktree add` sites, Claude Code,
and anything by hand) on every machine. It chains to
`$GIT_COMMON_DIR/hooks/post-checkout` in the main worktree, leaving
graphify's own machine-pinned block alone.

**It only runs where `core.hooksPath` points at it** — one command per machine,
and nothing enforces it:

```bash
git -C ~/src/claude-coordinator config core.hooksPath .githooks
```

**#1617:** an earlier version of this hook did `rm -rf graphify-out && ln -sfn
$base/graphify-out graphify-out` — replacing the *whole directory* with a
machine-local, absolute-path symlink. `graphify-out/` is only invisible to
git because of the tracked file inside it, `graphify-out/.gitignore` (`*` /
`!.gitignore`); the `rm -rf` deleted that tracked file, so every worktree got
a spurious diff (a deleted tracked `.gitignore` plus an untracked absolute
symlink) that coord's own worktree-rescue commit then committed verbatim.
The fix keeps `graphify-out/` a real, tracked-`.gitignore`-holding directory
and symlinks only what's *inside* it — the self-ignoring `.gitignore` rule
does the rest for free. The acceptance bar for any change here is `git
status --porcelain` being empty in a fresh linked worktree; test that with a
real `git worktree add`, not a mock.

Rebuilds stay disabled inside worktrees deliberately: `graphify-out`'s
contents are symlinks to the *shared* graph, so a rebuild there would
overwrite it from a feature-branch tree — and a worktree can be reaped
mid-rebuild, which is where graphify's own "burns a full AST pass and then
dies with ENOENT" comment came from.

### The hooks cannot keep the graph in sync, and fail silently when they try

Do not assume "the hooks handle it." Structurally they don't:

- `post-commit` / `post-checkout` / `post-merge` all `exit 0` during a
  **rebase, merge, or cherry-pick** — so the merge agent's proactive rebase
  (#306), the most common ref move in the fleet, never rebuilds.
- **`git reset --hard` fires nothing.** Git has no post-reset hook.
- Every failure path is `exit 0`, and the rebuild is a detached background
  process with a 600s `SIGALRM` timeout logging to
  `~/.cache/graphify-rebuild.log`. A timeout, an OOM, or an ENOENT all fail
  invisibly.
- Concurrent triggers coalesce (`Rebuild already in progress — changes queued`).
- The hooks' own `[ ! -f graphify-out/graph.json ] && exit 0` guard is a
  **permanent off-switch**: purge the graph once and they no-op forever.

So check, don't assume. `GRAPH_REPORT.md` records the commit it was built from,
which makes staleness a one-line comparison:

```bash
coord diagnose --graph     # every local checkout: in-sync vs STALE, + hooksPath
```

It prints a `GRAPH_HEALTH: checkouts=N stale=M` trailer. Fix a stale one with
`graphify update .` in that checkout. Worth running after any `reset --hard`,
after a rebase-heavy session, and on each machine periodically — the first real
run of it caught `~/src/vimcode` sitting 55 commits behind its own graph.

## 13. The `coord-drive-queue` timer runs a pinned CLI nothing upgrades for you, and a short queue is not unattended-safe

Two traps specific to the `coord drive-queue` timer (#1756,
[`docs/DRIVE_QUEUE.md`](DRIVE_QUEUE.md)), on top of everything section 9
already says about `coord drive` itself.

**The timer's `coord` is whatever `~/.local/bin/coord` resolves to — it does
not notice a merged fix until that install is upgraded**, and it is easy to
forget because the unit just keeps running "successfully" against stale
code. This is the same class of trap as item 1 (a merged fix is not a live
fix) and as the epic sequencer's separate `~/.coord-cli-venv` lane (§
"The fourth lane" in `docs/AGENT_OPERATIONS.md`, found **three releases
stale** on 2026-07-29) — but NOT the same venv. On dellserver,
`~/.local/bin/coord` is a symlink into `~/.coord-venv`, the same pinned,
non-editable venv `coord-agent` itself runs from (`deploy/coord-agent.service`),
so it rides the ordinary agent upgrade lane rather than needing a bespoke one:

```bash
coord agent update --machine dellserver   # or --all — the standard lane
~/.local/bin/coord --version              # VERIFY it took — an upgrade
                                           # silently no-ops more often than
                                           # you would think
```

The topology is **per-machine**: a dev box's `~/.local/bin/coord` is commonly
an *editable* install pointing at a checkout, which is fine interactively but
unsafe under this timer (`pip show claude-coordinator | grep -i editable`
must print nothing before you install the unit there).

**A queue longer than ~2 issues on one repo is not yet unattended-safe
(#1715).** Every merge stales every other queued branch's Test verdict on
that repo, and `coord drive` escalates to a human on a stale verdict rather
than re-testing — so *N* queued issues can cost *N−1* human interventions
overnight, the opposite of what a queue is for. Full reasoning and the
smaller, partially-mitigated `#1738` sibling trap (a content-irrelevant base
move) are in [`docs/DRIVE_QUEUE.md`](DRIVE_QUEUE.md)'s top section.

## 14. Fleet `coordinator.yml` is edited in a *different repo* than the one you're reading this in — and the daemon can silently run a broken copy of it

The daemon host's `~/.coord/coordinator.yml` is not a file you edit in place.
It is a **symlink** into a separate checkout, `~/src/coord-settings`
(`$COORD_SETTINGS_DIR` if overridden), pointing at
`coord-settings/coord/coordinator.yml`. The loop is:

```bash
# edit + review, in the coord-settings repo — NOT this one
vi ~/src/coord-settings/coord/coordinator.yml
git -C ~/src/coord-settings commit -am "..." && git -C ~/src/coord-settings push

# deploy, on the daemon host
git -C ~/src/coord-settings pull
```

That's it — **no `coord-serve` restart is needed.** `_reload_config_if_stale`
(#1081, `coord/serve_app.py`) tracks the backing file's mtime and swaps in a
freshly-parsed `Config` the moment anything notices it changed; `GET /config`
(what a thin client's `coord config` re-fetches into
`~/.coord/coordinator.remote.yml` — see item 8, a *completely different*
cache from the checkout this section is about) serves the raw bytes fresh on
every request regardless. If you find yourself restarting the daemon after a
`coordinator.yml` edit, that restart isn't doing anything for the config —
check whether you actually needed item 2's session-safety dance for it.

**A malformed edit is swallowed, not rejected.** If the pulled file fails to
parse (bad YAML, a validation error), `_reload_config_if_stale` logs one
warning and keeps serving the *last-good* `Config` for the daemon's own
gating decisions (review/pipeline/merge-auto-drain/milestone-auto-dispatch) —
but it still advances its mtime marker, so it will not retry the reload until
the file changes *again*. Meanwhile `GET /config` has no such guard: it hands
the broken bytes to every thin client that asks. The daemon and its clients
now silently disagree about what the fleet's config is, and nothing says so
until something downstream behaves strangely. **Validate the YAML before you
push it.**

Three narrower failure modes sit on top of "no content drift is possible
because the symlink makes the live file the tracked file" — each invisible
from a running fleet, each different enough to need a different fix:

1. The symlink gets silently replaced by a regular file (`coord init` offers
   to overwrite `coordinator.yml`; any `scp`/`cp`/editor write-and-rename
   does the same) — the daemon goes back to running an untracked file with a
   perfectly healthy-looking repo.
2. The checkout has uncommitted changes — a direct edit to the live path
   writes *through* the symlink into the checkout's working tree.
3. The checkout is behind (or ahead of) `origin` — pulled-but-not-pushed, or
   pushed-but-not-pulled.

`coord diagnose --config-provenance` (#1779, `coord/fleet_config_health.py`)
checks all three, read-only and with no network access (sync-vs-`origin` is
read from the existing remote-tracking ref, never a fresh `fetch`). It is a
**neutral skip**, not a warning, on every machine with no coord-settings
checkout — which is every machine except the daemon host and the operator's
box; the checkout is deliberately kept out of the fleet's own repo list so a
dispatched worker can never edit the file governing its own concurrency
limits, capability routing, and review gates.

---

## 15. `deploy/**` is a deploy lane with no deploy step — a unit file drifts forever unless you diff it by hand

`deploy/*.service`/`*.timer` is version-controlled, reviewed, and merged like
any other file. **Nothing in the release path ever installs it.** Bump → PR →
merge → tag push → `publish.yml` → PyPI, then `coord agent update` for the
venvs — none of that touches `~/.config/systemd/user/`. A unit is
hand-installed once at machine setup and then drifts silently: reviewing and
merging a `deploy/**` change *reads* like shipping a fix, the same trap as
item 1's table above, except here there isn't even a documented "restart X"
step to forget — there's no step at all.

Found on dellserver 2026-08-04 (#1831), immediately after a release, and it
was not cosmetic drift: `coord-serve.service` was **three weeks stale**, and
its `Environment=PATH=` still put an **editable** checkout of this repo
(`~/src/claude-coordinator/.venv/bin`) ahead of the pinned release.
`coord_argv()` (`coord/drive.py`) resolves subprocesses via
`shutil.which("coord")` — i.e. from that PATH — so the daemon process itself
ran the release while every worker it spawned ran stale editable code. Every
version readout anyone checked (daemon, all three agents, PyPI's simple
index, `~/.local/bin/coord`) said the release was live. None of them look at
what a unit's *subprocesses* actually resolve `coord` to — that fact only
exists on the filesystem of whichever host installed the unit, and nothing
was diffing it against `deploy/`.

**Two independent things closed this, and either alone regresses the other's
prior fix** — see `deploy/coord-serve.service`'s own comment, which names
both #1814 (added `~/.cargo/bin` for `cargo test` inside a revalidate) and
#1117 (added the repo venv so `cli-pytest` acceptance subprocesses import
`httpx`). The corrected PATH keeps both, ordered so `~/.local/bin` (a symlink
onto the pinned release) precedes the repo venv — the repo venv shadowing the
release was never necessary for either fix, it just happened to be first.

**Detection, not automation.** `coord doctor` / `coord health` now report
unit-file drift as a check (`unit_drift` / `fleet_unit_drift`,
`coord/health/checks/unit_drift.py`) — installed content vs. the units
**packaged with the installed release** (`coord/deploy/`, #1927; the
reference is deliberately *not* the host's own `deploy/` checkout, which
goes stale in step with the installed unit and so reports clean exactly
when it shouldn't) with the installed mtime and a line-diff summary, and
separately,
CRIT if any unit's PATH lets a `.venv/bin` checkout precede the release entry
points (`~/.local/bin`, `~/.coord-venv/bin`). Run `coord doctor` after any
`deploy/**` merge and after any machine-setup change that touches a unit
file — there is no automatic install step by design (writing a `systemctl`
unit on every host unattended is a bigger blast radius than silent drift),
so a human still has to run the `cp` + `systemctl --user daemon-reload &&
systemctl --user restart <unit>` the check's own detail line prints.

Full story: [`AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md#the-sixth-surface-deploy--reviewed-like-code-installed-by-nobody).

---

## 16. A review that reached `END_REVIEW` with no `REVIEW_VERDICT:` header is recoverable — recover it, don't re-dispatch (#1956)

A headless reviewer can write a complete, well-reasoned review — full body,
`## Blocking findings` / `None.`, thorough — and still end with `status=done`
+ `review_verdict=None` on the board, because it followed the *tail* of the
required format (`END_REVIEW`) while dropping the *header* that carries the
machine-readable data (`REVIEW_VERDICT:`). Live on quadraui#533
(2026-08-07): grepping the raw log for `REVIEW_VERDICT` found the string
exactly once, and that occurrence was inside the reviewer's own briefing
instructions — never emitted by the model. `coord gates` blocks with
`review : BLOCKED — review required but not approved`, which reads
*identically* to "review hasn't run yet" — nothing says the verdict is
sitting right there in the log, recoverable.

**How to tell the two apart.** `coord gates <repo> <issue>` now renders this
case as `review : ERROR`, not `BLOCKED` — `coord notify`'s own log also
carries a `log.warning` naming the assignment and quoting the excerpt right
before `END_REVIEW`, and the GitHub completion comment posted for the review
assignment says so too (distinct wording from the generic "findings could
not be extracted" message). If you see plain `BLOCKED`, the review really
hasn't produced anything usable yet; if you see `ERROR` (or the loud log
line / GitHub comment), the verdict is very likely sitting in the transcript
already.

**Recovery — do NOT re-dispatch.** Re-running the review costs a full cycle
to re-derive a conclusion already sitting in the log, and elitebook has a
documented ~14% rate of dropping the header again on the very next attempt
(`incident_elitebook_review_verdict_capture_drops`). Instead, read the
transcript, confirm the verdict the reviewer actually reached, and relay it
through the same seam a reviewer's own `REVIEW_VERDICT:` line would have
written to:

```
coord report-result --assignment <review_assignment_id> \
  --verdict <approve|request-changes> \
  --verdict-source recovered \
  --verdict-reason "REVIEW_VERDICT header missing, recovered from transcript (#1956)" \
  --body-file <extracted-review.md>       # required with --verdict request-changes
```

`--verdict-source recovered` (with a required `--verdict-reason`) is not
optional decoration — a relayed verdict with no stated provenance is
indistinguishable from one the reviewer agent produced itself at every
surface that shows it (`coord gates`, the board, the audit trail). Use
`--verdict-source overridden` instead of `recovered` when you are recording
a **different** verdict than what the reviewer actually concluded (a
deliberate human override), never for a straightforward transcript rescue —
the two read differently everywhere downstream, on purpose (#1956's second
half: a recovery asserts "the reviewer decided this, we merely restored it";
an override asserts "the reviewer decided otherwise and a human disagreed").

A `REVIEW_VERDICT:` marker that IS present but malformed (a bolded
`**REVIEW_VERDICT:**`, mismatched terminator, …) is the older, sibling
#1348 diagnostic — same recovery command, same `--verdict-source recovered`
convention, different detection path (`coord.review.detect_unparsed_review_marker`
vs. `coord.review.detect_end_review_without_verdict` for the header-omitted-
entirely case documented above).

**Historical rows predate the `verdict_source` column and read as `agent`
by construction — that is wrong for two known rows.** `verdict_source`
defaults to `NULL` on any pre-#1956 row, and every reader (`format_gate_report`,
`coord.models.Assignment.verdict_source`) treats `NULL` as `"agent"` — an
earned verdict, indistinguishable from a relay. Two rows from 2026-08-07
are known to be mislabeled that way and still need a one-time manual
backfill (an operator action against the live board, not something this
codebase can migrate automatically — a migration has no way to know which
historical `approve`s were relayed):

```
# fb021a044a0e (quadraui#533) — recovered from transcript, see case study above
coord report-result --assignment fb021a044a0e --status done --verdict approve \
  --verdict-source recovered \
  --verdict-reason "REVIEW_VERDICT header missing, recovered from transcript (#1956)"

# 7c5a9fe11925 (quadraui#545) — operator deliberately overrode a live request-changes
# after resolving the sole blocking finding (PR metadata, #1978)
coord report-result --assignment 7c5a9fe11925 --status done --verdict approve \
  --verdict-source overridden \
  --verdict-reason "operator override: resolved sole blocking finding (PR metadata, #1978)"
```

Until an operator runs these, `coord gates` / the audit trail will keep
showing both as plain `agent`-sourced approvals.

---

## 17. `test-mode:smoke` switches the HEADLESS Test stage off — including for every later fix round (#685/#2024)

The label is a **policy**, not a hint. With `test-mode:smoke` on an issue,
`coord.smoke.dispatch_pending_smoke` skips every work row for it on every
tick, forever, by design — the Test stage is meant to be human-attended (the
TUI's interactive smoke agent). Nothing else automatic records a Test verdict.

That is fine for round 0, which a human is usually watching. It is a **dead
end for every `--fix-of` round**, because:

* a fix round is a **new work row on the same branch**, carrying its own empty
  `test_state`; and
* review dispatch is held until *that* row carries `passed`/`skipped`
  (`pipeline.default_gates: [test, review, merge]` →
  `PipelineConfig.test_precedes_review()`, honoured by both
  `dispatch_pending_reviews` and `auto_loop.run_for_fix_transition`).

One component requires a verdict; by policy no component will produce one. The
fix worker finishes, and the pipeline stops with nothing running.

**It does not look blocked, it looks slow.** `coord gates` reads the
*branch-scoped merge* gate, which the PARENT row's verdict satisfies:

```
test   : passed (recorded on 8965c044976e)      # ← the parent, not the fix round
```

while `coord drive` reads the *current row's own* verdict and prints
`test=-`. Both are right. Since #2024 the gates summary names the row and adds
a note when the branch's current work row has no verdict of its own, and
`coord drive` exits `EXIT_DEAD_END` (6) with `test_stage_human_attended`
instead of counting `no state change` — vimcode#635 burned 25 minutes and then
160 minutes on exactly this, twice on one issue, each cleared within minutes of
an operator running `coord test <fix_aid> --passed` by hand.

**Recovery — record the verdict on the FIX row (not the parent):**

```bash
coord test <fix_aid> --passed                    # or: --skipped --reason "<why>"
# ...or actually run the attended stage the label asked for:
coord assign <machine> <repo> <issue> --smoke-of <fix_aid> --interactive
```

**Prevention:** if an issue is going to be driven unattended, do not leave
`test-mode:smoke` on it — `coord set-test-mode <repo> <issue> auto` puts the
headless Test stage back in charge of every round. The label is the right tool
for "the suite needs hardware/eyes"; it is the wrong tool for "this one suite
run was noisy", which is what `coord test <aid> --skipped --reason ...` is for.
