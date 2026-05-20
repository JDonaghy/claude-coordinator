# claude-coordinator

CLI tool + per-machine agent server that coordinates Claude Code workers across multiple machines and repos over Tailscale.

## Architecture

```
coordinator.yml           — Single config file: repos, machines, dependencies
coord CLI                  — User-facing commands (plan, approve, assign, status, etc.)
coord agent (per-machine)  — HTTP server (port 7433) that runs claude -p
coord web                  — Lightweight dashboard (port 7434)
claude -p                  — The actual worker (runs locally on each machine)
GitHub issues              — Work source + message bus (via issue comments)
Tailscale                  — Networking between machines
```

## Project Structure

```
coord/
  __init__.py        — Package init, version string
  cli.py             — Click CLI entry point and all subcommands
  config.py          — coordinator.yml parsing and validation (Config, HooksConfig, ReviewsConfig, ConcurrencyConfig, SmokeTestsConfig)
  models.py          — Dataclasses: Machine, Repo, Assignment, Proposal, SplitProposal, Board
  agent.py           — AgentServer: subprocess management for claude -p workers
  agent_app.py       — Starlette HTTP app for the agent server (routes, health, status)
  brain.py           — Coordinator brain: gathers context, calls claude -p for planning, parses proposals
  github_ops.py      — GitHub operations via gh CLI (issues, PRs, branches, comments)
  dispatch.py        — Assignment routing: POST to agent servers, briefing formatting, retry logic
  state.py           — Board state persistence and recovery (~/.coord/)
  claim.py           — Issue claim detection: prevents two agents picking the same issue
  comments.py        — Format/parse coordinator-authored issue comments (the message bus)
  deps.py            — Dependency graph: transitive deps, blocked repos, cycle detection
  events.py          — SSE event source for dashboard and live-log streaming
  freshness.py       — Compare agent-reported repo state against GitHub HEADs
  hooks.py           — Session lifecycle hooks (on_round_complete, on_session_end)
  merge_queue.py     — Merge queue: sequence completed branches into target branches via PRs
  network.py         — Network health checks for agent servers over Tailscale
  notify.py          — Poll agents, post completion/failure comments to GitHub
  progress.py        — Parse worker STATUS/STUCK signals from log output
  reconcile.py       — Reconcile board with live agent state, auto-reassign failures
  review.py          — Adversarial code review: dispatch independent reviewer on completion
  smoke.py           — Smoke-test orchestration: auto-queue validation on capable machines
  dashboard/
    __init__.py
    server.py        — Web dashboard HTTP server (Starlette + uvicorn)
    index.html       — Single-file dashboard (HTML + CSS + JS)
pyproject.toml
coordinator.yml      — Example config (also used for development)
tests/
  conftest.py
  test_agent.py, test_agent_app.py, test_agent_branch_capture.py, test_agent_repos_and_pull.py
  test_board_state.py, test_brain.py, test_claim.py
  test_cli_assign.py, test_cli_merge.py, test_cli_network.py
  test_comments.py, test_config.py, test_coord_test.py
  test_dashboard.py, test_deps.py, test_dispatch.py
  test_errors.py, test_events.py, test_freshness.py
  test_handoff.py, test_hooks.py, test_integration.py
  test_merge_queue.py, test_models.py, test_network.py
  test_notify.py, test_progress.py
  test_reconcile_branch.py, test_retry.py, test_review.py
  test_smoke.py, test_split.py, test_state.py
```

## Commands

```bash
# Core workflow
coord plan                       # Brain proposes assignments for idle machines
coord approve 1,3                # Dispatch approved assignments (comma-separated IDs)
coord assign <machine> <repo> <issue> [--briefing TEXT] [--dry-run]
                                 # Direct dispatch, bypasses the brain
coord status                     # Show all machines, assignments, connectivity
coord status --freshness         # Also report per-machine repo freshness vs GitHub HEADs
coord log <id> [-f]              # View claude -p output for an assignment (--follow for tail)

# Post-completion
coord notify                     # Poll agents, post completion/failure comments to GitHub
coord test <id>                  # Pull worker's branch locally, run build + tests
coord test --passed <id>         # Record smoke test as passed
coord test --fail <id> --reason "..."  # Record smoke test as failed
coord merge [--dry-run] [--repo NAME] [--method rebase|squash|merge] [--order IDs]
                                 # Process the merge queue: open PRs and merge in sequence
coord split S1,S2                # Create sub-issues from split proposals

# Recovery and lifecycle
coord retry <id>                 # Re-dispatch a failed assignment to a different machine
coord stop <id>                  # Cancel a running assignment
coord resume                     # Recover board state after crash, reconcile with agents
coord done                       # End session, run housekeeping hooks, show summary

# Setup and diagnostics
coord agent [--machine NAME]     # Start agent server on this machine (port 7433)
coord web                        # Start web dashboard (port 7434)
coord config                     # Pretty-print parsed coordinator.yml
coord version                    # Print version
coord init                       # Interactive setup (stub — not yet implemented)
```

## Development

```bash
pip install -e ".[dev]"
pytest
coord plan --dry-run
coord approve --dry-run 1,2
coord assign --dry-run precision claude-coordinator 42
```

## Key Design Decisions

- **No API key needed.** Everything uses `claude -p` which runs on Max/Pro subscription via OAuth.
- **Agent servers are dumb dispatchers.** They spawn `claude -p` and track the subprocess. All intelligence is in the coordinator brain.
- **GitHub issue comments as message bus.** Briefings, completion notices, and failure reports are posted as comments — persistent, linkable, readable by any agent. Comments carry `<!-- coord:... -->` markers for machine-parseable metadata.
- **coordinator.yml is the single source of truth** for repo topology, machine capabilities, dependencies, concurrency limits, review settings, and smoke-test rules.
- **User approves everything.** `coord plan` proposes, user reviews, `coord approve` dispatches. `coord assign` is the escape hatch for direct dispatch. No autonomous dispatch.
- **Claim detection prevents duplicate work.** Before dispatching, the coordinator checks the board for active assignments and the remote for `issue-{N}-*` branches. If either exists, dispatch is refused with a clear message.
- **Conflict rules are inferred, not configured.** The coordinator brain reads issue bodies and infers which files will be touched. No DSL for conflict zones — optional `file_groups` and `exclusive_files` in config for power users.
- **Adversarial reviews are rule-enforcing, not rubber-stamping.** On worker completion, a fresh `claude -p` session on a *different* machine reviews the PR diff against the repo's CLAUDE.md and the review checklist. Zero shared context with the worker — that's the whole point.
- **Merge queue sequences PRs safely.** Completed branches are enqueued on reconciliation. `coord merge` opens PRs and merges them in dependency-aware order, with conflict detection and size-based sequencing.
- **Smoke tests validate on capable hardware.** When a worker finishes, `capability_rules` in `smoke_tests` config map changed files to required machine capabilities (e.g. GTK changes → machine with GTK). A `type="smoke"` assignment runs build + tests on the right machine.
- **Progress streaming from workers.** Workers emit `STATUS:` and `STUCK:` lines in their logs. The coordinator parses these for real-time progress reporting in `coord status` and the dashboard.
- **Failure reassignment.** Failed assignments can be retried on a different machine via `coord retry`. With `concurrency.auto_reassign: true`, reconciliation auto-retries on a different machine.
- **Dependency freshness checks.** Before dispatching, `coord approve` checks whether upstream repos are up-to-date on the target machine. Stale dependencies trigger warnings or auto-pull with `--auto-pull`.

## Review Prompt Assembly

The reviewer gets a prompt built from:
1. **Repo's CLAUDE.md** — the project rules (source of truth, not duplicated)
2. **Generic checklist** — "did you add tests?", "did you stay in file scope?", "any security issues?"
3. **Repo overrides** — project-specific patterns from `coordinator.yml` `reviews.repo_overrides`
4. **The diff** — `gh pr diff` of the worker's branch vs base
5. **The issue** — title and body for intent verification

The reviewer reads the rules and enforces them against the diff. It does not have the worker's session context — genuinely independent.

## Cost Discipline

The coordinator session (typically Opus) costs ~10x more per token than Sonnet workers. Minimize direct code work in the coordinator — instead, write a good briefing and dispatch it.

- **Dispatch, don't do.** If a task can be described in a briefing, send it to a worker. Reserve the coordinator session for triage, review, and decisions.
- **Workers are cheap.** Sonnet workers typically cost $0.30-0.90 per task. An hour of Opus coordinator time costs $40+.
- **Compact aggressively.** Long coordinator sessions balloon cache reads. Use `/compact` when switching topics or after completing a batch of work.
- **Parallel workers, serial coordinator.** Dispatch multiple workers in parallel, then review results. Don't do two things at once in the coordinator session.
- **Trust the adversarial review.** When a review completes, read only the review comment — do not re-read the full PR diff to form an independent opinion. Summarize the reviewer's findings and ask the user how to proceed. Only read the diff if the review seems wrong or incomplete.
- **Audit before dispatching.** Include a step in briefings: "Before coding, verify this isn't already implemented." Workers have wasted full sessions building features that already existed.
- **Only the coordinator writes docs.** Workers must not update README, CHANGELOG, or shared documentation files. Parallel doc edits cause merge conflicts. Add docs to `files_forbidden` in briefings; the coordinator handles doc updates at session end.
- **Catch platform violations at review time.** The adversarial reviewer should check for platform-specific code in shared/cross-platform paths. Catching after merge costs an entire round-trip.

## Conventions

- Python 3.12+, type hints everywhere
- Click for CLI
- httpx for HTTP client, Starlette + uvicorn for HTTP server
- PyYAML for config
- No Anthropic SDK — all Claude interaction is via `claude -p` subprocess
- Tests use pytest with fixtures in conftest.py
- State files go in `~/.coord/`
- Agent server port: 7433, dashboard port: 7434
- GitHub issue comments carry `<!-- coord:event=... assignment=... -->` markers for machine parsing

## Status

Issues #1-19 are closed. The core loop, multi-machine dispatch, Tailscale networking, adversarial reviews, merge queue, smoke testing, progress streaming, claim detection, and failure reassignment are all implemented. Remaining work is tracked in the [GitHub issue tracker](https://github.com/JDonaghy/claude-coordinator/issues).
