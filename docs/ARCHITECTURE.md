# Architecture

How the coordinator, the agent servers, the CLI, the TUI, and the dashboard fit together.

## The big picture

```
                    ~/.coord/coord.db (SQLite)
                    coordinator.yml
                    GitHub (issues, PRs, comments)
                               ▲
                ┌──────────────┼──────────────┐
                │              │              │
            coord CLI     coord-tui       coord web
            (Python)      (Rust)          (Python, optional)
                │              │              │
                └──────────────┼──────────────┘
                               │  HTTP (port 7433)
                               ▼
                      ┌──────────────┐
                      │ coord agent  │  (one per machine)
                      │ Python HTTP  │
                      │ server       │
                      └──────┬───────┘
                             │ spawns
                             ▼
                      claude -p worker subprocess
                      (runs in isolated git worktree)
```

The system has three kinds of process:

1. **Coordinator clients** — the CLI (`coord`), the TUI (`coord-tui`), and the optional web dashboard (`coord web`). They read shared state from SQLite + `coordinator.yml` + GitHub, and dispatch work by calling agent HTTP endpoints.
2. **Agent servers** — one `coord agent` HTTP server per worker machine. Spawns and tracks `claude -p` subprocesses. Owns the worktrees, the log files, and per-worker lifecycle.
3. **Worker subprocesses** — `claude -p ...` invocations spawned by the agent. They run in isolated git worktrees and have no awareness of the coordinator beyond the briefing they received.

## Why this shape

The split-brain design has three goals:

- **Workers do one thing.** Each `claude -p` session has a single briefing, a single worktree, and no shared context with anything else. Fresh eyes every time.
- **The coordinator stays cheap.** The coordinator runs Opus by default (for triage and review); workers run Sonnet or Haiku. The coordinator's job is to *write good briefings and dispatch them*, not to do the work itself.
- **GitHub is the message bus.** Briefings, completions, failures, and reviews are posted as issue comments with `<!-- coord:event=... -->` markers. Persistent, linkable, parseable by any tool. Surviving a coordinator crash is a matter of reading the latest comments.

## The "no central daemon" choice

There is **no long-running coordinator daemon**. The CLI and TUI are short-lived processes that read state, do something, and exit. Three consequences:

- **State lives in SQLite + GitHub.** `~/.coord/coord.db` is the local cache; GitHub issue comments are the durable source of truth. Either is enough to reconstruct the other.
- **`coord notify` has to be fired periodically.** It polls each agent for completion, posts the GH comments, and triggers the auto-loop (review-on-completion, fix-on-request-changes, re-review-on-fix-completion). Without it, the pipeline visibly freezes — agents finish work but no one notices. Run it on a cron, a `watch`, a TUI timer, or by hand.
- **`coord` and `coord-tui` are peers, not nested layers.** The TUI does not shell out to `coord`. Both are independent clients of the same state and HTTP API. (See the [Divergence risk](#divergence-risk) section.)

## The agent HTTP API

The `coord agent` server exposes a small, JSON-only HTTP API on port 7433. Any client — `coord`, the TUI, `curl`, Postman — can call it. There's no authentication; Tailscale is the auth boundary.

| Method + Path | Purpose |
|---|---|
| `POST /assign` | Dispatch a worker on this machine. Body is the `AssignmentSpec` JSON. Returns `{"id": ..., "status": "running"}` |
| `GET  /status` | Active + completed assignments, with progress updates, exit codes, cost data |
| `GET  /health` | Version, uptime, last_update result, machine info |
| `GET  /logs/<id>` | Full worker log |
| `GET  /stream/<id>` | Server-sent events: tails the worker log |
| `POST /cancel/<id>` | Send SIGTERM to a running worker |
| `POST /restart` | Restart the agent process (waits for active workers up to `cancel_timeout`) |
| `POST /update` | `pip install --upgrade claude-coordinator` then re-exec |
| `POST /worktree-clean` | Prune stale worktrees for completed assignments |

Example: dispatch a worker with curl.

```bash
curl -X POST http://laptop.tailnet:7433/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_name": "myrepo",
    "repo_path": "~/src/myrepo",
    "issue_number": 42,
    "issue_title": "Fix the auth bug",
    "briefing": "Read src/auth.py and fix the timeout issue.",
    "files_allowed": [],
    "files_forbidden": [],
    "pull_repos": [],
    "type": "work"
  }'
```

That's all the agent does. Everything else — the merge queue, the plan brain, the notify pipeline, the review dispatch logic — lives in the coordinator clients and runs *on the machine where you invoke them*.

## Where each `coord` subcommand actually runs

Concretely: when you run `coord X` from your laptop, what happens?

| Subcommand | Reads | Writes / Calls |
|---|---|---|
| `coord status` | DB, agent `/status`, `/health` | Stdout |
| `coord assign` | DB, `coordinator.yml` | Agent `/assign`, DB |
| `coord plan` | DB, GitHub issues | Spawns `claude -p` for the brain, DB |
| `coord approve` | DB, agent `/status`, GitHub | Agent `/assign`, DB |
| `coord merge` | DB (merge_queue, board), `gh pr ...` | `gh pr create/merge`, DB |
| `coord notify` | Agent `/status`, DB | `gh issue comment`, agent `/logs/<id>`, DB, **triggers auto-loop** |
| `coord bounce` | DB | Agent `/assign` (fix worker), DB |
| `coord pr` | DB | Agent `/assign` (PR worker + review worker), DB |
| `coord agent update` | `coordinator.yml` | Agent `/update`, `/health` |
| `coord agent` | n/a (this **is** the agent server) | HTTP listener on port 7433 |

Notice that `coord agent` is the odd one out — it's the only subcommand that's a server, not a client. All others are short-lived clients.

## The auto-loop, end to end

The most coordinated path through the system is the review/fix/re-review loop. It's worth walking through in one place because the logic is spread across `coord/notify.py`, `coord/auto_loop.py`, `coord/review.py`, and `coord/merge_queue.py`.

1. `coord assign laptop myrepo 42` posts to `POST /assign` on laptop. Agent spawns `claude -p`.
2. Worker finishes, pushes branch, exits. Agent records `status=done`.
3. `coord notify` (run periodically) polls laptop, sees the completion, posts a GH comment, and (if `reviews.enabled`) calls `dispatch_review`. A new `type="review"` assignment is sent to a *different* machine.
4. Reviewer reads the diff, runs tests, posts `gh pr review` with `--approve` / `--request-changes` / `--comment`. The reviewer's log carries a machine-parseable verdict header.
5. `coord notify` runs again. Sees the review completion, parses the verdict, persists `review_verdict` and `review_findings` to the DB.
6. **If `request-changes`**: `run_for_review_transition` calls `_dispatch_fix`, which posts a `type="work"` `[fix-N]` assignment with `target_branch=<original work's branch>` so the fix lands on the same branch (not an orphan).
7. **If `approve`**: nothing happens immediately — the merge gate (`has_approved_review`) will allow the merge next time `coord merge` runs.
8. Fix worker finishes. `coord notify` detects the completion (via the `fix_completions` classifier added in #278), calls `run_for_fix_transition`, which dispatches a fresh review against the fix-1 assignment.
9. Re-review approves → merge gate passes. `coord merge` rebases and merges. Conflict on rebase → conflict-fix worker auto-dispatched (`#241`), pinned to the original branch via `target_branch` (`#277`), pushes the rebase, merge re-enqueues, succeeds.

If any single link is broken — most often `coord notify` not running — the whole loop stalls. The TUI helps spot this because completed assignments without comments are visible in the pipeline view, but the fix is always "run `coord notify`."

## Divergence risk

The CLI and the TUI are peer clients of the same state. They re-implement the same business logic in two languages. There is no compiler check keeping them in sync.

**What stays in sync naturally:**

- Anything that reads SQLite directly and renders it (status, queue rows, assignment metadata).
- Anything that POSTs to agent HTTP endpoints (`/assign`, `/cancel`, `/status`).
- Anything that hits GitHub via `gh` (the TUI shells out for these).

**What needs manual mirroring:**

- State-machine classification (e.g. `PipelineMergeState`: NotApplicable / NoQueue / Merged / BlockedOnReview / BlockedOnCi / Ready). The Python `coord/merge_queue.py` and the Rust `tui/src/app.rs` both have to know the same rules.
- Conflict classifier signal strings (`_REBASEABLE_SIGNALS`, `_HUMAN_SIGNALS`).
- Review gate predicate (`has_approved_review`).

**Symptom of divergence:** the TUI paints a stage as `BlockedOnReview` when in fact the Python side would proceed, or vice versa. Today (May 2026) the TUI mostly delegates to queue rows, so most gate logic is in Python — but the TUI is growing, and the more it implements directly, the more divergence opportunity.

**The natural next step**, if/when this becomes painful, is to make `coord` a long-running HTTP/JSON daemon (think `kubectl` + k8s apiserver) and have both the CLI and the TUI become thin clients. That would put all gate logic in one place. For now: status quo, with awareness.

## File map

| Path | What lives there |
|---|---|
| `coord/agent.py`, `coord/agent_app.py` | The HTTP server (`coord agent` subcommand). Subprocess management for `claude -p`. |
| `coord/cli.py` | All other `coord X` subcommands. Click entry points. |
| `coord/brain.py` | The planning brain: gathers context, calls `claude -p`, parses proposals. |
| `coord/merge_queue.py` | The merge state machine: enqueue, sequence, process, gate. |
| `coord/auto_loop.py` | Review → fix dispatch (#243), fix → review dispatch (#278). |
| `coord/notify.py` | Polls agents, posts GH comments, triggers the auto-loop. |
| `coord/conflict_fix.py` | Rebase-on-merge-failure worker dispatch (#241). |
| `coord/review.py` | Adversarial review dispatch + verdict parsing. |
| `coord/state.py`, `coord/db.py` | SQLite schema and access helpers. |
| `coord/dashboard/` | The web dashboard (`coord web`). Optional. |
| `tui/src/app.rs` | The Rust TUI. Big monolithic file by design; uses quadraui primitives. |
| `coordinator.yml` | Single source of truth for repos, machines, dependencies, policies. |
| `~/.coord/coord.db` | Local state cache. Survives across sessions; rebuilt from GitHub on `coord resume`. |
| `~/.coord/logs/<id>.log` | Per-assignment worker log (on the agent machine that ran it). |
| `~/.coord/worktrees/<id>/` | Per-assignment git worktree (on the agent machine). Cleaned up by `coord-tui` press `c` or `POST /worktree-clean`. |
