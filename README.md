# claude-coordinator

Coordinate multiple Claude Code workers from a single terminal.

Claude Code is great for one task at a time. But real projects have dozens of issues. This tool lets you run multiple Claude Code workers in parallel — even on a single machine — with a coordinator that picks the right model, avoids file conflicts, and handles the full issue-to-PR pipeline.

## The Problem

Running one Claude Code session at a time is a bottleneck. You context-switch between issues, lose session state, and can't parallelize. Complex issues get one shot; if the session dies mid-flight, you start over. There's no audit trail, no conflict detection, and no way to see what happened last Thursday.

## The Solution

One config file describes your repos and machines. Workers run in isolated git worktrees so they never step on each other. The coordinator tracks what's in flight, prevents conflicts, routes by capability, and sequences PRs.

Works on **one machine** with multiple worktrees. Add more machines over Tailscale when you need true parallelism.

```
You (coordinator)
  │
  ├── coord assign → Agent Server (localhost:7433)
  │                    ├── Worker 1 (worktree A) → claude -p --model sonnet
  │                    ├── Worker 2 (worktree B) → claude -p --model haiku
  │                    └── Worker 3 (worktree C) → claude -p --model opus
  │
  └── coord watch/test/pr/fix
```

## Quick Demo

```bash
pip install claude-coordinator     # from PyPI
coord init                         # interactive setup: detects repos, writes coordinator.yml
coord agent &                      # start the agent server (port 7433) — see "Quick Start" for the systemd setup

coord assign laptop myrepo 42 --model sonnet --briefing "Fix the auth bug"
# → laptop → myrepo #42: Fix the auth middleware timeout
# →   model: claude-sonnet-4-6
# →   dispatched (assignment a1b2c3)

coord watch a1b2c3            # filtered live output (stream-json events)
# → [init] claude-sonnet-4-6 session a1b2
# → [tool] Read auth/middleware.py
# → [tool] Edit auth/middleware.py
# → [result] completed in 3m, 6 turns, $0.45

coord pr a1b2c3               # dispatch a worker to create the PR
# → PR worker dispatched (assignment d4e5f6)
# →   branch: issue-42-fix-auth-middleware → main
# → Review dispatched (assignment g7h8i9)
# →   reviewer: laptop
```

## Quick Start

### 1. Install

```bash
pip install claude-coordinator
```

That's it. The `coord` CLI is now on your PATH. The same package provides the agent server (`coord agent` subcommand), so you don't need separate installs for the coordinator side and the worker side.

> **Developing the coordinator itself?** Clone the repo and `pip install -e .` instead. Reserve editable installs for development machines; agent machines should always be PyPI installs (see [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md)).

### 2. Configure

```bash
coord init        # interactive wizard: detects repos in cwd and ~/src/, writes coordinator.yml
coord config      # verify it parsed cleanly
```

Or copy `coordinator.example.yml` and edit it by hand. `coordinator.yml` is gitignored — keep secrets out of version control.

### 3. Start the agent server

For a quick local trial:

```bash
coord agent &     # runs on port 7433; auto-detects machine from hostname
```

For anything beyond a quick trial, use the installer script (sets up a systemd user service with auto-restart, survives reboots, separates worker logs from your shell):

```bash
curl -sSL https://raw.githubusercontent.com/JDonaghy/claude-coordinator/main/install-agent.sh | bash
```

The script also works on remote worker machines — see [Worker Node Setup](#worker-node-setup).

### 4. Coordinate

You have three ways to drive the coordinator:

**Option A — `coord-tui` (recommended for interactive use):**

A terminal UI with live status, a pipeline view, keyboard-driven merge/bounce/watch, and SSE log tailing. Distributed as a separate Rust binary; see the `tui/` directory for build instructions. Once installed, run `coord-tui` from your project root. Pipeline keybinds are in the [Pipeline Lifecycle](#pipeline-lifecycle-status-labels) section.

**Option B — The `coord` CLI directly:**

```bash
coord status                              # verify agent is reachable
coord assign laptop myrepo 42 --model sonnet --briefing "Fix the auth bug"
coord watch <id>                          # live filtered output
coord test <id>                           # pull branch, run build + tests
coord test --passed <id>                  # record result
coord pr <id>                             # dispatch PR-creation worker + adversarial review
coord notify                              # post completion comments, trigger the auto-loop (run periodically)
coord merge                               # open PRs and merge in sequence
```

**Option C — The `/coordinator` slash command in Claude Code:**

Open Claude Code in the repo, type `/coordinator`. It handles first-time setup, issue triage, dispatch, monitoring, smoke tests, and PR creation. The slash command is at `.claude/commands/coordinator.md`.

The CLI and the TUI are peer clients of the same state (SQLite + `coordinator.yml` + GitHub) — use whichever you prefer for any given operation; they don't conflict. To drive one shared board from any Tailscale host, an optional control-center daemon (`coord serve`, port 7435) fronts the canonical DB so every client renders the same state. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full picture.

## Worker Node Setup

To add a worker machine (no repo checkout needed):

```bash
curl -sSL https://raw.githubusercontent.com/JDonaghy/claude-coordinator/main/install-agent.sh | bash
```

Or with options:

```bash
curl -sSL https://raw.githubusercontent.com/JDonaghy/claude-coordinator/main/install-agent.sh | bash -s -- --machine myserver --port 7433
```

This installs coord, sets up a systemd service with auto-restart, and starts the agent.
Then add the machine to your coordinator.yml and run `coord status` to verify connectivity.

## Upgrading Agents

### Check current agent version

```bash
curl -s http://<host>:7433/status | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('version','<0.3.0 (old)'))"
```

### Agents >= 0.3.0 — remote self-update

```bash
curl -X POST http://<host>:7433/update
```

The agent installs the latest version from PyPI and restarts itself. The HTTP 202 response is returned immediately; the upgrade runs in the background (~10–30 seconds). Wait a moment, then re-check the version.

### Agents < 0.3.0 — manual upgrade

Old agents (installed before 0.3.0) don't have the `/update` endpoint. SSH in to each machine and run:

```bash
# If installed via install-agent.sh (venv + systemd user service):
~/.coord-venv/bin/pip install --upgrade claude-coordinator && systemctl --user restart coord-agent

# Alternatively, re-run the installer (it's idempotent):
curl -sSL https://raw.githubusercontent.com/JDonaghy/claude-coordinator/main/install-agent.sh | bash
```

After upgrading, verify with `curl -s http://<host>:7433/status | python3 -c "import sys,json; print(json.load(sys.stdin).get('version'))"`.

**Why this matters:** agents older than 0.3.0 reject `coord assign --force` with a 400 error (`unexpected keyword argument 'fresh_branch'`).

### When `/update` fails or the version doesn't advance

See [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md) for diagnostics and recovery. The most common cause is an old editable (`pip install -e .`) install on the agent machine — convert it to a PyPI install with the recipe in that doc.

## Command Reference

### Core Workflow

| Command | Description |
|---------|-------------|
| `coord plan [--dry-run]` | Brain proposes assignments for idle machines |
| `coord approve <IDs> [--dry-run] [--auto-pull] [--skip-freshness]` | Dispatch approved assignments (comma-separated IDs) |
| `coord assign <machine> <repo> <issue> [--model haiku\|sonnet\|opus] [--briefing TEXT] [--dry-run]` | Direct dispatch, bypasses the brain |
| `coord status [--machine NAME] [--freshness]` | Show all machines, assignments, connectivity |
| `coord watch <id> [--all]` | Filtered live log output (stream-json events) |
| `coord wait <id>` | Block until assignment completes |
| `coord log <id> [-f] [--machine NAME] [--local]` | Raw `claude -p` output for an assignment |

### Post-Completion

| Command | Description |
|---------|-------------|
| `coord test <id>` | Pull worker's branch locally, run build + tests |
| `coord test --passed <id>` | Record smoke test as passed |
| `coord test --fail <id> --reason "..."` | Record smoke test as failed |
| `coord pr <id> [--no-review]` | Dispatch a worker to create a PR (auto-dispatches adversarial review unless --no-review) |
| `coord fix <id> [--guidance "..."]` | Dispatch a fix-up worker for a failed smoke test (auto-escalates model) |
| `coord bounce <review-id>` | Bounce the pipeline back to Work after a `request-changes` review (uses cached findings from the DB) |
| `coord notify` | Poll agents, post completion/failure comments to GitHub, drive the auto-loop |
| `coord merge [--dry-run] [--repo NAME] [--method rebase\|squash\|merge] [--order IDs]` | Process merge queue |
| `coord split <IDs>` | Create sub-issues from split proposals |

### Recovery and Lifecycle

| Command | Description |
|---------|-------------|
| `coord resume-stuck <id> --guidance "..."` | Cancel a stuck worker, dispatch a continuation with guidance |
| `coord retry <id>` | Re-dispatch a failed assignment to a different machine |
| `coord stop <id>` | Cancel a running assignment |
| `coord resume` | Recover board state after crash, reconcile with agents |
| `coord done` | End session, run housekeeping hooks, show summary |

### Setup and Diagnostics

| Command | Description |
|---------|-------------|
| `coord init` | Interactive setup: detects repos, writes coordinator.yml |
| `coord agent [--machine NAME] [--host HOST] [--port PORT]` | Start agent server (default port 7433) |
| `coord web [--host HOST] [--port PORT]` | Start web dashboard (default port 7434) |
| `coord config` | Pretty-print parsed coordinator.yml |
| `coord version` | Print version |

### Model Tiers

| Flag | Use for |
|------|---------|
| `--model haiku` | Docs, config, trivial single-file changes |
| `--model sonnet` | Standard features, bug fixes (default) |
| `--model opus` | Complex multi-file or architectural work |

`coord fix` automatically escalates to the next tier on failure. Configure the ladder in `models.escalation`.

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
  max_workers: 3                 # how many claude -p sessions can run at once
  stagger_seconds: 30            # delay between dispatches (avoids rate limits)

models:
  default: sonnet                # model used when --model not specified
  escalation: [haiku, sonnet, opus]  # coord fix escalates through this list
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
    build_command: "npm run build"
    test_command: "npm test"

  - name: user-service
    github: acme/user-service
    depends_on: [shared-lib]     # won't dispatch if shared-lib has active work

  - name: shared-lib
    github: acme/shared-lib

machines:
  - name: laptop
    host: localhost              # single machine
    capabilities: [python, node]
    repos: [api-gateway, user-service]
    repo_paths:
      api-gateway: ~/src/api-gateway
      user-service: ~/src/user-service

  - name: server                 # second machine (Tailscale hostname)
    host: server.tailnet
    capabilities: [docker, python]
    repos: [user-service, shared-lib]
    repo_paths:
      user-service: ~/src/user-service
      shared-lib: ~/src/shared-lib

concurrency:
  max_workers: 2
  stagger_seconds: 30
  backoff_base: 60
  max_retries: 3
  auto_reassign: false           # auto-retry failed assignments on a different machine
  stale_threshold: 3             # unreachable poll count before marking stale

models:
  default: sonnet
  escalation: [haiku, sonnet, opus]
  labels:
    documentation: haiku
    architecture: opus

hooks:
  on_round_complete:
    - summary_report
  on_session_end:
    - summary_report

reviews:
  enabled: true                  # adversarial review on completion (default; set false to opt out)
  auto_dispatch: true
  require_approval: false
  checklist:
    - "Did the worker add tests?"
    - "Did the worker stay within file scope?"
  repo_overrides:
    api-gateway:
      - "Check rate limiting on new endpoints"

smoke_tests:
  auto_queue: false
  default_command: "make smoke"
  timeout_seconds: 600
  capability_rules:
    - files: [src/gtk/]
      requires: [gtk]
```

`coordinator.yml` is gitignored. Use `coordinator.example.yml` as the checked-in reference.

The config file resolves in this order: `$COORD_CONFIG` → `~/.coord/coordinator.yml` (the canonical home, so the tool runs on a machine with no repo checkout) → `./coordinator.yml` (a development fallback). `coord config` prints the resolved path so it's never ambiguous which file is loaded.

## Pipeline Lifecycle (`status:*` labels)

The `coord-tui` Pipeline panel organises GitHub issues into five lifecycle sections based on their labels and assignment state:

| Section | Condition |
|---|---|
| **New** | Open issue, no `status:*` label |
| **Refining** | Label `status:refining` on the issue |
| **Pending** | Label `status:ready`, no assignment yet |
| **In-progress** | Has at least one assignment row in the DB (any status) |
| **Done** | Issue is closed on GitHub |

### Transitions

- **New → Refining**: Add label `status:refining` (human or Claude) to mark an issue under active specification
- **Refining → Pending**: Swap label to `status:ready` once the spec is stable enough to dispatch
- **Pending → In-progress**: Press `[Go]` in the TUI (or `coord assign`) — automatic once an assignment row exists
- **In-progress → Done**: Merge the PR; include `Closes #N` in the PR body so GitHub auto-closes the issue

### TUI keyboard shortcuts

| Key | Where | Action |
|-----|-------|--------|
| `j` / `k` | Pipeline | Navigate issues |
| `Enter` | Pipeline | Fire the active pipeline action (`[Go]` / `[Retry]`) |
| `m` / `M` | Pipeline | Merge the selected issue's PR (surfaces blocked-on-review / blocked-on-CI as toasts) |
| `f` | Pipeline | Bounce after a `request-changes` review (re-runs the work against the cached findings) |
| `B` | Pipeline (Done) | Pull branch + run build & tests locally |
| `W` | Pipeline | Open the Watch overlay (SSE log tail of the active worker) |
| `[` / `]` | Stages tab | Focus previous / next stage (mouse click also works) |
| `R` | All | Immediate refresh from GitHub (force poll) |
| `D` | Pipeline | Dismiss a Done-section issue from the panel (session-only) |
| `h` / `l` | Detail | Cycle detail tabs (Pipeline → Issue → Stages) |
| `u` | Machines | Trigger `/update` on the selected agent |
| `c` | Machines | Clean stale worktrees on the selected agent |
| `r` | Machines | Restart the selected agent (graceful, waits for active workers) |

The background GitHub poll runs every **60 seconds**; press `R` to refresh on demand.

## Features

- **No API key** — uses `claude -p` on your Max/Pro subscription; billing stays per-seat
- **Single-machine first** — one agent server, multiple workers in isolated git worktrees; no Tailscale needed
- **Model tiering** — docs get haiku ($0.08/task), standard work gets sonnet, complex work gets opus; `coord fix` auto-escalates on failure
- **Worktree isolation** — workers operate in separate git worktrees; no shared working-tree state between sessions
- **Stream-json observability** — `coord watch` parses `claude -p` stream-json events and shows a clean filtered log
- **Full issue-to-PR pipeline** — `coord assign` → `coord watch` → `coord test` → `coord pr` → `coord merge`
- **`/coordinator` slash command** — open Claude Code, type `/coordinator` for guided operation: setup, triage, dispatch, monitoring
- **Multi-repo** — tracks dependency chains between repos; upstream work blocks downstream dispatch
- **File conflict avoidance** — coordinator ensures no two workers touch the same files simultaneously
- **Claim detection** — checks board and remote `issue-{N}-*` branches before dispatching; refuses duplicates
- **Adversarial reviews + auto-loop** — `coord pr` auto-dispatches an independent `claude -p` session that reviews the diff with zero shared context. If the verdict is `request-changes`, the auto-loop dispatches a fix worker pinned to the original branch; when the fix worker finishes, a fresh review fires automatically. The loop runs up to 3 iterations before asking for human judgment. The whole sequence is driven by `coord notify` — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the end-to-end walk-through. Reviewer independence comes from a fresh session, not separate hardware (it works on a single machine).
- **Smoke testing** — capability-aware routing (GTK changes → machine with GTK)
- **Merge queue** — sequences completed PRs with conflict detection and dependency-aware ordering
- **Progress streaming** — workers emit `STATUS:`/`STUCK:` lines; `coord status` shows real-time progress
- **Failure reassignment** — `coord retry` re-dispatches to a different machine; `auto_reassign` does it automatically
- **Crash recovery** — `coord resume` reconciles board with live agent state after restart
- **Web dashboard + phone PWA** — a lightweight board view at port 7434, plus a React/Vite phone control-center PWA served from the same port

## Why This Works (Even With One Machine)

This tool encodes a pattern discovered through real multi-agent coordination sessions: **separate the tech lead from the IC.**

The coordinator thinks about *what to do next* — priority, dependencies, conflicts, which machine is idle. Workers think about *how to do this one thing* — read the issue, write the code, push the branch. Neither is distracted by the other's concern.

This division of labor produces better results than a single long-running Claude Code session:

- **Forced scoping.** One issue per worker session prevents scope creep. No "while I'm here, let me also refactor this." The worker does one thing and finishes.
- **Structured handoffs.** Every assignment has a briefing posted as a GitHub issue comment. If a session dies, a new one picks up from the comment — zero context loss.
- **Persistent record.** Every decision, briefing, and result lives on GitHub. You can review what happened a week later. Terminal scrollback is gone when the window closes.
- **Fresh eyes.** Each worker starts with no prior context. Adversarial reviews take this further: a separate session reviews the work with zero shared context — even on the same machine.
- **Human stays strategic.** You approve assignments and make judgment calls. You don't ferry messages between terminals or track who's touching which file in your head.
- **Cost savings.** Model tiering means you're not paying opus prices for documentation fixes. Auto-escalation on failure means you start cheap and only pay more when needed.

Even with a single machine, the pattern gives you scoping discipline, handoff resilience, and an auditable trail of decisions that a raw terminal session doesn't.

## Scaling Up

Started on one machine? Add more by:

1. On the new machine, run the installer (sets up the venv + systemd service in one shot):
   ```bash
   curl -sSL https://raw.githubusercontent.com/JDonaghy/claude-coordinator/main/install-agent.sh | bash -s -- --machine <name>
   ```
   No git clone needed on the worker machine — `install-agent.sh` pulls from PyPI.
2. Add it to `coordinator.yml` under `machines:` with its Tailscale hostname.
3. `coord status` from the coordinator machine shows all machines and their connectivity.

For Tailscale setup, see [tailscale.com/kb](https://tailscale.com/kb/). The agent server only needs port 7433 reachable on the tailnet.

## Troubleshooting

### Agent won't start

- **Port in use:** Another `coord agent` is running. Check `lsof -i :7433`, or use `--port` to pick a different one.
- **Hostname mismatch:** The agent matches `socket.gethostname()` against `coordinator.yml`. If it fails, pass `--machine NAME` explicitly.

### "connection refused" in coord status

- **Agent not running:** Start `coord agent` on the target machine.
- **Tailscale not connected:** Run `tailscale status` on both machines.
- **Firewall:** Tailscale usually handles this, but check `ufw`/`iptables` if you have custom rules.

### Worker fails immediately

- **Repo path wrong:** `repo_paths` in `coordinator.yml` must match the actual path on that machine. Run `coord config` to see parsed paths.
- **`gh` not authenticated:** The coordinator uses `gh` for GitHub operations (issues, PRs, comments). Workers do NOT use `gh` — only the coordinator does. Run `gh auth status` on the machine running `coord` commands.
- **`claude` CLI not available:** The agent spawns `claude -p` as a subprocess. Ensure the Claude Code CLI is installed and on the PATH.

### Stale dependency warnings

When `coord approve` warns about stale dependencies, an upstream repo on the target machine is behind GitHub's HEAD:
- `--auto-pull` to `git pull --ff-only` stale repos before starting the worker
- `--skip-freshness` to skip the check entirely
- Or manually pull on the target machine

### Board state issues

- **Stuck assignments after crash:** Run `coord resume` to reconcile board with live agent state.
- **Stale completed entries:** `coord resume` runs garbage collection, trimming old completed assignments down to a recent window.

## Requirements

- Python 3.12+
- Claude Code CLI with Max or Pro subscription
- `gh` CLI (authenticated, for coordinator-side GitHub operations)
- Tailscale — optional, only needed for multi-machine setups

## Releasing a New Version

1. Bump the version in `coord/__init__.py` and `pyproject.toml` (both must match)
2. Commit: `git commit -am "chore: bump version to X.Y.Z"`
3. Tag and push:
   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```
4. GitHub Actions (`publish.yml`) builds and publishes to PyPI automatically using the `PYPI_API_TOKEN` repository secret. Check the Actions tab for progress.

After the PyPI publish completes, upgrade remote agents — see [Upgrading Agents](#upgrading-agents).
