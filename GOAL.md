# Current Goal — North Star

> **The living, cross-repo / cross-machine objective for the coordinator and every agent it dispatches.**
> This is *meta-level*: above any single issue, repo, or session (and broader than Claude's own per-session goal feature). Both humans and agents may edit it as priorities evolve — keep it short, current, and re-date the Status line. `coordinator.yml` is the source of truth for *topology*; **this file is the source of truth for *intent*.**
>
> _Last updated: 2026-06-14_

## 🎯 North star

**Make human-attended interactive `claude` sessions drivable end-to-end from the coord-tui board** — run the full lifecycle **Work → Test → Review → Smoke-test → Merge** through interactive sessions, not `claude -p` workers. The board *launches* sessions today; the remaining work is the **stage-to-stage handoff** so each stage's result feeds the next.

## Why now (the deadline)

On **2026-06-15** Anthropic begins billing `claude -p` / Agent SDK at full API rates, which kills the "free subscription" worker model. **Human-attended interactive sessions are the ToS-compliant escape hatch** — interactive means a human is attending; automation stays on metered API-key `claude -p`. So this must work *before* June 15. (See #322, #437; no TTY scraping — #426 closed on ToS.)

## Critical path — the interactive-mode migration (target June 12)

The active plan is **migrate the metered pipeline stages to human-attended
interactive sessions** (Claude Max, subscription) launched from the board, with
auto-dispatched `claude -p` kept as a **#524-capped fallback** (not a hard
cutover). The big design insight: interactive Review/Smoke report verdicts via
the already-merged `coord report-result` path — **the #478 MCP server is NOT on
the critical path** (demoted to Horizon).

| Leg | State | Issues |
|---|---|---|
| **Launch Work/Plan from the board** — right repo, isolated worktree | 🟢 merged | #467, #480 |
| **Paste fallback / terminal feel** — selection, mouse/wheel, paste, scrollback | 🟢 merged | #468, #464, #454/#455, #283 |
| **Result out** — git-floor backstop + `coord report-result` through the IssueStore seam | 🟢 merged | #466, #448 |
| **Session resilience** — survive a TUI crash, reattachable (tmux named sessions) | 🟢 merged | #487, #490 |
| **Two-tier test gate** — automated build/test before review, human smoke after approve | 🟢 merged | #465 |
| **A1 — interactive Review dispatch** (`coord assign --interactive --review-of`) | 🟢 merged | PR #538 |
| **In-TUI render** — scrub `$TMUX` from the embedded terminal so interactive sessions render in the pane | 🟢 merged | quadraui PR #360 |
| **A2 — TUI "Review (interactive)" board action** | 🟢 merged | PR #540 |
| **A3 — interactive Smoke** (`--smoke-of`) — testing agent: lists smoke tests, pulls artifact, records verdict | 🟢 merged | #350, #581 |
| **leg 3c — guided approve→test→merge** — test-fail→interactive fix dialog, test-pass→interactive merge agent (`--merge-of`, proactive rebase) | 🟢 merged | #306, #581 |
| **Track B — remote Review** (`--review-of` over ssh+tmux, read-only) | 🟢 merged | #486 (`9e0c5d2`) |
| **Track B — remote Fix** (`--fix-of`: remote worktree + finalize/push-back) | 🟢 merged | #486 (`6c16d3b`) |
| **Track B — TUI machine picker** (drive remote Review/Fix from a board card) | 🟢 merged | #486, #493/#499 |

## Status (2026-06-11)

- ✅ **Board launches interactive Work/Plan safely**; result-out, tmux resilience, and the two-tier test gate are all merged (#465/#433/#487 now **closed** — GOAL's old "NEXT: #478" was stale).
- ✅ **The interactive Review leg is DONE end-to-end** — A1 (`coord assign --interactive --review-of`, PR #538) + the embedded-terminal `$TMUX` scrub (quadraui PR #360) + A2 (TUI "Start review (interactive)" board action, PR #540) are all **merged**. Proven in the wild: a review launched from coord-tui's Terminal tab renders the Claude Max session **in the pane** (no tmux hijack), reviews the diff, reports via `coord report-result`. A2 verified with `cargo build` + `cargo test` (570 pass) before merge. coord-tui rebuilt + reinstalled.
- 🛡 **Flood control landed** — the #476 decision gate (request-changes with 0 blocking → advance, don't re-dispatch a fix) + incremental re-reviews are live on `main`, validated in the wild on #436. Follow-up persist fix (#537) shipped.
- 🕹 **"Drive it all from the TUI" workstream** (the operator runs the whole lifecycle from the board), all board-driven (ToS §3.7 — verdicts/completions come from `coord report-result` + the git-floor backstop, never the session TTY):
  - **leg 1** — non-interactive Work/Plan restored as a peer of the interactive launchers in the right-click menu (`4e994d8`).
  - **leg 2** — auto-advance **Work → Review** (`e7f92a8`): interactive work finishes → one-key confirm launches the human-attended review.
  - **leg 3a** — `coord assign --interactive --fix-of <review_aid>` (`98b6c71`): a human-attended fix that **continues the reviewed branch** (same PR, not an orphan), briefed with the findings, bumping `review_iteration`.
  - **leg 3b** — TUI verdict-routing (`58b06b1`): an interactive review's verdict routes — **request-changes → one-key fix prompt** (→ leg-3a `--fix-of`); **approve → smoke/merge notice**. The re-review gate now fires after a fix, so the **next review is incremental** (the token-waste fix the user flagged). + a "Start fix (interactive)" menu item.
  - All on `main`; coord-tui rebuilt + installed; coord suite 2059 + tui 593 pass.
  - **✅ SMOKED END-TO-END in the wild (2026-06-12, quadraui #287 rounded corners):** interactive Work → interactive Review (approved) → smoke gate → **merged to develop (PR #361)**, driven from the board. Session resilience proven (an accidental Esc didn't lose the work — tmux #487 survived; recovered via `coord reattach`). Smoke fixed the **Esc-quits** bug (`f184726`) and filed 8 follow-ups: #541 (issue fuzzy-finder), #542 (auto-advance resilience across TUI restarts — refs #517), #543 (finalize must record branch), #544 (coord ready add coord label), #545 (refinement leaves work-shaped branch), #546 (cost-per-issue reporting), #547 (briefing readability), #548 (review verdict misrouted to work row → merge gate blind). The manual board nudges needed (record branch, relocate verdict, smoke pass) all map to #543/#548 — once those land the flow is hands-off.
  - **leg 4 (Track B) — remote interactive Review is LANDED + smoked e2e (2026-06-11).** `coord assign --interactive --review-of <work_aid> <remote>` ungated from local-only (`9e0c5d2`): read-only in the remote's LIVE checkout (no worktree — it's the worker-worktree base), reviewer prompt + read-only tools, recorded in the coordinator DB. Verdict relay is operator-on-coordinator (a remote `report-result` writes the wrong DB — the #486d gap), zero release needed. **Proven on dellserver against quadraui #287:** 1-prompt launch → in-pane render → real `git fetch`+diff → a genuinely good independent `REVIEW_VERDICT` (cross-backend Before/After table, macOS Core-Graphics correctness check, caught a real run-on-doc nit). Also shipped a needed SSH `ControlMaster` multiplex fix (`f40f632`): one remote launch fired ~5 unmultiplexed ssh auths → a wall of passphrase prompts; now one connection per launch (smoked: 5 prompts → 1).
  - **leg 4 (Track B) — remote interactive Fix is LANDED + smoked e2e (2026-06-11).** `coord assign --interactive --fix-of <review_aid> <remote>` ungated (`6c16d3b`): a remote worktree on the EXISTING branch (`git worktree add -B <branch> origin/<branch>`) + `finalize_remote_interactive_exit` — on exit the coordinator sshs in, fast-forward-pushes the worktree's commits to origin/<branch>, records the completion through the seam locally (re-review fires), and removes the worktree (PRESERVED on push failure — commits never live only in a deleted worktree). This is the #486d push-back the remote-WORK path deferred. **Proven on dellserver against quadraui #326 (a real request-changes review):** worktree on issue-326 → the worker fixed the cargo-fmt violations + ran cargo test/clippy → pushed → finalize `status=done commits_ahead=3 pushed=True`, worktree removed.
  - **✅ leg 3c + A3 LANDED (2026-06-14):** the testing + merge agents are now driven from the row right-click menu, completing the **Test → Merge** handoff:
    - **Start testing (interactive)** → `coord assign --interactive --smoke-of <work_aid>`: a human-attended testing agent (read-only, live checkout) that surfaces the cached smoke-test plan, offers `coord pull-artifact`, interviews the operator, and records the verdict via `coord test --passed|--fail`.
    - **Verdict routing** (board-driven, never TTY-scraped): a recorded `failed` raises a **fail→fix** confirm dialog → interactive `--fix-of` on the same branch; `passed`/`skipped` raises a **pass→merge** confirm dialog → interactive `--merge-of`. Mirrors the leg-2/3b Work→Review / request-changes→fix prompts.
    - **`--fix-of` generalised (#581):** it now also accepts a WORK id whose Test gate failed (not only a request-changes review), briefing the fix with the recorded failure story — so an *approved* branch that fails a *manual test* reaches the same interactive fix loop.
    - **Start merge (interactive)** → `coord assign --interactive --merge-of <work_aid>`: a merge agent that worktrees the branch, fetches + **rebases onto the default branch (#306 proactive rebase)**, resolves mechanical conflicts (semantic with the operator), runs tests, `git push --force-with-lease`, then hands back to the operator to merge (Go / `coord merge`).
    - All on `main`; coord suite 2062 + tui 599 pass; coord-tui rebuilt + installed.
- 📋 **Next, in order:** local interactive lifecycle (Work→Review→Test→Merge) is now complete end-to-end; **leg 4 cont. is remote Test/Merge over SSH (Track B)**. The merge agent supersedes #306's reactive-only conflict-fix with a proactive interactive rebase; #277/#567 (conflict-fix orphan branch, NULL-branch verdict gate) remain open backend hygiene. A1 follow-ups to fold in: the briefing emits both the `REVIEW_VERDICT` block and the report-result reminder; `coord report-result` needs a `--body-file` for full review bodies (see `project_a1_interactive_review`).
- 🧭 **Open design Q — where do automated tests gate?** CI is pytest-only, so Rust repos (tui/quadraui/vimcode) still have no automated gate; they need an explicit `cargo build && cargo test` gate (extend CI, or a pre-merge verify step).

## Horizon (beyond the deadline)

Once the local interactive lifecycle is solid, the direction is **coord-tui as a "control center"**: one developer driving human-attended interactive `claude` sessions across a **fleet of ssh-reachable machines** (cloud VMs / lab boxes over Tailscale or a corp network), to **scale what a single developer can do** — a local box can't run >1 compute-heavy job (Rust `cargo build`/`test`) at once. Two legs sharing **one ssh + tmux substrate**:

- **#486** — remote interactive sessions (revives #446): launch/drive `claude` on a selected remote machine, PTY into the TUI pane.
- **#487** — resilience: host sessions in tmux named sessions so they **survive a control-center crash and are reattachable** (today's local `pty.fork` dies with the TUI).
- **#517 + #518** — pipeline supervisor (stage-end triage + bounded autonomy) + control-center decision UX (quadrant tabs, decision cards): the brain auto-advances the clear transitions and **surfaces only the judgment calls with a recommendation**, so one developer triages a decision queue across many sessions instead of babysitting each. Absorbs #476, builds on #477 + the quadraui tab-groups primitive (#144/#349).
- **#584** — portable control center: run `coord-tui` from **any** Tailscale machine against one **shared board + config**, instead of pinning the whole control center to whichever host owns `~/.coord/coord.db` + `coordinator.yml` (the 2026-06-14 elitebook-vs-precision friction). Likely a coordination daemon fronting the DB (thin-client TUI/CLI) — the network boundary the `issue_store` seam (#466/#478) + `board --json` projection (#550) were built for; config served from it (or DB-backed) rather than a per-host file. Single-user precursor to the multi-tenant #282.

Enabled by #478 (result-out) + #480 (worktree isolation). Possibly a multi-tenant service later (monetization TBD). **Not a June-15 blocker** — the local MVP (#467) is the escape hatch; this is the scale-up. See `project_fleet_control_center_vision` in coordinator memory.

## How to use this doc

- **Agents / coordinator brain:** treat this as the standing objective behind all planning and triage. Bias proposals toward unblocking the critical path above; don't silently drift to unrelated backlog.
- **Humans:** edit freely as priorities shift; keep it short, re-date Status. Commit + push so every machine and every agent picks it up (it propagates via git, like all coordinator state).
- **Future:** surface + edit this directly in the coord-tui board, inject it into worker briefings (cross-repo reach), and bias `coord plan` toward it — tracked in **#469**.
