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
coord init                       # Interactive setup: detects repos + capabilities, writes coordinator.yml
```

## Development

Always work in a virtualenv. Agent workers are spawned with the agent's own
venv stripped from `PATH` (#402), so a bare `pip install` resolves to system
Python (PEP 668) — and must **never** target the agent's runtime venv. Create
your own venv in the checkout (`.venv/` is gitignored):

```bash
python3 -m venv .venv && . .venv/bin/activate
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
- **Merge is gated on CI checks (#240).** Before merging a PR, `coord merge` calls `gh pr checks` via `coord.ci_store.CiStore` and refuses when any check has failed or is still running. Pass `--force-merge` to override (the failures are surfaced in the TUI and CLI output so the override is intentional). `ci_store: { type: none }` in `coordinator.yml` disables the gate entirely.
- **Mechanical merge conflicts auto-rebase (#241).** When `coord merge` fails because the worker's branch is out of date on a rebaseable conflict, the coordinator dispatches a `type="conflict-fix"` worker that rebases, resolves obvious additive merges, runs tests, and `git push --force-with-lease`. On success the merge re-enqueues automatically; on failure the entry is marked `HUMAN_REQUIRED` and surfaced in the TUI. Semantic conflicts (same function modified two ways) are not attempted — the worker exits and posts a comment for manual resolution. `gh` is denied for `conflict-fix` workers; only the coordinator drives merge retries.
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
- **Never dispatch reviews via `coord assign`.** Workers have `gh` on the deny-list, so a worker dispatched with `coord assign` cannot run `gh pr diff` or `gh pr review`. Reviews must go through the review pipeline (`coord review` or auto-dispatch on completion) which uses `type="review"` and grants GitHub access.

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

## Operational guides

- **Architecture overview**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the CLI, TUI, agent servers, and workers fit together; the agent HTTP API surface; where each `coord` subcommand actually runs; the auto-loop walked end-to-end.
- **Why a merge/review isn't happening**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#when-a-merge-isnt-happening) — the Test gate (manual `coord test --passed`/P-S verdict) that silently blocks review→merge, plus a gate-by-gate checklist (review approved? CI green? PR conflict? queue clog / `--order`? post-bounce keying?). **Check here first when "Go does nothing" or a story stalls with no review.**
- **Why an issue is in the Pipeline you never dispatched**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#when-an-issue-is-sitting-in-the-pipeline-you-never-dispatched) — Board vs Pipeline membership is **label-driven**: an open issue with a `status:ready` label (set by `coord ready`, and by the refinement / new-issue chat finalize step) shows as a Pipeline "ready" card even with zero assignments. **Drop it back with `coord backlog <repo> <issue>`** (strips `status:*`). This is the `status:ready` limbo → see #359.
- **Releasing to PyPI is a tag push, not `twine upload`.** Bump `pyproject.toml` + `coord/__init__.py` (must match), push main, then push a `vX.Y.Z` tag — `.github/workflows/publish.yml` builds and publishes with the `PYPI_API_TOKEN` repo secret (not available locally). **Agent-side changes (anything in `coord/agent.py`, e.g. worker prompts) only reach agents after a release + `coord agent update`;** coordinator-only code is live from the editable install immediately. Full steps in [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md#publishing-a-release-pypi).
- **Agents are installed from PyPI**, not from a local git clone. The `~/src/claude-coordinator` directory only exists on the coordinator-development machine; remote agent machines should have only `~/.coord-venv` with `pip install claude-coordinator`. Editable installs on remote agents are the source of most upgrade failures.
- When an upgrade fails (`coord agent update --machine X` reports `did not come back`, or the version doesn't advance), see [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md). The most common fix is converting an editable install to PyPI — that doc has the exact commands.
- New machines: `docs/AGENT_OPERATIONS.md` also covers first-time install and verification.
- **`coord-tui` depends on `quadraui` by a relative path (`../../quadraui/quadraui`).** Workers touching `tui/src/**` or `tui/Cargo.toml` build against whatever branch is currently checked out in `~/src/quadraui`. If a `tui/` task consumes a not-yet-merged quadraui feature, the briefing **must** name the quadraui PR/branch — the worker is expected to `git -C ~/src/quadraui fetch && git -C ~/src/quadraui checkout <branch>` before `cargo build`, and restore the original branch before finishing. Without this, the worker's build silently picks up the wrong `quadraui` and produces a PR that won't compile on anyone else's checkout once that quadraui PR moves. **Verify build EXIT=0 from `tui/` after restoring the original branch.**
- **`coord-tui` ships as a locally-built binary, not via PyPI.** After a tui/ PR merges, the user needs to rebuild and reinstall locally: `cd tui && cargo build && cp target/debug/coord-tui ~/.local/bin/coord-tui`. The PyPI release flow above does not apply to coord-tui. Workers should not attempt to bump versions for tui-only changes.

## Status

Issues #1-19 are closed. The core loop, multi-machine dispatch, Tailscale networking, adversarial reviews, merge queue, smoke testing, progress streaming, claim detection, and failure reassignment are all implemented. Remaining work is tracked in the [GitHub issue tracker](https://github.com/JDonaghy/claude-coordinator/issues).
