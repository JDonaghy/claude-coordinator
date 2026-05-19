# claude-coordinator

Coordinate Claude Code workers across multiple machines and repos over Tailscale. No API key needed — runs entirely on your Max/Pro subscription.

## The Problem

You have 3 machines and 2 repos. You want all 3 working in parallel without stepping on each other's files. Today you copy-paste assignments between terminal windows and track who's doing what in your head.

## The Solution

One config file describes your setup. A coordinator brain (Claude) proposes assignments. You approve from CLI or phone. Agent servers on each machine execute the work.

```
┌─ Phone / Browser ──────────────────────────┐
│  Board view, approve assignments, live logs │
└──────────────┬─────────────────────────────┘
               │ Tailscale
┌──────────────▼──────────────────────────────┐
│  coord plan → approve → dispatch             │
└──────┬──────────────┬──────────────┬────────┘
       │              │              │
  ┌────▼────┐   ┌─────▼─────┐  ┌────▼─────┐
  │ Machine A│   │ Machine B │  │ Server C │
  │ coord    │   │ coord     │  │ coord    │
  │ agent    │   │ agent     │  │ agent    │
  │ claude -p│   │ claude -p │  │ claude -p│
  └─────────┘   └───────────┘  └──────────┘
```

## Quick Start

### 1. Set up each machine

SSH into each machine and run:

```bash
git clone https://github.com/YourOrg/claude-coordinator.git ~/src/claude-coordinator
cd ~/src/claude-coordinator
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Create your coordinator.yml

Copy `coordinator.yml` to the repo root and edit it with your repos, machines, and Tailscale hostnames. Run `coord config` to verify it parses cleanly.

### 3. Start agent servers

On each machine:

```bash
cd ~/src/claude-coordinator
source .venv/bin/activate
coord agent
```

The agent auto-detects which machine it is from the hostname (case-insensitive match against `coordinator.yml`). Use `--machine NAME` if auto-detection fails.

Verify with `curl http://localhost:7433/health` on each machine.

### 4. Coordinate

From any machine:

```bash
source ~/src/claude-coordinator/.venv/bin/activate
coord status                    # verify all machines are online
coord plan                      # brain proposes assignments
coord approve 1,2,3             # dispatch to machines
coord status                    # monitor progress
coord notify                    # post completions to GitHub
coord test <id>                 # pull branch for smoke testing
coord test --passed <id>        # record result
coord merge                     # open PRs and merge completed branches
coord done                      # end session, run hooks
```

Or bypass the brain and dispatch directly:

```bash
coord assign macbook api-gateway 42 --briefing "Fix the auth middleware timeout"
```

## Command Reference

### Core Workflow

| Command | Description |
|---------|-------------|
| `coord plan [--dry-run]` | Brain proposes assignments for idle machines |
| `coord approve <IDs> [--dry-run] [--auto-pull] [--skip-freshness]` | Dispatch approved assignments (comma-separated IDs) |
| `coord assign <machine> <repo> <issue> [--briefing TEXT] [--dry-run]` | Direct dispatch, bypasses the brain |
| `coord status [--machine NAME] [--freshness]` | Show all machines, assignments, connectivity |
| `coord log <id> [-f] [--machine NAME] [--local]` | View claude -p output for an assignment |

### Post-Completion

| Command | Description |
|---------|-------------|
| `coord notify` | Poll agents, post completion/failure comments to GitHub |
| `coord test <id>` | Pull worker's branch locally, run build + tests |
| `coord test --passed <id>` | Record smoke test as passed |
| `coord test --fail <id> --reason "..."` | Record smoke test as failed |
| `coord merge [--dry-run] [--repo NAME] [--method rebase\|squash\|merge] [--order IDs]` | Process merge queue |
| `coord split <IDs>` | Create sub-issues from split proposals |

### Recovery and Lifecycle

| Command | Description |
|---------|-------------|
| `coord retry <id>` | Re-dispatch a failed assignment to a different machine |
| `coord stop <id>` | Cancel a running assignment |
| `coord resume` | Recover board state after crash, reconcile with agents |
| `coord done` | End session, run housekeeping hooks |

### Setup and Diagnostics

| Command | Description |
|---------|-------------|
| `coord agent [--machine NAME] [--host HOST] [--port PORT]` | Start agent server on this machine (default port 7433) |
| `coord web [--host HOST] [--port PORT]` | Start web dashboard (default port 7434) |
| `coord config` | Pretty-print parsed coordinator.yml |
| `coord version` | Print version |

## Configuration

```yaml
# coordinator.yml

repos:
  - name: api-gateway
    github: acme/api-gateway       # owner/repo on GitHub
    depends_on: []                  # list of repo names this depends on
    default_branch: main            # branch to merge into (default: main)
    build_command: "npm run build"  # optional: used by coord test for smoke testing
    test_command: "npm test"        # optional: used by coord test for smoke testing

  - name: user-service
    github: acme/user-service
    depends_on: [shared-lib]        # won't dispatch if shared-lib has active work
    default_branch: main
    build_command: "cargo build"
    test_command: "cargo test"

  - name: shared-lib
    github: acme/shared-lib
    depends_on: []

machines:
  - name: macbook
    host: macbook.tailnet           # Tailscale hostname (or any reachable hostname)
    capabilities: [docker, node, python]  # used for smoke-test routing
    repos: [api-gateway, user-service]    # which repos this machine can work on
    repo_paths:                     # local paths on this machine
      api-gateway: ~/src/api-gateway
      user-service: ~/src/user-service

  - name: server
    host: server.tailnet
    capabilities: [docker, python]
    repos: [user-service, shared-lib]
    repo_paths:
      user-service: ~/src/user-service
      shared-lib: ~/src/shared-lib

concurrency:
  max_workers: 2          # max simultaneous claude -p sessions across all machines
  stagger_seconds: 30     # delay between starting workers (avoids rate limits)
  backoff_base: 60        # seconds to wait on rate limit before retry
  max_retries: 3          # retries per dispatch on transient failures
  auto_reassign: false    # auto-retry failed assignments on a different machine
  stale_threshold: 3      # unreachable poll count before marking assignment stale

hooks:
  on_round_complete:      # run when all active assignments finish
    - summary_report
  on_session_end:         # run on `coord done`
    - summary_report
    # - close_merged_issues  # available but disabled by default

reviews:
  enabled: false          # opt-in: auto-dispatch adversarial reviews on completion
  auto_dispatch: true     # dispatch review automatically (vs manual trigger)
  require_approval: false # require human approval before merging reviewed PR
  reviewer_prompt: ""     # additional instructions for the reviewer
  checklist:              # items the reviewer checks against
    - "Did the worker add tests?"
    - "Did the worker stay within file scope?"
  repo_overrides:         # per-repo additional checklist items
    api-gateway:
      - "Check rate limiting on new endpoints"

smoke_tests:
  auto_queue: false                 # opt-in: auto-queue smoke tests on completion
  default_command: "make smoke"     # fallback if repo has no test_command
  timeout_seconds: 600              # max time for smoke test
  capability_rules:                 # route smoke tests to capable machines
    - files: [src/gtk/]             # if these files changed...
      requires: [gtk]               # ...smoke test needs a machine with gtk
```

## Features

- **No API key** — uses `claude -p` on your Max/Pro subscription
- **Multi-repo** — tracks dependencies between repos (e.g. shared-lib blocks user-service)
- **File conflict avoidance** — coordinator ensures two workers never touch the same files
- **Machine constraints** — respects capabilities (GTK, Docker, GPU) and repo availability
- **Claim detection** — prevents duplicate work by checking the board and remote branches
- **GitHub issues as work source** — reads open issues, posts briefings and status as comments
- **Adversarial reviews** — independent code review on a different machine with zero shared context
- **Smoke testing** — capability-aware routing ensures tests run on hardware that can validate them
- **Merge queue** — sequences completed PRs with conflict detection and dependency-aware ordering
- **Dependency freshness** — warns when upstream repos are stale on the target machine
- **Progress streaming** — workers emit STATUS/STUCK lines; `coord status` shows real-time progress
- **Failure reassignment** — re-dispatch failed work to a different machine automatically or manually
- **Split proposals** — the brain can propose splitting large issues into sub-tasks
- **Web dashboard** — approve assignments from your phone over Tailscale
- **Crash recovery** — `coord resume` reconciles board state with live agent servers

## How It Works

1. `coord plan` reads open GitHub issues and your config
2. The coordinator brain (Claude) proposes assignments — which machine, which issue, which files
3. You approve from CLI (`coord approve 1,2`) or dispatch directly (`coord assign`)
4. Coordinator dispatches to each machine's agent server over Tailscale
5. Agent servers run `claude -p` locally — billing stays on your subscription
6. Status updates posted as GitHub issue comments
7. `coord notify` posts completion/failure comments; `coord resume` reconciles state
8. Completed branches enter the merge queue; `coord merge` sequences PRs
9. `coord plan` proposes the next round

## Why This Works (Even With One Machine)

This tool encodes a pattern discovered through real multi-agent coordination sessions: **separate the tech lead from the IC.**

The coordinator thinks about *what to do next* — priority, dependencies, conflicts, which machine is idle. Workers think about *how to do this one thing* — read the issue, write the code, push the branch. Neither is distracted by the other's concern.

This division of labor produces better results than a single long-running Claude Code session, for several reasons:

- **Forced scoping.** One issue per worker session prevents scope creep. No "while I'm here, let me also refactor this." The worker does one thing and finishes.
- **Structured handoffs.** Every assignment has a briefing posted as a GitHub issue comment. If a session dies, a new one picks up from the comment — zero context loss.
- **Persistent record.** Every decision, briefing, and result lives on GitHub. You can review what happened a week later. Terminal scrollback is gone when the window closes.
- **Fresh eyes.** Each worker starts with no prior context. This sounds like a weakness but it's a strength — the worker reads the code as-is, not as it was 2 hours ago. Adversarial reviews take this further: a different machine reviews the work with genuinely independent context.
- **Human stays strategic.** You approve assignments and make judgment calls. You don't ferry messages between terminals or track who's touching which file in your head.

Even with a single machine, the pattern gives you scoping discipline, handoff resilience, and an auditable trail of decisions that a raw terminal session doesn't.

## Troubleshooting

### Agent won't start

- **Port already in use:** Another `coord agent` process is running, or another service is on port 7433. Check with `lsof -i :7433` or `ss -tlnp | grep 7433`. Kill the existing process or use `--port` to pick a different port.
- **Hostname mismatch:** The agent auto-detects which machine it is by matching `socket.gethostname()` against `coordinator.yml` entries. If it fails, pass `--machine NAME` explicitly. The match is case-insensitive and checks both `name` and `host` fields.

### "connection refused" in coord status

- **Agent not running:** SSH into the machine and start `coord agent`.
- **Tailscale not connected:** Run `tailscale status` on both machines. Ensure both are on the same tailnet and can ping each other.
- **Firewall blocking port 7433:** Tailscale usually handles this, but check `ufw` or `iptables` if you have custom rules.

### Worker fails immediately

- **Repo path wrong:** The `repo_paths` in `coordinator.yml` must match the actual path on that machine. The agent expands `~` but the directory must exist. Check with `coord config` to see parsed paths.
- **`gh` not authenticated:** Workers use `gh` for GitHub operations. Run `gh auth status` on the machine to verify. If it says "not logged in", run `gh auth login`.
- **`claude` CLI not available:** The agent spawns `claude -p` as a subprocess. Ensure the Claude Code CLI is installed and on the PATH.

### Stale dependency warnings

When `coord approve` warns about stale dependencies, it means an upstream repo on the target machine is behind GitHub's HEAD. Options:
- Pass `--auto-pull` to have the agent `git pull --ff-only` stale repos before starting the worker.
- Pass `--skip-freshness` to skip the check entirely (faster, but risks building against old code).
- SSH into the machine and manually pull the repo.

### Board state issues

- **Stuck assignments after crash:** Run `coord resume` to reconcile the board with live agent state. It queries each agent server and updates assignments that finished while the coordinator was down.
- **Stale completed entries:** `coord resume` runs garbage collection, keeping the 50 most recent completed assignments.

## Requirements

- Python 3.12+
- Claude Code CLI with Max or Pro subscription
- `gh` CLI (authenticated)
- Tailscale (for multi-machine setups)
