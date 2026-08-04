# The drive queue (`coord drive-queue`, #1750)

`coord drive <repo> <issue>` drives **one** issue end-to-end with nothing
watching. Nothing decided *what to drive next* — that used to mean either a
human typing the next `coord drive` by hand, or `scripts/drive-batch.sh`, a
bash loop that is durable for exactly one tmux session and gone the moment it
dies. `coord drive-queue` is the durable, board-backed replacement: declare
the order once (with pins and dependencies), and a periodic `tick` launches
at most one drive per run, first-eligible-wins, never past the concurrency
ceiling.

This is the operator runbook. For the implementation, see `coord/drive_queue.py`
(pure `plan_tick`) and `coord/commands/drive_queue.py` (the CLI/tick shell).

---

## Read this before queuing more than ~2 issues (#1715)

**A queue longer than about two issues is still not an *unattended* feature —
but clearing it is now one command and one suite run, not *N−1* by hand.**

Every merge on a repo invalidates every *other* queued branch's Test verdict on
that repo (the base moved). Queue *N* independent issues against the same repo
and merging the first one stales the other *N−1*. That is the cascade, and it
used to cost *N−1* human interventions — which defeats the entire point of an
unattended timer.

Three arms have since landed against it:

* **#1738** — `coord drive` re-dispatches the Test stage once, automatically,
  on a STALE (not missing) verdict. Only fires while a **live drive** is
  watching the issue. Measured 2026-08-03: reached 1 of 4 real stalls.
* **#1769** — `coord merge --revalidate` re-tests a stale-but-`passed` entry
  against the current base from the *merge* lane, which is where a branch with
  no live drive actually sits.
* **#1715** — that flag **batches**. When several queued entries share a base
  they are composed onto it together and validated by **ONE** suite run, not
  one each. A four-branch group costs ~7 minutes, not ~26.

So the practical cost of a deep queue is now: let it run, then drain it with

```bash
coord merge --revalidate --dry-run   # names each batch and its members
coord merge --revalidate             # one composed suite run per base, then merge
```

**What has *not* changed: none of this is automatic.** `--revalidate` is
strictly opt-in and the unattended auto-drain passes `revalidate=False`
permanently — starting suite runs on a timer is the shape that was gated off
after the 2026-06-07 token-burn incident, and `merge.auto_drain` is `false` by
design. An overnight timer with no operator still parks stale entries; the
difference is that clearing them in the morning is one command and one suite
run rather than *N−1* worktrees by hand.

One honest caveat on the batch: a composed run validates the **composite**, not
each branch alone. Every member already carries its own `passed` verdict from an
earlier base, so the composite re-confirms they still hold *together* against
the current one — a re-confirmation, not a first proof. An entry that never had
a verdict, or that is blocked on review/CI/conflict, is never included. If the
composite fails, **nothing merges**; each branch is then re-tested alone so the
culprit is named and the innocent branches still go through.

Related, narrower version of the same class: **#1738** made the base-freshness
check a little smarter — a base move that only touches `docs/**`,
`scripts/**`, `.github/ISSUE_TEMPLATE/**`, or a top-level `*.md` file is
recognized as content-irrelevant and does **not** stale a green Test verdict.
Everything else still does, and `coord drive` is "biased hard toward staling"
by design (an unreadable diff or any file outside that allowlist keeps the
pre-#1738 behavior). `coord drive` now re-dispatches the Test stage once,
automatically, on a STALE (not missing) verdict, bounded by the same
`fix_rounds` budget as a real test failure — but that budget is shared with
genuine fixes, so a queue that keeps re-staling itself burns it fast and still
lands on the human escalation.

**Practical guidance:** queue depth per repo is no longer bounded by the
*arithmetic* — it is bounded by whether anyone is around to type
`coord merge --revalidate` afterwards. For a genuinely unattended overnight
run, still prefer 1–2 entries per repo, or issues in *different* repos (a
merge in one repo does not stale verdicts in another). For a run you will come
back to, queue as deep as you like: the morning drain is one command.

---

## 1. Enqueue

CLI:

```bash
coord drive-queue add REPO ISSUE                        # append to the tail
coord drive-queue add REPO ISSUE --machine dellserver    # pin to one machine
coord drive-queue add REPO ISSUE --after 1750            # wait for #1750 first
coord drive-queue add REPO ISSUE --after 1750,other#42   # comma-separated, repeatable, cross-repo
coord drive-queue add REPO ISSUE --position 0            # insert at the head instead of appending
```

`--machine` pins the drive to one machine (default: let `coord drive` route
it). `--after` names pre-req issues that must land first — a bare number
resolves against `REPO`, `repo#issue` crosses repos; a self-edge or a
dependency cycle is rejected **before** the write, leaving the queue
untouched (the same posture `coord milestone write-order` takes). Re-running
`add` on an already-queued `REPO ISSUE` updates it in place.

```bash
coord drive-queue list             # the queue in run order, with state/attempts/deferrals
coord drive-queue status           # counts by state, plus the current queue-level alert
coord drive-queue remove REPO ISSUE
coord drive-queue move REPO ISSUE --to 0
```

TUI: right-click the status bar's `QUEUE: …` segment → **"Drive queue…"**
(shortcut `q`) opens the overlay — `j`/`k` to move the cursor, `J`/`K` to
reorder the selected entry, `x` to remove it, `u` to unblock a `blocked`
entry in place (the same remove-and-re-add the CLI's suggested fix performs,
without leaving the overlay). To add an issue from the Pipeline: right-click
its row → **"Add to drive queue"** → pick a machine (or "no preference").

## 2. Install the timer

Same host as `coord-serve`/`coord-web`/`coord-notify` — the daemon host that
owns `~/.coord/coord.db` (dellserver in production). The tick subprocess-
launches `coord drive --tmux`, which needs a local tmux server and the repo
checkouts under `SRC_ROOT`, so — like `drive-batch.sh` — it belongs where
those exist, not on a thin client.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/coord-drive-queue.service deploy/coord-drive-queue.timer \
    ~/.config/systemd/user/
loginctl enable-linger "$USER"          # survive logout / reboot
systemctl --user daemon-reload
systemctl --user enable --now coord-drive-queue.timer
```

This is a `Type=oneshot` service activated **by the timer** — do not
`systemctl --user enable coord-drive-queue.service` directly (it has no
`[Install]` section). Verify one tick runs clean before trusting the timer:

```bash
systemctl --user start coord-drive-queue.service
journalctl --user -u coord-drive-queue -n 50
systemctl --user list-timers | grep drive-queue   # next-elapse ~15min out
```

Logs live in the user journal: `journalctl --user -u coord-drive-queue -f` to
follow live, `-n 50` for the last tick's summary. With an empty queue a tick
logs `capacity: 0/1 occupied, 1 free` / `no launch` and exits 0 — that is the
timer working, not a problem.

## 3. Stop it

Two different questions, two different commands — the same "hold, don't
kill" distinction `coord pause` draws for routing, and it trips people the
same way when they only know the pause half:

```bash
# 1. Stop LAUNCHING new drives from the queue. Does NOT touch anything
#    already running.
systemctl --user stop coord-drive-queue.timer

# 2. Stop a drive that is ALREADY running: kills its tmux session and
#    releases the per-issue flock instantly (so `coord drive`'s own
#    already-driving guard never sees a stale lock).
coord drive-stop REPO ISSUE
```

Say both, in that order, when you tell someone how to stop the queue.
Stopping the timer alone leaves every currently-launched drive running to
completion (or its deadline); it just stops the tick from starting the *next*
one. `coord drive-sessions` lists every live `coord drive --tmux` session (with
its own attach/stop hints) if you need to see what's actually running before
deciding what to `drive-stop`.

## 4. Read the alert — `QUEUE: STALLED` vs `QUEUE: BLOCKED`

The TUI status bar always shows a `QUEUE: …` segment (never blank — silence
reads as "nothing to report" when it might mean "the segment crashed"):

| Segment | Meaning | What to do |
|---|---|---|
| `QUEUE: empty` | nothing queued | nothing |
| `QUEUE: 1 running · 3 waiting` | normal operation | nothing |
| `QUEUE: STALLED — 3 waiting, none eligible` (warn) | capacity is **free**, but every waiting entry is deferred — usually waiting on an `--after` pre-req that hasn't landed yet | usually self-resolves once the pre-req lands; `coord drive-queue status` shows the alert's `gate_readings` detail lines naming exactly which entries are deferred and why |
| `QUEUE: BLOCKED 2 · 1 waiting` (warn/crit, **outranks a simultaneous stall**) | one or more entries are unsatisfiable and will never launch on their own: a dependency cycle, an `--after` pre-req that can't resolve, or a drive session that died `attempts` times in a row (default `DEFAULT_MAX_ATTEMPTS = 2`) | needs an operator action — see below |

`coord drive-queue status` (or the TUI overlay) shows the reason for both.
Each `blocked` entry's fix is remove-and-re-add — there is deliberately no
`coord drive-queue reset`, because a fresh row is already `waiting` with
`attempts=0` and no stale `--after`:

```bash
coord drive-queue remove REPO ISSUE && coord drive-queue add REPO ISSUE
# re-add WITHOUT the bad --after if that was the cause
```

or, from the TUI overlay, select the blocked entry and press `u`.

## 5. The pinned-CLI trap

The timer runs a **specific installed `coord`**, not a checkout — it does not
notice a merged fix on `main` until that `coord` is upgraded. On dellserver,
`~/.local/bin/coord` is a symlink into `~/.coord-venv`, a pinned, non-editable
PyPI install — the very same venv `deploy/coord-agent.service` runs `coord
agent` from (`%h/.coord-venv/bin/coord`, see `install-agent.sh`). That already
satisfies #1523's "the runner's own CLI must not be rewritable by worker
branch churn" requirement, **and** it means this venv is upgraded by the
ordinary, already-documented fleet procedure. It is deliberately **not** a
bespoke pinned venv like the epic sequencer's `~/.coord-cli-venv`
(`docs/AGENT_OPERATIONS.md`'s "fourth lane"), which only a human remembering
to run the upgrade keeps current, and which was found three releases stale
on 2026-07-29.

```bash
coord agent update --machine dellserver   # or --all; upgrades ~/.coord-venv
                                           # in place and restarts coord-agent
~/.local/bin/coord --version              # VERIFY it took — an upgrade
                                           # silently no-ops more often than
                                           # you would think
```

`coord agent update` restarts `coord-agent`, which kills any headless worker
currently running on dellserver — check for active assignments first (see
`docs/OPERATING_GOTCHAS.md` §2). It does **not** interrupt a `coord drive
--tmux` session the queue already launched: that runs as its own tmux/`coord
drive` process tree, independent of `coord-agent`.

**If you ever install this timer on a machine other than the current daemon
host, verify first that its `coord` resolves to a non-editable install:**

```bash
readlink -f ~/.local/bin/coord                   # which venv it resolves to
pip show claude-coordinator | grep -i editable   # must print NOTHING
```

The topology is **per-machine, not universal** — on a dev box (e.g.
elitebook) `~/.local/bin/coord` is commonly an *editable* install pointing at
a checkout, which would let a worker's own branch churn rewrite the runner
mid-run. That is fine for interactive use; it is not safe under an unattended
timer. Install `coord-drive-queue.timer` only where the verify step above
comes back non-editable.

## 6. The deadline trap (#1660)

An expired `coord drive --deadline` (default 240 minutes; `drive-batch.sh`
uses 120) stops the **observer**, not the work — the fleet carries Test,
Review, and Merge through to completion regardless, exactly as described in
[`docs/OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md#9-the-unattended-driver-coord-drive-and-scriptsdrive-batchsh).

The queue's `tick` is deliberately built so this does not cause it to launch
on top of invisible live work: capacity is counted from **board state**
(whether the work is still `ACTIVE`), not from a session count. A drive whose
observer already exited on its deadline still occupies a queue slot — the
tick's reconcile step reports it `held` with reason `"drive session is gone
but work is still ACTIVE on the board (observer deadline, #1660) — still
occupying a machine"`.

The trap for an operator is the opposite direction: **`coord drive-sessions`
only lists live tmux sessions**, so once the observer has exited on its
deadline, that drive is invisible to `coord drive-sessions` even though it is
still occupying a queue slot and the fleet is still actively working it. Do
not use `coord drive-sessions`'s count as "how much of the queue is actually
running" — cross-check `coord drive-queue status` (which reflects board
state) instead.

## 7. #1715

See "Read this before queuing more than ~2 issues" above, at the top of this
document.

## 8. #1738

See the same section — it's the smaller, already-partially-fixed version of
the same class of problem (a content-irrelevant base move staling a Test
verdict).

---

## See also

- [`docs/OPERATING_GOTCHAS.md`](OPERATING_GOTCHAS.md) — the deadline trap and
  the `coord-venv` stale-CLI trap in their general form (this doc is the
  drive-queue-specific instance of both).
- [`docs/AGENT_OPERATIONS.md`](AGENT_OPERATIONS.md) — the service/host/unit
  table `coord-drive-queue` has a row in, and the sibling `coord-notify`
  timer walkthrough this one mirrors.
- `scripts/drive-batch.sh` — the tool this replaces for anything past a
  one-off foreground run; its header explains when to still reach for it.
