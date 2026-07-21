# claude-coordinator

Coordinate a fleet of Claude Code workers — and human-attended interactive `claude` sessions — from a single board.

Claude Code is great at one task at a time. Real projects have dozens of issues, spread across repos, that each want their own session, their own scoping, and their own review. `claude-coordinator` runs many workers in parallel — on one machine or across several over Tailscale — behind a coordinator that picks the model, avoids file conflicts, routes by capability, and drives the full **Work → Test → Review → Merge** pipeline. Each stage can run as a cheap `claude -p` worker *or* as a human-attended interactive session you launch and steer from the board.

## The problem

Running one Claude Code session at a time is a bottleneck. You context-switch between issues, lose session state, and can't parallelize. A complex issue gets one shot; if the session dies mid-flight, you start over. There's no audit trail, no conflict detection, and no way to see what happened last Thursday.

## The solution

One config file describes your repos and machines. Workers run in isolated git worktrees so they never step on each other. The coordinator tracks what's in flight, prevents conflicts, sequences PRs, and moves each issue through a gated pipeline. You approve the decisions; the workers do the work.

Works on **one machine** with multiple worktrees. Add more machines over Tailscale when you want true parallelism, capability routing (a GTK box, a browser box), or a remote review.

## How it works

```
        ~/.coord/coord.db (SQLite)  ·  coordinator.yml  ·  GitHub (issues / PRs / comments)
                                        ▲
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
          coord CLI                coord-tui                 coord web
          (Python)                 (Rust board)              (phone PWA + REST)
              │                         │                         │
              └──────────── coord serve ─┴─ (optional daemon, port 7435) ──┘
                                        │  canonical board for thin clients
                                        │
                                        │  HTTP (port 7433)
                                        ▼
                                ┌────────────────┐
                                │  coord agent   │  one per machine
                                │  (HTTP server) │
                                └───────┬────────┘
                                        │ spawns
                        ┌───────────────┴───────────────┐
                        ▼                               ▼
              claude -p worker                 interactive claude session
              (headless, isolated worktree)    (human-attended, tmux, #437)
```

Three kinds of process:

1. **Coordinator clients** — the CLI (`coord`), the terminal board (`coord-tui`), and the web dashboard / phone PWA (`coord web`). They read shared state and dispatch work. All three are **peers** of the same state, not layers — use whichever fits the task.
2. **Agent servers** — one `coord agent` per worker machine (port 7433). A dumb dispatcher: it spawns and tracks worker processes and owns the worktrees and logs. All the intelligence lives in the coordinator.
3. **Workers** — either a headless `claude -p` subprocess in an isolated worktree, or a human-attended interactive `claude` session in a named tmux session that you drive from the board. Both report their result through the same board seam.

State lives in SQLite (`~/.coord/`) plus GitHub issue comments (the durable message bus — every briefing, completion, failure, and review verdict is a comment carrying a `<!-- coord:event=... -->` marker). Either can reconstruct the other. An optional **control-center daemon** (`coord serve`) fronts one canonical DB so every client on your tailnet renders and drives the same board.

## The pipeline: Work → Test → Review → Merge

Every issue moves through four gated stages:

| Stage | What happens | Automated path | Interactive path |
|-------|--------------|----------------|------------------|
| **Work** | Read the issue, write code, push a branch | `claude -p` worker | `coord assign --interactive` |
| **Test** | Build + run tests on capability-matched hardware; record a verdict | headless smoke assignment | `--smoke-of` testing agent |
| **Review** | A fresh, zero-context session reviews the diff against the repo's rules | `type="review"` worker on a *different* machine | `--review-of` reviewer |
| **Merge** | Rebase onto the base branch, resolve conflicts, run tests, merge | merge queue + auto-rebase | `--merge-of` merge agent |

Two rules shape the flow:

- **Test precedes Review.** The smoke test runs *before* the PR/review — the natural order. Review auto-dispatch is **held** until the work has a `passed`/`skipped` Test verdict. A work item left at *Pending Test* gets no review, so it never merges — this is the single most common reason a story silently stalls. Record the verdict with `coord test <id> --passed|--skipped|--fail`, or the **P / S / F** keys on the Test stage in the TUI. The displayed stage order and this gate both come from `pipeline.default_gates` in `coordinator.yml` (default `[test, review, merge]`).
- **A failed test routes exactly like a request-changes review.** Both drop the issue back to a fix on the *same* branch (`coord fix`, or the interactive `--fix-of`), never an orphan.

`coord notify` drives the automated legs — review-on-completion, fix-on-request-changes, re-review-on-fix. Run it periodically (cron, a TUI timer, or by hand); nothing advances on its own.

## Quick demo

```bash
pip install claude-coordinator     # from PyPI
coord init                         # interactive setup: detects repos, writes coordinator.yml
coord agent &                      # start the agent server (port 7433) — see Quick Start for the systemd setup

coord assign laptop myrepo 42 --model sonnet --briefing "Fix the auth middleware timeout"
# → laptop → myrepo #42: Fix the auth middleware timeout
# →   model: sonnet
# →   dispatched (assignment a1b2c3)

coord watch a1b2c3            # filtered live output (stream-json events)
# → [init]   session a1b2 · model sonnet
# → [tool]   Read auth/middleware.py
# → [tool]   Edit auth/middleware.py
# → [result] completed in 3m · 6 turns · $0.45

coord test a1b2c3 --passed   # record the Test-gate verdict (or run the build+tests locally first)
coord pr a1b2c3              # open the PR + auto-dispatch an adversarial review
# → PR worker dispatched (assignment d4e5f6)  ·  branch issue-42-fix-auth-middleware → main
# → Review dispatched (assignment g7h8i9)      ·  reviewer: server
coord merge                  # once the review approves + CI is green
```

Prefer to drive it by hand? Launch any stage as an interactive session from `coord-tui` (or with `coord assign --interactive --smoke-of / --review-of / --merge-of`) and steer it yourself — see [Driving from coord-tui](#driving-from-coord-tui).

## Quick start

### 1. Install

```bash
pip install claude-coordinator
```

The `coord` CLI is now on your PATH. The same package provides the agent server (`coord agent`), so the coordinator side and the worker side share one install.

> **Developing the coordinator itself?** Clone the repo and `pip install -e .`. Reserve editable installs for development machines — agent machines must always be PyPI installs (see [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md)).

The Rust terminal board, `coord-tui`, ships separately as a locally-built binary — see [Driving from coord-tui](#driving-from-coord-tui).

### 2. Configure

```bash
coord init        # interactive wizard: detects repos in cwd and ~/src/, writes coordinator.yml
coord config      # verify it parsed cleanly (prints the resolved config path)
```

Or copy `coordinator.example.yml` and edit by hand. `coordinator.yml` is gitignored — keep secrets out of version control. Its canonical home is `~/.coord/coordinator.yml` so the tool runs on a machine with no repo checkout.

### 3. Start the agent server

For a quick local trial:

```bash
coord agent &     # port 7433; auto-detects the machine from hostname
```

For anything beyond a trial, use the installer (systemd user service, auto-restart, survives reboots, separate worker logs):

```bash
curl -sSL https://raw.githubusercontent.com/JDonaghy/claude-coordinator/main/install-agent.sh | bash
```

The same script sets up remote worker machines — see [Worker node setup](#worker-node-setup).

### 4. Coordinate

Three peer clients drive the same board — pick per task, they don't conflict:

**`coord-tui` (recommended for interactive use)** — a terminal board with a live pipeline, right-click actions, and the one-key stage-to-stage handoffs. See [Driving from coord-tui](#driving-from-coord-tui).

**The `coord` CLI directly:**

```bash
coord status                              # machines, assignments, connectivity
coord assign laptop myrepo 42 --model sonnet --briefing "Fix the auth bug"
coord watch <id>                          # live filtered output
coord test <id> --passed                  # record the Test-gate verdict
coord pr <id>                             # open the PR + adversarial review
coord notify                              # drive the auto-loop (run periodically)
coord merge                               # open + merge PRs in sequence
```

**The `/coordinator` slash command in Claude Code** — open Claude Code in the repo and type `/coordinator` for guided setup, triage, dispatch, monitoring, and PR creation (`.claude/commands/coordinator.md`).

To share one board across every Tailscale host, run the control-center daemon (`coord serve`, port 7435) on an always-on machine; the CLI and TUI then read from it as thin clients. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Driving from coord-tui

`coord-tui` is the rich terminal board: a live pipeline view, a machines panel, an embedded terminal for interactive sessions, SSE log tailing, and a per-issue usage view. Interaction is **right-click-first** — right-click a pipeline row for its context menu; `?` opens a help overlay and command palette. It's distributed as a locally-built Rust binary (it links `quadraui` by path), so after a `tui/` change:

```bash
cd tui && cargo build && cp target/debug/coord-tui ~/.local/bin/coord-tui
```

### Board-driven stage handoffs

The whole lifecycle can be driven as **human-attended interactive sessions** launched from a pipeline row's right-click menu. Verdicts are always recorded through the board (`coord report-result` / `coord test`), never scraped from the session's terminal:

- **Start testing** → launches an interactive testing agent (`--smoke-of`) read-only in the live checkout; it lists the smoke plan, offers `coord pull-artifact`, and records `coord test --passed|--fail`.
- A `passed`/`skipped` test arms **pass → review**, which launches the interactive reviewer (`--review-of`).
- An approved review arms **Start merge** (`--merge-of`): a merge agent worktrees the branch, proactively rebases onto the base branch, resolves mechanical conflicts (escalating semantic ones to you), runs tests, `git push --force-with-lease`, then `coord verify-merge` and `coord merge`.
- A failed test **or** a request-changes review offers one-key **Fix** (`--fix-of`) on the same branch.

Each stage also has a `claude -p` automation peer in the **Start (automated)** submenu, so you can mix attended and headless per stage. Review and Fix can run on a **remote** machine over ssh+tmux (pick the machine from the card) — useful for reviewer independence or capability routing.

### Keyboard shortcuts

Right-click is the primary surface; these keys are the fast paths. Press `?` for the full, always-current list.

| Key | Where | Action |
|-----|-------|--------|
| `j` / `k` | Pipeline | Navigate issues |
| `Enter` | Pipeline | Fire the active pipeline action (`[Go]` / `[Retry]`) |
| `m` / `M` | Pipeline | Merge the selected issue's PR (surfaces blocked-on-review / blocked-on-CI as toasts) |
| `P` / `S` / `F` | Test stage | Record Test verdict: pass / skip / fail |
| `f` | Pipeline | Bounce after a request-changes review (re-runs the fix against cached findings) |
| `B` | Pipeline | Pull the branch + run build & tests locally |
| `W` | Pipeline | Watch overlay (SSE log tail of the active worker) |
| `L` | All | Sessions panel (running interactive sessions: attach / kill / reap) |
| `R` | All | Immediate refresh from GitHub (the background poll runs every 60s) |
| `u` / `r` / `c` | Machines | Update / restart the agent · clean stale worktrees |

## Milestones & epics (Pipeline v2)

For work bigger than one issue, group issues under an **epic** and drive the whole thing as one unit. An epic is a GitHub tracking issue (carrying the `epic` label) whose body holds a `## Work order` block — a DAG of child issues written as `- #762 {group: A, after: #761}`, where `group` is a parallel cohort and `after` is a hard dependency. Membership is backed by GitHub's native sub-issues API. The issue pipeline (above) nests inside a **milestone pipeline** so expensive gates are paid once per milestone, not once per issue:

| Gate | Command | What it checks |
|------|---------|----------------|
| **A — contract** | `coord acceptance mock` | A mock-first, black-box acceptance contract exists before any issue dispatches (repos with an `acceptance.drivers` entry). |
| **B — architecture** | `coord milestone gate-b` | An independent review confirms the *assembled* milestone was built to the Gate-A contract. |
| **C — acceptance** | `coord milestone gate-c` | The full accumulated acceptance suite is green (catches integration gaps between issues). |
| **D — ship** | `coord milestone ship` | Merges the milestone's `feature/ms-NN` branch to `develop`, gated on Gate B (approved) + Gate C (re-run live). |

Drive a milestone with the `coord milestone` group:

```bash
coord milestone chat myrepo --new            # steward session: draft the milestone + Work order DAG
coord milestone write-order myrepo <epic>    # validate + write the ## Work order block
coord milestone dispatch myrepo <epic>       # dispatch the ready frontier; drain as `after` deps clear
coord milestone gate-b myrepo <epic>         # architecture review of the assembled result
coord milestone gate-c myrepo <epic>         # full acceptance suite
coord milestone ship  myrepo <epic>          # Gate D → merge feature/ms-NN into develop
```

**Branch model (opt-in).** Set `develop_branch:` on a repo to enable the develop + feature-branch flow: issues in a milestone branch off `feature/ms-NN`, merge back into it, and reach `develop` only via `coord milestone ship`. Repos that don't set `develop_branch` keep the default single-branch (`default_branch`) flow unchanged. `develop → main` is a separate release cut, not automated by `ship`.

See [`docs/PIPELINE_V2.md`](docs/PIPELINE_V2.md) and [`docs/ORACLE_LOOP.md`](docs/ORACLE_LOOP.md) for the full model.

## Command reference

`coord <cmd> --help` documents every command and flag. This is the curated set; the CLI has more (`coord --help`).

### Core workflow

| Command | Description |
|---------|-------------|
| `coord plan [--dry-run]` | Brain proposes assignments for idle machines |
| `coord approve <IDs> [--dry-run] [--auto-pull] [--skip-freshness]` | Dispatch approved proposals (comma-separated) |
| `coord assign <machine> <repo> <issue> [--model haiku\|sonnet\|opus] [--briefing TEXT\|--briefing-file F] [--dry-run]` | Direct dispatch, bypasses the brain |
| `coord status [--machine NAME] [--freshness] [--no-reconcile]` | Machines, assignments, connectivity |
| `coord watch <id> [--all]` | Filtered live log (stream-json events) |
| `coord wait <id>` | Block until an assignment completes |
| `coord log <id> [-f] [--machine NAME] [--local]` | Raw `claude -p` output |

### Post-completion & merge

| Command | Description |
|---------|-------------|
| `coord test <id>` | Pull the worker's branch locally, run build + tests |
| `coord test <id> --passed \| --skipped \| --fail --reason "..."` | Record the Test-gate verdict |
| `coord pr <id> [--no-review]` | Open a PR (auto-dispatches an adversarial review unless `--no-review`) |
| `coord fix <id> [--guidance "..."]` | Dispatch a fix-up worker for a failed test (auto-escalates model) |
| `coord bounce <review-id>` | Bounce back to Work after a request-changes review (uses cached findings) |
| `coord notify` | Poll agents, post GitHub comments, drive the auto-loop |
| `coord merge [--dry-run] [--plan] [--repo N] [--method rebase\|squash\|merge] [--order IDs \| --only ID]` | Process the merge queue |
| `coord merge --force-merge \| --skip-review \| --skip-smoke` | Override the CI / review / smoke gate for a merge |
| `coord merge --only <id> --override-human-required "<reason>"` | Audited override of a HUMAN_REQUIRED entry |
| `coord verify-merge <work-id>` | Self-check a `--merge-of` rebase before reporting done |
| `coord reconcile-merges [--repo N] [--dry-run]` | Backfill branches + record out-of-band merges |

### Interactive session driving

All of these take `coord assign --interactive` and record their verdict through the board (`coord report-result` / `coord test`). Read-only flavours run in the live checkout with no worktree; writing flavours use an isolated worktree and push back.

| Flag on `coord assign --interactive` | What it launches |
|---|---|
| *(none)* | A human-attended Work session with the briefing pre-filled |
| `--plan-only` | A read-only planning session (structured plan, no branch) |
| `--smoke-of <work-id>` | A testing agent (records `coord test`) |
| `--review-of <work-id>` | A reviewer (records `coord report-result --verdict`) |
| `--merge-of <work-id>` | A merge agent (worktree, proactive rebase, `verify-merge`, `coord merge`) |
| `--fix-of <id>` | A fix on the existing branch — takes a request-changes review id **or** a test-failed work id |
| `--rework-of <id\|branch>` | Continue an existing branch with a fresh `--briefing` |
| `--troubleshoot` / `--chat` | Read-only diagnostic / issue-chat session |
| `--audit-of <epic>` / `--milestone-chat-of <epic>` | Milestone outcome audit / milestone-steward chat |

Verdict-relay helpers: `coord report-result`, `coord set-review-findings`, `coord fix-briefing`, `coord reattach <id>`, `coord inject <id> <text>`.

### Milestones & epics

| Command | Description |
|---------|-------------|
| `coord milestone create\|edit\|assign\|remove` | Manage the native GitHub milestone + issue membership |
| `coord milestone add-child\|sync` | Manage epic sub-issue membership (checklist → live sub-issues API) |
| `coord milestone chat [--new]` | Steward session to draft the milestone + `## Work order` |
| `coord milestone order\|write-order` | Read / validate + write the `## Work order` DAG |
| `coord milestone dispatch [--dry-run\|--next\|--pick N]` | Dispatch the ready frontier into the pipeline |
| `coord milestone gate-b\|gate-c\|ship` | Architecture review · acceptance suite · Gate-D ship to `develop` |
| `coord plans [--repo N] [--json]` | Cross-repo milestone roster |
| `coord acceptance mock\|author\|run\|record` | Gate-A contract, sealed-suite authoring + runs (oracle loop) |

### Observability

| Command | Description |
|---------|-------------|
| `coord usage [--today\|--week\|--month\|--since S] [--by-issue\|--issue N\|--by repo\|week\|month\|issue] [--by-time]` | Per-issue/repo/window cost, tokens, and time-spent |
| `coord audit [--category C] [--repo N] [--issue N] [--since T] [--json]` | Query the durable, ordered event log |
| `coord sessions [--remote] [--prune] [--reap-merged]` | List interactive tmux sessions; reap dead/merged ones |
| `coord terminal new\|list\|kill\|attach` | Persistent fleet-wide shell sessions |

### Recovery & lifecycle

| Command | Description |
|---------|-------------|
| `coord retry <id>` | Re-dispatch a failed assignment to a different machine |
| `coord stop <id>` | Cancel a running assignment |
| `coord resume-stuck <id> --guidance "..."` | Cancel a stuck worker, dispatch a continuation |
| `coord resume` | Reconcile board state after a crash |
| `coord diagnose [repo issue] [--stage S] [--reset] [--orphan-worktrees]` | Diagnose / recover a stuck pipeline stage or sweep orphaned worktrees |
| `coord done` | End the session, run housekeeping hooks, show a summary |

### Setup & diagnostics

| Command | Description |
|---------|-------------|
| `coord init` / `coord config` | Interactive setup / pretty-print the parsed config |
| `coord agent [--machine N] [--host H] [--port P]` | Start the agent server (default 7433) |
| `coord agent update\|restart\|clean-worktrees [--machine N \| --all]` | Manage remote agents |
| `coord web [--host H] [--port P]` | Web dashboard + phone PWA (default 7434) |
| `coord serve [--host H] [--port P]` | Control-center board daemon (default 7435) |
| `coord sync [--quiet]` | Sync open issues from GitHub into the local cache |
| `coord pause <machine>` / `coord unpause <machine>` | Stop / resume routing to a machine |
| `coord track\|untrack\|backlog <repo> <issue>` | Move an issue into / out of the Pipeline |
| `coord version` | Print the version |

### Model tiers

| Flag | Use for |
|------|---------|
| `--model haiku` | Docs, config, trivial single-file changes |
| `--model sonnet` | Standard features, bug fixes (default) |
| `--model opus` | Complex multi-file or architectural work |

`coord fix` escalates to the next tier on failure. Configure the ladder in `models.escalation` and pin exact model ids per alias with `models.versions`.

### Ports

| Port | Service |
|------|---------|
| 7433 | `coord agent` — per-machine worker dispatcher |
| 7434 | `coord web` — web dashboard + phone PWA |
| 7435 | `coord serve` — control-center board daemon |

## Configuration

### Minimal single-machine config

```yaml
# coordinator.yml
repos:
  - name: my-project
    github: owner/my-project
    default_branch: main
    build_command: "pytest"
    test_command: "pytest"

machines:
  - name: laptop
    host: localhost              # single machine: localhost works fine
    capabilities: [python]
    repos: [my-project]
    repo_paths:
      my-project: ~/src/my-project

concurrency:
  max_workers: 3                 # how many worker sessions run at once
  stagger_seconds: 30            # delay between dispatches (avoids rate limits)

models:
  default: sonnet
  escalation: [haiku, sonnet, opus]
  labels:                        # assign model by GitHub issue label
    documentation: haiku
    architecture: opus
```

### Full reference

```yaml
repos:
  - name: api-gateway
    github: acme/api-gateway
    depends_on: []               # blocks dispatch if listed repos have active work
    default_branch: main
    develop_branch: develop      # opt in to the milestone feature-branch model (see Milestones)
    build_command: "npm run build"
    test_command: "npm test"
    artifact_paths:              # stash built binaries so `coord pull-artifact` can rsync them
      - dist/

  - name: shared-lib
    github: acme/shared-lib

machines:
  - name: laptop
    host: localhost
    capabilities: [python, node]
    repos: [api-gateway, shared-lib]
    repo_paths:
      api-gateway: ~/src/api-gateway
      shared-lib: ~/src/shared-lib

  - name: server                 # second machine (Tailscale hostname)
    host: server.tailnet
    capabilities: [docker, python, browser]
    repos: [shared-lib]
    repo_paths:
      shared-lib: ~/src/shared-lib

concurrency:
  max_workers: 2
  stagger_seconds: 30
  backoff_base: 60
  max_retries: 3
  auto_reassign: false           # auto-retry failed assignments on a different machine
  # interactive_session_timeout_hours: 12   # SSH-probe idle remote interactive sessions (#588)

pipeline:
  default_gates: [test, review, merge]   # displayed stage order + the test-before-review gate

models:
  default: sonnet
  escalation: [haiku, sonnet, opus]
  labels:
    documentation: haiku
    architecture: opus
  # versions:                    # pin an alias to an exact model id passed to `claude -p --model`
  #   sonnet: claude-sonnet-5

reviews:
  enabled: true                  # adversarial review on completion (set false to opt out)
  auto_dispatch: true
  checklist:
    - "Did the worker add tests?"
    - "Did the worker stay within file scope?"
  repo_overrides:
    api-gateway:
      - "Check rate limiting on new endpoints"

smoke_tests:
  auto_queue: true               # auto-dispatch a headless Test when Work completes
  capability_rules:
    - files: [src/gtk/]
      requires: [gtk]            # route GTK changes to a machine with the gtk capability

ci_store:
  type: github                   # gate merges on `gh pr checks`; `type: none` disables the gate

# acceptance:                    # per-repo sealed-oracle drivers for the milestone acceptance gates
#   drivers:
#     api-gateway: { type: web-playwright }

hooks:
  on_round_complete: [summary_report]
  on_session_end:    [summary_report]
```

`coordinator.yml` is gitignored; `coordinator.example.yml` is the checked-in reference. Config resolves in order: `$COORD_CONFIG` → `~/.coord/coordinator.yml` (the canonical home, so a machine needs no repo checkout) → `./coordinator.yml` (a development fallback). `coord config` and `coord serve` print the resolved path so it's never ambiguous which file loaded.

## Pipeline lifecycle (`status:*` labels)

The Pipeline organizes GitHub issues into lifecycle sections from their labels and assignment state. Membership is **label-driven** — an open issue with a `status:ready` label shows as a Pipeline card even with zero assignments (only issues carrying the `coord` label appear at all).

| Section | Condition |
|---|---|
| **New** | Open issue, no `status:*` label |
| **Refining** | Label `status:refining` |
| **Pending** | Label `status:ready`, no assignment yet |
| **In-progress** | At least one assignment row (any status) |
| **Done** | Issue is closed on GitHub |

Transitions: `coord refine` → `coord ready` marks an issue New → Refining → Pending; `[Go]` (or `coord assign`) starts it; merging a PR with `Closes #N` closes the issue. To drop a card back to the Board, `coord backlog <repo> <issue>` (strips `status:*`). Epics add a milestone tier on top of these sections — see [Milestones & epics](#milestones--epics-pipeline-v2).

## Features

- **No API key** — uses `claude -p` on your Max/Pro subscription; billing stays per-seat.
- **Two ways to run every stage** — cheap headless `claude -p` workers *or* human-attended interactive sessions you launch and steer from the board. `claude -p` is a first-class automation path, not a deprecated one.
- **Single-machine first** — one agent server, many workers in isolated git worktrees; no Tailscale needed. Add machines for parallelism, capability routing, or remote review.
- **Gated pipeline** — Work → Test → Review → Merge, with Test gating Review and CI/review/smoke gating Merge.
- **Model tiering** — haiku for docs, sonnet for standard work, opus for architecture; `coord fix` auto-escalates on failure.
- **Adversarial review + auto-loop** — a fresh, zero-context session reviews the diff against the repo's rules; request-changes dispatches a fix pinned to the same branch, then re-reviews. Up to 3 iterations before asking for human judgment. Independence comes from a fresh session, not separate hardware.
- **Milestones & epics** — group issues under an epic with a `## Work order` DAG; amortize architecture and acceptance gates across the milestone; ship as one unit (`coord milestone ship`).
- **Merge queue** — dependency-aware sequencing, CI gating (`gh pr checks`), auto-rebase of mechanical conflicts, and escalation of semantic ones.
- **Capability-aware testing** — `smoke_tests.capability_rules` route platform-specific suites to capable hardware (a GTK box, a browser box).
- **Observability** — `coord usage` (per-issue/repo/window cost, tokens, time), `coord audit` (durable event log), stream-json `coord watch`, and `STATUS:`/`STUCK:` progress lines.
- **Crash recovery** — `coord resume` / `coord diagnose` reconcile the board with live agent and git state; interactive tmux sessions survive a TUI crash and are reattachable.
- **Web dashboard + phone PWA** — a board view and a React/Vite phone control-center at port 7434, served over Tailscale.

## Why this works (even with one machine)

The tool encodes a pattern from real multi-agent sessions: **separate the tech lead from the IC.** The coordinator thinks about *what to do next* — priority, dependencies, conflicts, which machine is idle. Workers think about *how to do this one thing*. Neither is distracted by the other's concern.

- **Forced scoping.** One issue per worker session. No "while I'm here, let me also refactor this."
- **Structured handoffs.** Every assignment is a briefing posted as a GitHub comment. If a session dies, a new one resumes from the comment — zero context loss.
- **Persistent record.** Every decision, briefing, verdict, and result lives on GitHub. Review what happened a week later; terminal scrollback is gone when the window closes.
- **Fresh eyes.** Each worker starts with no prior context. Adversarial review takes it further: a separate session reviews with zero shared context — even on the same machine.
- **Human stays strategic.** You approve assignments and make judgment calls; you don't ferry messages between terminals or track who's touching which file.
- **Cost discipline.** Model tiering means no opus prices for a docs fix; auto-escalation starts cheap and pays more only when needed.

## Scaling up

1. On the new machine, run the installer (venv + systemd service in one shot):
   ```bash
   curl -sSL https://raw.githubusercontent.com/JDonaghy/claude-coordinator/main/install-agent.sh | bash -s -- --machine <name>
   ```
   No git clone needed — `install-agent.sh` pulls from PyPI.
2. Add the machine to `coordinator.yml` under `machines:` with its Tailscale hostname and capabilities.
3. `coord status` from the coordinator machine shows all machines and their connectivity.

For Tailscale setup, see [tailscale.com/kb](https://tailscale.com/kb/). The agent server only needs port 7433 reachable on the tailnet.

## Worker node setup

To add a worker machine (no repo checkout needed):

```bash
curl -sSL https://raw.githubusercontent.com/JDonaghy/claude-coordinator/main/install-agent.sh | bash
# or with options:
curl -sSL https://raw.githubusercontent.com/JDonaghy/claude-coordinator/main/install-agent.sh | bash -s -- --machine myserver --port 7433
```

This installs coord, sets up a systemd service with auto-restart, and starts the agent. Then add the machine to `coordinator.yml` and run `coord status` to verify connectivity.

## Upgrading agents

Check a running agent's version:

```bash
curl -s http://<host>:7433/health | python3 -c "import sys,json; print(json.load(sys.stdin).get('version'))"
```

Trigger a remote self-update (installs the latest PyPI version and restarts):

```bash
coord agent update --machine <name>      # or --all
```

If `/update` fails or the version doesn't advance, the most common cause is an old editable (`pip install -e .`) install on the agent machine — convert it to a PyPI install with the recipe in [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md). Read that doc end-to-end before touching any agent install.

## Troubleshooting

**Agent won't start** — port in use (`lsof -i :7433`, or `--port`), or a hostname mismatch (`socket.gethostname()` vs `coordinator.yml`; pass `--machine NAME`).

**"connection refused" in `coord status`** — agent not running, Tailscale down (`tailscale status`), or a firewall rule.

**Worker fails immediately** — wrong `repo_paths` (check `coord config`), `gh` not authenticated (the *coordinator* uses `gh`; workers do not — `gh auth status`), or the `claude` CLI not on PATH on the agent machine.

**A story won't merge / "Go does nothing"** — almost always the **Test gate**: no review is dispatched until the work has a `passed`/`skipped` verdict. Record it (`coord test <id> --passed`, or **P/S** in the TUI). Other gates: review not approved, CI red (`--force-merge` overrides), a PR conflict (a conflict-fix worker runs invisibly), or a queue clog. Full gate-by-gate checklist: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#when-a-merge-isnt-happening).

**An issue you never dispatched is in the Pipeline** — it carries a `status:ready` label. Drop it back with `coord backlog <repo> <issue>`. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#when-an-issue-is-sitting-in-the-pipeline-you-never-dispatched).

**Board state issues** — `coord resume` reconciles the board with live agent state and garbage-collects stale entries.

## Requirements

- Python 3.12+
- Claude Code CLI with a Max or Pro subscription
- `gh` CLI (authenticated, for coordinator-side GitHub operations)
- Rust toolchain — only to build `coord-tui`
- Tailscale — optional, only for multi-machine setups

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the clients, agents, daemon, and workers fit together; the agent HTTP API; the auto-loop end to end; the merge-gate checklist.
- [`docs/PIPELINE_V2.md`](docs/PIPELINE_V2.md) / [`docs/ORACLE_LOOP.md`](docs/ORACLE_LOOP.md) — the two-tier milestone pipeline and the sealed-acceptance oracle loop.
- [`docs/PHONE_WEBAPP.md`](docs/PHONE_WEBAPP.md) — build + serve the phone control-center PWA over Tailscale.
- [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md) — agent install, upgrade, editable-drift recovery, and releasing to PyPI.

## Releasing a new version

1. Bump the version in `coord/__init__.py` and `pyproject.toml` (both must match).
2. Commit, then tag and push:
   ```bash
   git tag vX.Y.Z && git push origin main vX.Y.Z
   ```
3. GitHub Actions (`publish.yml`) builds and publishes to PyPI using the `PYPI_API_TOKEN` secret.
4. After the publish completes, upgrade remote agents — `coord agent update --all`.

`coord-tui` ships as a locally-built binary, not via PyPI — rebuild and reinstall it after a `tui/` change (see [Driving from coord-tui](#driving-from-coord-tui)).
