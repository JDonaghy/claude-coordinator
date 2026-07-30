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

Fleet config is edited on the **daemon host**:

```bash
coord sessions --remote        # MUST be empty — restart breaks interactive finalize
ssh <daemon-host> 'vi ~/.coord/coordinator.yml'
ssh <daemon-host> 'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-serve'
coord config                   # re-caches from the daemon
```

Then read the refreshed local cache back: if it shows the new value, the edit
*and* the daemon reload are both proven in one step. Note that the daemon does
not hot-reload — without the restart the file is changed and nothing uses it.

---

## 9. The unattended driver (`scripts/`)

`scripts/drive-issue.sh` drives one issue Work → Test → Review → Merge with
nothing watching, using only normal `coord` commands. It is resumable — re-run
it on the same issue and it picks up from wherever the board actually is.
`--dry-run` shows state without touching anything.

Supporting files: `coord-test-runner.sh` (the Test gate) and
`coord_issue_state.py` (read-only per-issue state oracle, ETag-cached).

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

Note that enabling `auto_queue` also **back-fills**: any completed work row
with no test verdict becomes eligible, so turning it on works through the
existing backlog as capacity frees. Expect smoke assignments for issues you did
not just start.

Known limits, tracked under milestone #49 / epic #1406:

- Interactive (`provider_name="claude-pty"`) work **never** auto-dispatches a
  review (the #555 guard). The driver refuses at preflight; `--force-review`
  requests one explicitly, or use `coord review <aid>`.
- **The failure path is still unproven.** Every drive so far has passed its
  tests, so the `coord fix` loop on a genuine test failure has not run
  end-to-end. This is the reason #1392 (port to `coord drive`) is deliberately
  not dispatchable yet — porting now would port unexercised paths.
- Flags must precede the positionals (`drive-issue.sh --machine precision
  <repo> <issue>`); the parse loop `break`s at the first non-flag, so trailing
  flags fail with a usage dump. Fixed by #1392's Click port.
- **Never edit `drive-issue.sh` while a drive is running.** bash reads a script
  lazily by byte offset, so an in-place edit shifts them under every live
  interpreter and can corrupt a run mid-flight.

Resolved since this list was first written: the runner's path routing now
covers other repos and **refuses** rather than recording a false `skipped`
(#1408); the Test stage is board-visible (#1395 `running` marker, then #1426);
merges serialize fleet-wide rather than per-machine (#1400).

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

The fix is `.githooks/post-checkout`, which symlinks the worktree's
`graphify-out` at the base checkout's graph. It lives in a **hook**, not in
coord's dispatch code, because `git worktree add` fires `post-checkout` with
cwd set to the new worktree — so one implementation covers every creator
(coord's two remote `worktree add` sites, Claude Code, and anything by hand) on
every machine. It chains to `$GIT_COMMON_DIR/hooks/post-checkout` in the main
worktree, leaving graphify's own machine-pinned block alone.

**It only runs where `core.hooksPath` points at it** — one command per machine,
and nothing enforces it:

```bash
git -C ~/src/claude-coordinator config core.hooksPath .githooks
```

Rebuilds stay disabled inside worktrees deliberately: `graphify-out` is now a
symlink to the *shared* graph, so a rebuild there would overwrite it from a
feature-branch tree — and a worktree can be reaped mid-rebuild, which is where
graphify's own "burns a full AST pass and then dies with ENOENT" comment came
from.

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
