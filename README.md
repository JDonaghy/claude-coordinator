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

## Requirements

- Python 3.12+
- Claude Code CLI with Max or Pro subscription
- `gh` CLI (authenticated)
- Tailscale (for multi-machine setups)
