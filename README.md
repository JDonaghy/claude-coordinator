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
pip install -e .
coord init                    # interactive setup: detects repos, writes coordinator.yml
coord agent &                 # start the agent server (port 7433)

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
git clone https://github.com/JDonaghy/claude-coordinator.git ~/src/claude-coordinator
cd ~/src/claude-coordinator
pip install -e .
```

### 2. Configure

```bash
coord init        # interactive wizard: detects repos in cwd and ~/src/, writes coordinator.yml
coord config      # verify it parsed cleanly
```

Or copy `coordinator.example.yml` and edit it by hand. `coordinator.yml` is gitignored — keep secrets out of version control.

### 3. Start the agent server

```bash
coord agent &     # runs on port 7433; auto-detects machine from hostname
```

### 4. Coordinate

**Option A — Use the /coordinator slash command in Claude Code:**

Open Claude Code in the repo, type `/coordinator`. It handles first-time setup, issue triage, dispatch, monitoring, smoke tests, and PR creation. The slash command is at `.claude/commands/coordinator.md`.

**Option B — Use the CLI directly:**

```bash
coord status                              # verify agent is reachable
coord assign laptop myrepo 42 --model sonnet --briefing "Fix the auth bug"
coord watch <id>                          # live filtered output
coord test <id>                           # pull branch, run build + tests
coord test --passed <id>                  # record result
coord pr <id>                             # dispatch PR-creation worker
coord merge                               # open PRs and merge in sequence
```

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

# If installed editable (git clone):
cd ~/src/claude-coordinator && git pull --ff-only
# then restart the agent process
```

After upgrading, verify with `curl -s http://<host>:7433/status | python3 -c "import sys,json; print(json.load(sys.stdin).get('version'))"`.

**Why this matters:** agents older than 0.3.0 reject `coord assign --force` with a 400 error (`unexpected keyword argument 'fresh_branch'`).

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
| `coord notify` | Poll agents, post completion/failure comments to GitHub |
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
  enabled: false                 # opt-in: adversarial review on completion
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
- **Adversarial reviews** — enabled by default: `coord pr` auto-dispatches an independent `claude -p` session that reviews the diff with zero shared context. Works on a single machine — independence comes from a fresh session, not separate hardware
- **Smoke testing** — capability-aware routing (GTK changes → machine with GTK)
- **Merge queue** — sequences completed PRs with conflict detection and dependency-aware ordering
- **Progress streaming** — workers emit `STATUS:`/`STUCK:` lines; `coord status` shows real-time progress
- **Failure reassignment** — `coord retry` re-dispatches to a different machine; `auto_reassign` does it automatically
- **Crash recovery** — `coord resume` reconciles board with live agent state after restart
- **Web dashboard** — lightweight board view at port 7434

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

1. Install `coord` on the new machine (`pip install -e .` from the cloned repo)
2. Start the agent: `coord agent` (port 7433)
3. Add it to `coordinator.yml` under `machines:` with its Tailscale hostname
4. `coord status` shows all machines and their connectivity

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
- **Stale completed entries:** `coord resume` runs garbage collection, keeping the 50 most recent completed assignments.

## Requirements

- Python 3.12+
- Claude Code CLI with Max or Pro subscription
- `gh` CLI (authenticated, for coordinator-side GitHub operations)

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
- Tailscale — optional, only needed for multi-machine setups
