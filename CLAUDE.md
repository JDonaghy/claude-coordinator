# claude-coordinator

CLI tool + per-machine agent server that coordinates Claude Code workers across multiple machines and repos over Tailscale.

## Architecture

```
coordinator.yml           — Single config file: repos, machines, dependencies
coord CLI                  — User-facing commands (plan, approve, status, etc.)
coord agent (per-machine)  — HTTP server (port 7433) that runs claude -p
coord web                  — Lightweight dashboard (port 7434)
claude -p                  — The actual worker (runs locally on each machine)
GitHub issues              — Work source + message bus (via issue comments)
Tailscale                  — Networking between machines
```

## Target Project Structure

```
coord/
  __init__.py
  cli.py           — Click CLI entry point and subcommands
  config.py        — coordinator.yml parsing and validation
  models.py        — Dataclasses: Machine, Repo, Assignment, Board
  agent.py         — Agent server (HTTP, spawns claude -p)
  brain.py         — Coordinator brain (calls claude -p for planning)
  github_ops.py    — GitHub operations via gh CLI
  dispatch.py      — Assignment routing and conflict checking
  state.py         — Board state persistence and recovery
  dashboard/
    server.py      — Web dashboard HTTP server
    index.html     — Single-file dashboard (HTML + CSS + JS)
pyproject.toml
coordinator.yml    — Example config (also used for development)
tests/
  test_config.py
  test_models.py
  test_agent.py
  test_brain.py
  conftest.py
```

Note: the existing top-level files (`coordinator.py`, `board.py`, `workers.py`, `github_ops.py`) are the initial prototype. They will be restructured into the `coord/` package during issue #1.

## Commands

```bash
coord init              # Interactive setup, generates coordinator.yml
coord agent             # Start agent server on this machine (port 7433)
coord plan              # Brain proposes assignments for idle machines
coord approve 1,3       # Dispatch approved assignments
coord status            # Show all machines, assignments, connectivity
coord log <id>          # View claude -p output for an assignment
coord web               # Start web dashboard (port 7434)
```

## Development

```bash
pip install -e ".[dev]"
pytest
coord plan --dry-run
coord approve --dry-run 1,2
```

## Key Design Decisions

- **No API key needed.** Everything uses `claude -p` which runs on Max/Pro subscription via OAuth.
- **Agent servers are dumb dispatchers.** They spawn `claude -p` and track the subprocess. All intelligence is in the coordinator brain.
- **GitHub issue comments as message bus.** Briefings and status updates are posted as comments — persistent, linkable, readable by any agent.
- **coordinator.yml is the single source of truth** for repo topology, machine capabilities, and dependencies.
- **User approves everything.** `coord plan` proposes, user reviews, `coord approve` dispatches. No autonomous dispatch in v1.
- **Conflict rules are inferred, not configured.** The coordinator brain (Claude) reads issue bodies and infers which files will be touched. No DSL for conflict zones — optional `file_groups` and `exclusive_files` in config for power users.
- **Adversarial reviews are rule-enforcing, not rubber-stamping.** The reviewer prompt is assembled from two sources: the repo's CLAUDE.md (project-specific rules) and the coordinator's review checklist in `coordinator.yml` (what to look for). Workers violate rules they're focused on following — the reviewer's sole job is enforcement.

## Review Prompt Assembly

The reviewer gets a prompt built from:
1. **Repo's CLAUDE.md** — the project rules (source of truth, not duplicated)
2. **Generic checklist** — "did you add tests?", "did you stay in file scope?", "any security issues?"
3. **Repo overrides** — project-specific patterns from `coordinator.yml` `reviews.repo_overrides`
4. **The diff** — `git diff` of the worker's branch vs base
5. **The issue** — title and body for intent verification

The reviewer reads the rules and enforces them against the diff. It does not have the worker's session context — genuinely independent.

## Conventions

- Python 3.12+, type hints everywhere
- Click for CLI
- httpx for HTTP client, uvicorn for HTTP server
- PyYAML for config
- No Anthropic SDK — all Claude interaction is via `claude -p` subprocess
- Tests use pytest with fixtures in conftest.py
- State files go in `~/.coord/`
- Agent server port: 7433, dashboard port: 7434

## Milestones

1. **MVP Single-Machine** (#1–#4) — core loop on one machine, one repo
2. **Multi-Machine Multi-Repo** (#5–#8) — Tailscale, dependencies, GitHub comments
3. **Web Dashboard** (#9–#11) — phone-accessible UI
4. **Polish** (#12–#14) — init wizard, error handling, docs

## Issue Dependency Graph

```
#1 (CLI scaffold)
 ├── #2 (Agent server) ──┐
 │                        ├── #4 (E2E test)
 └── #3 (Brain + plan)  ─┘       │
      └── #7 (GitHub comments)   │
                                  ▼
                           #5 (Tailscale)
                            ├── #6 (Multi-repo deps)
                            │    └── #8 (State persistence)
                            │         ├── #9 (Dashboard)
                            │         │    ├── #10 (SSE streaming)
                            │         │    └── #11 (Approval flow)
                            │         └── #13 (Error handling)
                            └── #9

#12 (coord init) — independent
#14 (Documentation) — independent, last
```
