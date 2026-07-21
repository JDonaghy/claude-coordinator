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

## The control-center daemon (`coord serve`, #584) and the "no orchestration daemon" choice

There is **no autonomous orchestration daemon** — nothing drives work on its own; a human (or a periodic `coord notify`) always advances the loop. What *has* been added (#584, the "portable control center") is an optional **board-serving daemon**, `coord serve` (port 7435): it fronts the one canonical SQLite DB + `coordinator.yml` on an always-on host (e.g. dellserver) and serves the board (`GET /board`) + config (`GET /config`) and records results (`POST /result`, `/completion`) over Tailscale, so `coord` and `coord-tui` on *any* machine render and drive the **same** board as bearer-token thin clients instead of each owning a local DB. Consequences:

- **State lives in SQLite + GitHub.** `~/.coord/coord.db` (owned by the daemon host when `coord serve` runs, else local) is the cache; GitHub issue comments are the durable source of truth. Either reconstructs the other.
- **`coord notify` still has to be fired periodically.** The daemon serves state; it does not drive the loop. `notify` polls each agent for completion, posts the GH comments, and triggers the auto-loop (review-on-completion, fix-on-request-changes, re-review-on-fix-completion). Without it, the pipeline visibly freezes — agents finish work but no one notices. Run it on a cron, a `watch`, a TUI timer, or by hand.
- **`coord` and `coord-tui` are peers, not nested layers.** The TUI does not shell out to `coord`. Both are independent clients of the same state — directly against SQLite, or (with `coord serve`) against the daemon's HTTP API. (See the [Divergence risk](#divergence-risk) section.)

## The web dashboard (`coord web`, port 7434) and Phone Control Center

`coord web` is an optional **web dashboard** that serves two things from the same port:

1. **The React PWA** (Phone Control Center) — a mobile-optimised single-page app for reviewing pipeline status and triggering gate actions from a phone over Tailscale. Built separately from source (`npm run build` in `coord/dashboard/webapp/`) and served from `dist/`.
2. **The JSON REST API** — `GET /api/pipeline`, `POST /api/pipeline/action`, `GET /api/board`, etc. — called by the React app, and also directly curl-able.

Like `coord-tui`, `coord web` is a **peer client** of the same shared state (`~/.coord/coord.db`). Run it on the always-on host so phones can reach it via Tailscale at `http://<hostname>:7434`.

**Full runbook** (build → serve → phone access → API reference → ToS posture): **[docs/PHONE_WEBAPP.md](PHONE_WEBAPP.md)**.

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
3. `coord notify` (run periodically) polls laptop, sees the completion, posts a GH comment. **Review auto-dispatch is gated on the Test stage:** the default pipeline order is `Work → Test → Review → Merge`, and when `default_gates` orders `"test"` *before* `"review"` (the default — `PipelineConfig.test_precedes_review()`), `dispatch_pending_reviews` only fires once the work has a `passed`/`skipped` Test verdict — recorded with `coord test <work_id> --passed|--skipped` (or the **P/S** keys on the Test stage in the TUI). A work assignment left at *Pending Test* gets no review, so it never merges and the TUI "Go" does nothing — this is the single most common reason a story silently stops progressing. (A `failed` test routes to a fix, not a review.) The explicit `coord review`/`coord pr` paths bypass this gate so a human can always force a review. With the gate satisfied (and `reviews.enabled`), `dispatch_review` sends a `type="review"` assignment to a *different* machine.
4. Reviewer reads the diff, runs tests, posts `gh pr review` with `--approve` / `--request-changes` / `--comment`. The reviewer's log carries a machine-parseable verdict header.
5. `coord notify` runs again. Sees the review completion, parses the verdict, persists `review_verdict` and `review_findings` to the DB.
6. **If `request-changes`**: `run_for_review_transition` calls `_dispatch_fix`, which posts a `type="work"` `[fix-N]` assignment with `target_branch=<original work's branch>` so the fix lands on the same branch (not an orphan).
7. **If `approve`**: nothing happens immediately — the merge gate (`has_approved_review`) will allow the merge next time `coord merge` runs.
8. Fix worker finishes. `coord notify` detects the completion (via the `fix_completions` classifier added in #278), calls `run_for_fix_transition`, which dispatches a fresh review against the fix-1 assignment.
9. Re-review approves → merge gate passes. `coord merge` rebases and merges. Conflict on rebase → conflict-fix worker auto-dispatched (`#241`), pinned to the original branch via `target_branch` (`#277`), pushes the rebase, merge re-enqueues, succeeds.

If any single link is broken — most often `coord notify` not running — the whole loop stalls. The TUI helps spot this because completed assignments without comments are visible in the pipeline view, but the fix is always "run `coord notify`."

> **Updated (2026-07-20):** the flat, stage-serial loop above is still what runs for a standalone
> issue. What has since **shipped on top of it** is the milestone tier of
> **[`PIPELINE_V2.md`](PIPELINE_V2.md)** — this issue loop now nests inside a milestone pipeline with
> Gates A–D (`coord acceptance mock`, `coord milestone gate-b`/`gate-c`/`ship`), and the develop +
> feature-branch model (#934) that routes each milestone's issues onto a `feature/ms-NN` branch. See
> [The milestone tier and the branch model](#the-milestone-tier-and-the-branch-model-934) below.
> **[`ORACLE_LOOP.md`](ORACLE_LOOP.md)** — the tight in-session loop where the worker iterates against
> a sealed, independently-authored acceptance oracle and the coordinator re-runs it externally as the
> trust gate — is available (`coord acceptance author`/`run`/`record`) but is **opt-in**, not the
> default path for new work.

## The milestone tier and the branch model (#934)

The auto-loop above sequences a *single* issue. For work that spans many issues, that loop **nests inside a milestone pipeline** ([`PIPELINE_V2.md`](PIPELINE_V2.md)) so the expensive gates (architecture review, acceptance authoring) are paid **once per milestone**, not once per issue.

**What an epic is.** A GitHub tracking issue carrying the `epic` label (`TRACKING_ISSUE_LABEL`, `coord/milestone_order.py`) whose body holds a `## Work order` block — a DAG of children written as `- #762 {group: A, after: #761}` (`group` = parallel cohort, `after` = hard dependency). Membership is backed by GitHub's native **sub-issues API** (`coord/parentage_github.py`; the older `## Sub-issues` checklist is migrated by `coord milestone sync`, #1061). The `coord milestone` group (`coord/commands/milestone.py`) drives it: `chat` (steward session to draft the order), `write-order`/`order` (write/read the DAG), `dispatch` (promote the ready frontier into the pipeline, draining as `after` deps clear), and the gate commands below.

**The four gates** wrap the whole milestone (distinct from the per-issue Work → Test → Review → Merge stages):

| Gate | Command | Enforces |
|---|---|---|
| **A — contract** | `coord acceptance mock` | A mock-first black-box `contract.md` exists on the default branch before any issue dispatches — checked by `gate_a_status` (`coord/milestone_dispatch.py`) for repos with an `acceptance.drivers` entry; repos without one skip the gate. |
| **B — architecture** | `coord milestone gate-b` (#933) | An independent `type=review` confirms the *assembled* milestone was built to the Gate-A contract. `request-changes` = bounce, not ship. |
| **C — acceptance** | `coord milestone gate-c` (#932) | The full accumulated acceptance suite is green (integration gaps *between* issues that per-issue runs miss). |
| **D — ship** | `coord milestone ship` (#934) | Merges `feature/ms-NN → develop`, gated on Gate B (`approve`) + Gate C (re-run live). |

**The branch model** (`coord/branch_model.py`, opt-in via a repo's `develop_branch:` config, `coord/config.py`). When `develop_branch` is set on a repo *and* an issue belongs to a GitHub Milestone:

- each milestone gets one `feature/ms-NN` integration branch off `develop` (`feature_branch_name` → `feature/ms-{n}`; created idempotently by `ensure_feature_branch_exists` before dispatch);
- that milestone's issues branch off `feature/ms-NN` and their PRs merge **back into it**, not into `main`;
- `feature/ms-NN → develop` happens only via Gate D (`coord milestone ship`); `develop → main` is a separate, un-automated release cut.

`resolve_base_branch(repo, milestone_number)` is the single resolver — it returns `feature/ms-NN` only when `develop_branch` is set and a milestone number is present, else `repo.default_branch or "main"`. It is **fail-open**: a repo that never sets `develop_branch`, or an issue with no milestone, resolves to exactly today's single-branch `main` flow, with no extra `gh` call. The resolver is threaded through the five branch-deciding seams — Work dispatch (`coord/dispatch.py`), review/diff base (`coord/review.py`), merge target (`coord/merge_queue.py`, `coord/commands/merge.py`), reconcile (`coord/reconcile.py`), and the auto-loop (`coord/auto_loop.py`) — each guarded on `repo.develop_branch` so the default path is untouched. (The interactive `--review-of`/`--merge-of` surfaces are deliberately **not** yet wired into the milestone base — a documented follow-up.)

## Observability: `coord usage` and `coord audit`

Two read-only commands surface what the fleet did, both routed through the same board seam as `coord status` (daemon when `coord serve` is up, local DB otherwise) so a thin client never opens `~/.coord/coord.db` directly.

- **`coord usage`** (`coord/commands/status.py`, aggregation in `coord/usage_rollup.py`) — per-assignment/model/issue **cost, tokens, and wall-clock time**, over a time window (`--today`/`--week`/`--month`/`--since`) and grouped by issue, repo, or time bucket (`--by-issue`/`--issue N`/`--by repo|week|month`/`--by-time`). Cost is the captured `cost_usd` when real, else estimated from token counts × `PricingConfig` rates.
- **`coord audit`** (query surface `coord/commands/audit.py`, store `coord/audit.py`) — a durable, append-only, keyset-paginated **event log**: dispatch, verdicts, merges, notifications. `record_audit()` is invoked at the `state._*_local` / `issue_store` **write choke points** (e.g. `_record_test_verdict_local`), so there is one row per real transition regardless of topology, and the write is best-effort — it never raises into the caller (a board mutation must succeed even if the audit write fails). Event names reuse the `coord:event=` vocabulary from `coord/comments.py` so the audit log and the GitHub message bus agree.

## When a merge isn't happening

A story that won't merge — the TUI "Go" does nothing, `coord merge` skips it, the box stays grey/pending — almost always traces to one of these gates. Check in order:

1. **Test gate (the #1 cause).** No review is dispatched until the work's Test stage has a verdict (see step 3 of the auto-loop above). **Symptom:** work `done`, but no `type="review"` assignment exists and `review_state` is null. **Fix:** `coord test <work_assignment_id> --passed` (`--skipped` for trivial, `--fail --reason "…"` for broken), then `coord pr <id>` opens/reuses the PR and dispatches the review. In the TUI: **P / S / F** on the Test stage.
2. **Review not approved.** The merge gate is `has_approved_review` — a `type="review"` assignment with `review_verdict="approve"` for the work behind the queue entry. No review or `request-changes` → merge refuses with *"review required but not approved"*.
3. **CI red.** Merge is gated on `gh pr checks` (#240). Failing/pending checks block it (surfaced in the queue entry's `error`). `coord merge --force-merge` overrides.
4. **PR conflicts.** `mergeable=CONFLICTING` → `coord merge` auto-dispatches a conflict-fix worker (#241) to rebase; on success it re-enqueues and merges, on a semantic conflict it marks the entry `HUMAN_REQUIRED`. This worker runs invisibly — check for a `type="conflict-fix"` running assignment before assuming nothing happened.
5. **Queue clog / group halt.** `coord merge` processes each `(repo, target_branch)` group together; pre-#292 it `break`s on the first blocked entry (now skip-and-`continue`). A queue full of stale entries (for already-closed issues) can stall everything behind them. To merge one issue past a clog: `coord merge --repo <r> --order <assignment_id>` jumps it to the front. To declog: delete `merge_queue` rows whose GitHub issue is already closed — they are never auto-pruned (the closed-issue filter only blocks *new* enqueues).
6. **Post-bounce keying (#292).** After a review bounce (request-changes → fix → approve), the queue entry can be keyed to the *original* (request-changes) work while the approval sits on the *fix* assignment, so `has_approved_review` fails. Fixed in #292; the pre-fix manual workaround was re-keying `merge_queue.assignment_id` to the approved fix.

**Live-on-pull vs needs-release:** the merge/review/auto-loop logic (`merge_queue.py`, `auto_loop.py`, `reconcile.py`, `cli.py`) runs in fresh `coord` CLI invocations, so a `git pull` of the coordinator clone makes fixes live immediately. Only agent-side code (`agent.py` / `agent_app.py`, the long-running `coord agent` service) needs a release + `coord agent update` — see [AGENT_OPERATIONS.md](AGENT_OPERATIONS.md).

## When an issue is sitting in the pipeline you never dispatched

Board vs Pipeline membership is **label-driven, not assignment-driven**. An open issue with *zero* assignments can still show up in the Pipeline — because it carries a `status:ready` label. The lifecycle (defined in `coord/cli.py`'s `refine`/`ready`/`backlog` commands and mirrored in `tui/src/app.rs`) is:

| State | Signal | Where it shows |
|---|---|---|
| **Backlog** | `coord` label, **no** `status:*` label, no assignments | Board sidebar |
| **Refining** | `status:refining` | Board sidebar |
| **Refined / Ready** | `status:ready`, no assignments | **Pipeline** (a "pending / ready-to-dispatch" card) |
| **In-progress** | has a `type="work"` assignment | Pipeline |
| **Done** | merged | Pipeline (Done group) |

So the "ready-but-not-started" state — `coord` + `status:ready`, no work assignment — is a Pipeline card that looks dispatched but isn't. **The refinement chat and new-issue chat flows finalize by flipping `status:refining → status:ready` (via `coord ready`), which silently parks the issue in this limbo.** That's how issues "appear in the pipeline" without anyone dispatching them.

**To drop one back to the Board:** `coord backlog <repo> <issue>` strips `status:refining`/`status:ready`, returning it to unscoped Backlog. It's symmetric with `coord refine` / `coord ready`, and writes through to the local `issues` cache so the TUI reflects it on the next refresh. (The TUI's right-click *Drop to Backlog* fires the same command — #266.)

**Known gap (#359):** refinement/plan-only issues get stranded in the Pipeline this way; the desired fix is for the chat dialogs to route straight to **Plan** or **Work** instead of leaving an issue in the `status:ready` limbo stage.

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

**That next step has since shipped in part:** `coord serve` (#584) makes the daemon the canonical board holder with the CLI and TUI as thin clients — but the *gate logic* still lives in both languages (the daemon serves rows, it doesn't yet centralise the state-machine rules). Closing the remaining drift is now tracked as explicit tech debt: a `BoardService` facade (#749) and generating the wire types from one schema so the Rust/TS mirrors can't diverge — #748 hardens the `/board` parse (the blank-board class), #750 removes the hand-mirror. See the Tech Debt milestone (epic #751).

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
| `coord/branch_model.py` | #934 develop + feature-branch resolver (`resolve_base_branch`, `ensure_feature_branch_exists`); opt-in via a repo's `develop_branch`. |
| `coord/commands/milestone.py`, `coord/milestone_order.py`, `coord/milestone_dispatch.py`, `coord/milestone_chat.py` | The milestone tier: `## Work order` DAG, frontier dispatch, Gates A–D, steward chat. |
| `coord/usage_rollup.py`, `coord/usage.py` | `coord usage` cost/token/time aggregation over the board. |
| `coord/audit.py`, `coord/commands/audit.py` | Durable event log: `record_audit()` at the state write-waist; `coord audit` query surface. |
| `coord/review.py` | Adversarial review dispatch + verdict parsing. `dispatch_pending_reviews()` is the **bulk** path used by `reconcile()` and `coord notify`: it bounds dispatch with a per-pass cap (`reviews.max_auto_dispatch_per_pass`, default 5) and a **surge gate** (`reviews.flood_threshold`, default 12 — above it, refuse all and require `reviews.allow_review_flood: true` / `COORD_ALLOW_REVIEW_FLOOD=1`). This is the flood guard: a backlog "unmasking" (e.g. dropping a gate that had suppressed reviews) can't fire hundreds of metered reviews at once. See the 2026-06-08 incident. |
| `coord/state.py`, `coord/db.py` | SQLite schema and access helpers. |
| `coord/dashboard/` | The web dashboard (`coord web`). Optional. |
| `tui/src/app.rs` | The Rust TUI (uses quadraui primitives). Historically one ~48k-line file; being decomposed into an `app/` module — see the Tech Debt milestone (epic #751 / #742–#745). |
| `coordinator.yml` | Single source of truth for repos, machines, dependencies, policies (incl. `pipeline.default_gates`). Canonical location `~/.coord/coordinator.yml`; resolved `$COORD_CONFIG` → `~/.coord/coordinator.yml` → `./coordinator.yml`, so a machine needs no repo checkout. `coord config` / `coord serve` print the resolved path. |
| `~/.coord/coord.db` | Local state cache. Survives across sessions; rebuilt from GitHub on `coord resume`. |
| `~/.coord/logs/<id>.log` | Per-assignment worker log (on the agent machine that ran it). |
| `~/.coord/worktrees/<id>/` | Per-assignment git worktree (on the agent machine). Cleaned up by `coord-tui` press `c` or `POST /worktree-clean`. |
