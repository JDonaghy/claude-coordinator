# claude-coordinator

CLI tool + per-machine agent server that coordinates Claude Code workers across multiple machines and repos over Tailscale.

## Current Goal — read first

**[`GOAL.md`](GOAL.md) holds the current north-star objective** — the living, cross-repo / cross-machine goal that should bias all planning, triage, and dispatch. It is meta-level (above any single issue, repo, or session) and changes as priorities evolve: read it first, plan against it, and keep it current. `coordinator.yml` is the source of truth for *topology*; `GOAL.md` is the source of truth for *intent*.

## Codebase navigation — query the graph first

This repo ships a **graphify** knowledge graph in `graphify-out/` (`graph.json`,
`GRAPH_REPORT.md`), kept current automatically by `post-commit` / `post-checkout`
git hooks. For any architecture / "where is this handled" / "what calls this" /
file-relationship question, **query the graph first** (the `graphify` skill, or the
graphify CLI) before reaching for grep/Read. Grep/Read are for exact-string or
line-level confirmation — not the first move.

## Architecture

```
coordinator.yml           — Single config file: repos, machines, dependencies
coord CLI                  — User-facing commands (plan, approve, assign, status, etc.)
coord agent (per-machine)  — HTTP server (port 7433) that runs claude -p
coord serve                — Board daemon (port 7435): canonical board state for thin clients
coord web                  — Lightweight dashboard (port 7434)
claude -p                  — The actual worker (runs locally on each machine)
GitHub issues              — Work source + message bus (via issue comments)
Tailscale                  — Networking between machines
```

## Project Structure

Query the **graphify graph** (`graphify-out/`) for the full module map + relationships — it's authoritative and auto-updated. Key entry points: `coord/cli.py` (Click CLI + all subcommands), `coord/agent.py` (`AgentServer`: `claude -p` subprocess mgmt) + `coord/agent_app.py` / `coord/serve_app.py` (agent + board-daemon HTTP apps), `coord/brain.py` (planning), `coord/dispatch.py` (routing: POST to agents, briefings), `coord/review.py` (adversarial review), `coord/merge_queue.py` (merge sequencing) + `coord/reconcile.py` (board↔agent), `coord/state.py` (board persistence in `~/.coord/`), `coord/models.py` (dataclasses), `coord/config.py` (`coordinator.yml` parsing), `coord/dashboard/` (web dashboard + `webapp/` phone PWA). Tests: `tests/test_<module>.py` (pytest; fixtures in `conftest.py`).

## Commands

`coord <cmd> --help` documents every command + flags. The core loop:

```bash
coord plan                 # Brain proposes assignments for idle machines
coord approve 1,3          # Dispatch approved proposals (comma-separated IDs)
coord assign <machine> <repo> <issue> [--briefing TEXT | --briefing-file F] [--dry-run]  # Direct dispatch
coord status [--freshness] # Machines, assignments, connectivity (+ repo freshness vs GitHub HEADs)
coord log <id> [-f] [--machine NAME]            # claude -p output (remote logs need --machine)
coord notify               # Poll agents, post completion/failure comments to GitHub
coord test --passed|--fail|--skipped <id>       # Record the Test-gate verdict (bare `coord test <id>` builds+tests locally)
coord merge [--dry-run] [--repo NAME] [--method rebase|squash|merge] [--order IDs] [--force-merge]
coord reconcile-merges     # Backfill missing branches + record out-of-band merges (#609/#611)
coord retry|stop|resume <id>                    # Recovery; `coord done` ends the session
```
Setup / diagnostics (discoverable via `--help`): `coord init`, `coord config`, `coord agent`, `coord serve`, `coord web`, `coord diagnose`, `coord sessions [--remote]`, `coord split`.

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
- **coordinator.yml is the single source of truth** for repo topology, machine capabilities, dependencies, concurrency limits, review settings, smoke-test rules, and the pipeline gate order (`pipeline.default_gates`).
- **coordinator.yml lives in `~/.coord/` — not the repo checkout.** Config-path resolution (`coord.config.resolve_config_path`) is `$COORD_CONFIG` → `~/.coord/coordinator.yml` → `./coordinator.yml` (first existing wins). The canonical home is `~/.coord/coordinator.yml`, mirroring `~/.coord/coord.db` + `~/.coord/client.toml`, so the tool runs on a machine with **no repo checkout**. `./coordinator.yml` is a development fallback only — relying on it makes the loaded file depend on your CWD (this bit us: a near-empty `~/src/<repo>/coordinator.yml` stub shadowed the real `~/coordinator.yml`). `coord config` and `coord serve` both print the resolved path so it's never ambiguous which file is loaded.
- **User approves everything.** `coord plan` proposes, user reviews, `coord approve` dispatches. `coord assign` is the escape hatch for direct dispatch. No autonomous dispatch.
- **Claim detection prevents duplicate work.** Before dispatching, the coordinator checks the board for active assignments and the remote for `issue-{N}-*` branches. If either exists, dispatch is refused with a clear message.
- **Conflict rules are inferred, not configured.** The coordinator brain reads issue bodies and infers which files will be touched. No DSL for conflict zones — optional `file_groups` and `exclusive_files` in config for power users.
- **Adversarial reviews are rule-enforcing, not rubber-stamping.** On worker completion, a fresh `claude -p` session on a *different* machine reviews the PR diff against the repo's CLAUDE.md and the review checklist. Zero shared context with the worker — that's the whole point.
- **Merge queue sequences PRs safely.** Completed branches are enqueued on reconciliation. `coord merge` opens PRs and merges them in dependency-aware order, with conflict detection and size-based sequencing.
- **Merge is gated on CI checks (#240).** Before merging a PR, `coord merge` calls `gh pr checks` via `coord.ci_store.CiStore` and refuses when any check has failed or is still running. Pass `--force-merge` to override (the failures are surfaced in the TUI and CLI output so the override is intentional). `ci_store: { type: none }` in `coordinator.yml` disables the gate entirely.
- **Mechanical merge conflicts auto-rebase (#241).** When `coord merge` fails because the worker's branch is out of date on a rebaseable conflict, the coordinator dispatches a `type="conflict-fix"` worker that rebases, resolves obvious additive merges, runs tests, and `git push --force-with-lease`. On success the merge re-enqueues automatically; on failure the entry is marked `HUMAN_REQUIRED` and surfaced in the TUI. Semantic conflicts (same function modified two ways) are not attempted — the worker exits and posts a comment for manual resolution. `gh` is denied for `conflict-fix` workers; only the coordinator drives merge retries.
- **Smoke tests validate on capable hardware.** When a worker finishes, `capability_rules` in `smoke_tests` config map changed files to required machine capabilities (e.g. GTK changes → machine with GTK). A `type="smoke"` assignment runs build + tests on the right machine.
- **Test precedes Review — the pipeline order is `Work → Test → Review → Merge`.** The smoke test runs *before* the PR/review (the natural order: smoke before PR), reversing the #520 "get the review over with first" workaround now that the agent-assisted Testing stage is smooth. This is enforced in two places that must stay in sync: (1) the **displayed** stage order comes from `pipeline.default_gates = ["test","review","merge"]` (`coord/config.py`; an old DB carrying the #520-era `["review","test","merge"]` is migrated in `coord/db.py`); (2) the **headless** auto-loop holds review dispatch until the work has a `passed`/`skipped` test verdict whenever `default_gates` orders test before review — `PipelineConfig.test_precedes_review()` drives `dispatch_pending_reviews` (`coord/review.py`). The explicit `coord review`/`coord pr` paths stay ungated so a human can always force a review. (The merge gate already required a test verdict — `requires_smoke` — so the human test touchpoint just moved earlier, and review cycles are no longer burned on untested code.)
- **Interactive testing + merge agents drive the Test → Review → Merge handoff (leg 3c / A3, #350/#581/#306/#606).** From a Pipeline row's right-click menu, the TUI routes **board-driven** verdicts (never TTY-scraped, ToS §3.7) along `Work → Test → Review → Merge`: **Start testing** = `coord assign --interactive --smoke-of <work_aid>` (read-only testing agent in the live checkout; records the verdict via `coord test --passed|--fail`); a `passed`/`skipped` test → **pass→review** (launches the interactive review); an approved review → **Start merge** = `--merge-of <work_aid>` (worktrees + proactively rebases onto the default branch #306, resolves mechanical conflicts (semantic with the operator), runs tests, `git push --force-with-lease`, then `coord verify-merge` and — if clean — `coord merge` itself to complete it #606); a `failed` test **or** request-changes review → one-key **`--fix-of`** on the same branch (a test-fail takes the identical action as a request-changes; `--fix-of` accepts a review id **or** a test-failed work id, the #581 front door). The merge agent is still gated on CI/review/smoke (a gate failure is reported, not forced) and `verify-merge` (#604) runs first. Full walkthrough: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
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

## Testing — black-box coverage is the acceptance bar

**Every PR that changes user-visible behavior must ship a black-box test** that drives the *running app* and asserts on its rendered output — not just unit tests on internal functions. The adversarial reviewer reads this file and **rejects behavior-changing PRs that lack one** (pure refactors / internal-only changes are exempt — say so in the PR if that applies). Build the **harness once per repo**; add **tests incrementally, one (or a few) per behavior-changing issue** — do *not* big-bang a full suite. Coverage then grows with churn and ratchets up (PRs add coverage, never remove it). Keep a thin **core smoke set** over the few most-trafficked screens so critical flows stay guarded even by unrelated changes.

> **The acceptance suite is becoming an independent, sealed *oracle* (2026-07-04, [`docs/ORACLE_LOOP.md`](docs/ORACLE_LOOP.md)).** In an oracle-loop milestone the worker **no longer authors** the acceptance tests — an independent `test-author` agent writes them from a mock-first Gate-A contract, and they are delivered to the worker **read-only / run-only** (`coord acceptance run --issue N`). The worker iterates against them **in its own warm session** until green (the tight loop), then the coordinator re-runs the sealed suite **externally** against the pushed SHA as the trust gate. The runner sits above **pluggable framework drivers** — `tui-tuidriver` (quadraui `TuiDriver`, the coord-tui case below), `web-playwright` (web/Electron), native — declared per repo in `acceptance.drivers` and routed to a capability-matched machine. Workers still write their **own unit/internal tests**; they must **never** edit `tests/acceptance/**`.

**How it runs:** black-box tests are part of the repo's normal test command, so the **Test stage** executes them on a capability-matched machine — `smoke_tests.capability_rules` route platform-specific suites to capable hardware (a GTK box; a machine with a browser). Favor the automated pre-review gate; the point is to trust the suite so manual/interactive smoke (incl. driving from a phone) is rarely needed.

### coord-tui — quadraui `TuiDriver` (harness shipped: #690 / #691)
- Drives the whole app through the real `event → handle → render` path against ratatui's headless `TestBackend` and asserts on the screen grid. `cargo test`-native, deterministic, no TTY.
- `CoordApp` implements quadraui's `ShellApp`, so use `quadraui::tui::testing::driver_with_shell(app, CoordApp::shell_config(), w, h)`. API: `find("text")` → coords, `click(x, y)`, `press`, `type_char`, `screen()`, `screen_contains(needle)`. **Locate targets with `find` — never hardcode coordinates.**
- **Reuse the existing fixtures** — `make_test_app(data: BoardData) -> CoordApp` (and `make_app_with_assignments`, `make_app_with_one_completed_issue`, …) in `tui/src/app.rs` build a full app from in-memory `BoardData`, no live daemon. Put the tests **in-crate** (`#[cfg(test)]`), **not** in `tui/tests/` — the fixtures are `#[cfg(test)]`/private and an integration-test crate can't see them.
- Limit: `TuiDriver` renders to `TestBackend`, so it does **not** parse real ANSI — terminal-protocol bugs (raw-mode, SGR mouse, the embedded `claude` PTY pane) are out of reach and still need a live smoke. A native pty + vt100 tier is tracked in quadraui#302 (unbuilt).

### coord web (Phone Control Center) — shipped v1 (#700–#703); browser E2E forthcoming
- The phone web app lives in `coord/dashboard/webapp/` (React / Vite / TS PWA, served by `coord/dashboard/server.py`). **v1 milestone (#700–#703) is shipped.** See [`docs/PHONE_WEBAPP.md`](docs/PHONE_WEBAPP.md) for the full runbook (build → serve → phone access over Tailscale).
- **Build the React bundle before first use** — `dist/` is gitignored. From `coord/dashboard/webapp/`: `npm install && npm run build`. Re-run after pulling changes to `src/`. The server falls back to the legacy `index.html` when `dist/` is absent so existing behaviour is unchanged without a build.
- **Run on the always-on host:** `coord web` binds `0.0.0.0:7434`. Reach it from a phone via the Tailscale MagicDNS name: `http://dellserver:7434` (replace with your host's name). Install as a PWA via "Add to Home Screen" in Safari / Chrome.
- **Vitest unit tests** ship in `coord/dashboard/webapp/src/components/__tests__/` — run with `npm test` inside `coord/dashboard/webapp/`.
- **Playwright E2E tests** are the forthcoming acceptance bar: start the dashboard server against a seeded board (the web parallel of `make_test_app(BoardData)`), drive a real headless browser, assert on the rendered DOM / screenshots. Route to a **browser-capable machine** via `smoke_tests.capability_rules`: add a `browser`/`playwright` capability in `coordinator.yml` and map `coord/dashboard/webapp/**` → that capability. Browsers headless-test more cleanly than terminals, so the webapp should lean almost entirely on this automated gate rather than interactive smoke.

## Conventions

- Python 3.12+, type hints everywhere
- Click for CLI
- httpx for HTTP client, Starlette + uvicorn for HTTP server
- PyYAML for config
- No Anthropic SDK — all Claude interaction is via `claude -p` subprocess
- Tests use pytest with fixtures in conftest.py
- State files go in `~/.coord/` — including `coordinator.yml` (canonical: `~/.coord/coordinator.yml`; override with `$COORD_CONFIG` or `--config`; `./coordinator.yml` is a dev fallback)
- Agent server port: 7433, dashboard port: 7434, board daemon port: 7435
- GitHub issue comments carry `<!-- coord:event=... assignment=... -->` markers for machine parsing

## Operational guides

- **Architecture overview**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the CLI, TUI, agent servers, and workers fit together; the agent HTTP API surface; where each `coord` subcommand actually runs; the auto-loop walked end-to-end.
- **Phone Web Control Center (v1)**: [`docs/PHONE_WEBAPP.md`](docs/PHONE_WEBAPP.md) — full runbook: build the React bundle (`npm install && npm run build` in `coord/dashboard/webapp/`), start `coord web` on the daemon host, access from a phone via Tailscale MagicDNS URL, install as a PWA. Also: complete API surface (`GET /api/pipeline` field reference including `review_verdict`, `review_findings_body`, `test_verdict` added in #698; `POST /api/pipeline/action` action table), ToS posture (headless-only, no live terminal), and test-tier map.
- **Why a merge/review isn't happening**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#when-a-merge-isnt-happening) — the Test gate (manual `coord test --passed`/P-S verdict) that silently blocks review→merge, plus a gate-by-gate checklist (review approved? CI green? PR conflict? queue clog / `--order`? post-bounce keying?). **Check here first when "Go does nothing" or a story stalls with no review.**
- **Why an issue is in the Pipeline you never dispatched**: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#when-an-issue-is-sitting-in-the-pipeline-you-never-dispatched) — Board vs Pipeline membership is **label-driven**: an open issue with a `status:ready` label (set by `coord ready`, and by the refinement / new-issue chat finalize step) shows as a Pipeline "ready" card even with zero assignments. **Drop it back with `coord backlog <repo> <issue>`** (strips `status:*`). This is the `status:ready` limbo → see #359.
- **Releasing to PyPI is a tag push, not `twine upload`.** Bump `pyproject.toml` + `coord/__init__.py` (must match), push main, then push a `vX.Y.Z` tag — `.github/workflows/publish.yml` builds and publishes with the `PYPI_API_TOKEN` repo secret (not available locally). **Agent-side changes (anything in `coord/agent.py`, e.g. worker prompts) only reach agents after a release + `coord agent update`;** coordinator-only code is live from the editable install immediately. Full steps in [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md#publishing-a-release-pypi).
- **INVARIANT: every remote agent's `~/.coord-venv` MUST be a PyPI install (`pip install claude-coordinator`), never editable.** The single most-recurring fleet failure. PyPI → `coord agent update` does `pip install --upgrade` and a released `vX.Y.Z` lands cleanly; editable → update silently `git pull`s a local checkout instead, so version bumps never propagate and the agent often "did not come back." **Root cause:** someone ran `pip install -e .` into `~/.coord-venv` — the editable **install** is the problem, **not** the `~/src/<repo>` checkout. #402's PATH-strip only stops *workers*' bare-pip, not a deliberate editable install.
  - **DO NOT delete `~/src/<repo>` to "fix" drift** — it's the **worker worktree base** (`git worktree add` runs from it; worktrees in `~/.coord/worktrees/` are worktrees *of* it), so deleting it breaks every task for that repo on that machine. Fix **only the install**.
  - **Detect:** `ssh <host> '~/.coord-venv/bin/pip show claude-coordinator | grep -i "editable\|location"'` — any `Editable project location:` line ⇒ drift (PyPI shows only a site-packages `Location`).
  - **Fix (keep the checkout):** in `~/.coord-venv`, `pip uninstall -y claude-coordinator && pip install --upgrade claude-coordinator`, then `XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-agent` (the `/update` `os.execv` self-restart does **not** take under systemd — #404, leaves same PID + stale version).
  - **`✗ did not come back` is usually a FALSE NEGATIVE** (agent online, restart just didn't take) — check the running version/PID first, then pick drift-fix vs. plain `systemctl --user restart coord-agent`. **Before touching any agent install, READ [`docs/AGENT_OPERATIONS.md`](docs/AGENT_OPERATIONS.md) end-to-end** (exact convert-to-PyPI commands, systemd restart, first-time install) — don't re-derive it.
- **`coord-tui` depends on `quadraui` by a relative path (`../../quadraui/quadraui`).** Workers touching `tui/src/**` or `tui/Cargo.toml` build against whatever branch is currently checked out in `~/src/quadraui`. If a `tui/` task consumes a not-yet-merged quadraui feature, the briefing **must** name the quadraui PR/branch — the worker is expected to `git -C ~/src/quadraui fetch && git -C ~/src/quadraui checkout <branch>` before `cargo build`, and restore the original branch before finishing. Without this, the worker's build silently picks up the wrong `quadraui` and produces a PR that won't compile on anyone else's checkout once that quadraui PR moves. **Verify build EXIT=0 from `tui/` after restoring the original branch.**
- **`coord-tui` ships as a locally-built binary, not via PyPI.** After a tui/ PR merges, the user needs to rebuild and reinstall locally: `cd tui && cargo build && cp target/debug/coord-tui ~/.local/bin/coord-tui`. The PyPI release flow above does not apply to coord-tui. Workers should not attempt to bump versions for tui-only changes.
