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

```bash
pip install claude-coordinator

# On each machine
coord agent

# On any machine (or phone via coord web)
coord plan --config coordinator.yml
coord approve 1,2,3
coord status
```

## Configuration

```yaml
# coordinator.yml
repos:
  - name: api-gateway
    github: acme/api-gateway
    depends_on: []

  - name: user-service
    github: acme/user-service
    depends_on: [shared-lib]

  - name: shared-lib
    github: acme/shared-lib
    depends_on: []

machines:
  - name: macbook
    host: macbook.tailnet
    capabilities: [docker, node, python]
    repos: [api-gateway, user-service]

  - name: server
    host: server.tailnet
    capabilities: [docker, python]
    repos: [user-service, shared-lib]
```

## Features

- **No API key** — uses `claude -p` on your Max/Pro subscription
- **Multi-repo** — tracks dependencies between repos (e.g. shared-lib → user-service)
- **File conflict avoidance** — coordinator ensures two workers never touch the same files
- **Machine constraints** — respects capabilities (GTK, Docker, GPU) and repo availability
- **GitHub issues as work source** — reads open issues, posts briefings as comments
- **Web dashboard** — approve assignments from your phone over Tailscale
- **Crash recovery** — coordinator and agents persist state independently

## How It Works

1. `coord plan` reads open GitHub issues and your config
2. The coordinator brain (Claude) proposes assignments — which machine, which issue, which files
3. You approve from CLI (`coord approve 1,2`) or the web dashboard
4. Coordinator dispatches to each machine's agent server over Tailscale
5. Agent servers run `claude -p` locally — billing stays on your subscription
6. Status updates posted as GitHub issue comments
7. When a worker finishes, `coord plan` proposes the next round

## Why This Works (Even With One Machine)

This tool encodes a pattern discovered through real multi-agent coordination sessions: **separate the tech lead from the IC.**

The coordinator thinks about *what to do next* — priority, dependencies, conflicts, which machine is idle. Workers think about *how to do this one thing* — read the issue, write the code, push the branch. Neither is distracted by the other's concern.

This division of labor produces better results than a single long-running Claude Code session, for several reasons:

- **Forced scoping.** One issue per worker session prevents scope creep. No "while I'm here, let me also refactor this." The worker does one thing and finishes.
- **Structured handoffs.** Every assignment has a briefing posted as a GitHub issue comment. If a session dies, a new one picks up from the comment — zero context loss.
- **Persistent record.** Every decision, briefing, and result lives on GitHub. You can review what happened a week later. Terminal scrollback is gone when the window closes.
- **Fresh eyes.** Each worker starts with no prior context. This sounds like a weakness but it's a strength — the worker reads the code as-is, not as it was 2 hours ago. Adversarial reviews (#15) take this further: a different machine reviews the work with genuinely independent context.
- **Human stays strategic.** You approve assignments and make judgment calls. You don't ferry messages between terminals or track who's touching which file in your head.

Even with a single machine, the pattern gives you scoping discipline, handoff resilience, and an auditable trail of decisions that a raw terminal session doesn't.

## Requirements

- Python 3.12+
- Claude Code CLI with Max or Pro subscription
- `gh` CLI (authenticated)
- Tailscale (for multi-machine setups)
