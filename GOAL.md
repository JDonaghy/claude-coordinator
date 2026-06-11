# Current Goal — North Star

> **The living, cross-repo / cross-machine objective for the coordinator and every agent it dispatches.**
> This is *meta-level*: above any single issue, repo, or session (and broader than Claude's own per-session goal feature). Both humans and agents may edit it as priorities evolve — keep it short, current, and re-date the Status line. `coordinator.yml` is the source of truth for *topology*; **this file is the source of truth for *intent*.**
>
> _Last updated: 2026-06-11_

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
| **A3 — interactive Smoke** (`--smoke-of`) | 🔴 **NEXT** | — |
| **Track B — remote fleet** (ssh+tmux; Review/Smoke read-only first) | ⚪ designed | #486, #493/#499 |

## Status (2026-06-11)

- ✅ **Board launches interactive Work/Plan safely**; result-out, tmux resilience, and the two-tier test gate are all merged (#465/#433/#487 now **closed** — GOAL's old "NEXT: #478" was stale).
- ✅ **The interactive Review leg is DONE end-to-end** — A1 (`coord assign --interactive --review-of`, PR #538) + the embedded-terminal `$TMUX` scrub (quadraui PR #360) + A2 (TUI "Start review (interactive)" board action, PR #540) are all **merged**. Proven in the wild: a review launched from coord-tui's Terminal tab renders the Claude Max session **in the pane** (no tmux hijack), reviews the diff, reports via `coord report-result`. A2 verified with `cargo build` + `cargo test` (570 pass) before merge. coord-tui rebuilt + reinstalled.
- 🛡 **Flood control landed** — the #476 decision gate (request-changes with 0 blocking → advance, don't re-dispatch a fix) + incremental re-reviews are live on `main`, validated in the wild on #436. Follow-up persist fix (#537) shipped.
- 🕹 **"Drive it all from the TUI" workstream** (the operator runs the whole lifecycle from the board): **leg 1** — non-interactive Work/Plan restored as a peer of the interactive launchers in the right-click menu (`4e994d8`); **leg 2** — auto-advance **Work → Review** (#517 first cut, `e7f92a8`): when an interactive work session finishes, the board-driven detector raises a one-key confirm to launch the human-attended review (no scraping — watches for a new done-with-branch work aid via `finalize_interactive_exit`; ToS §3.7). Both on `main`, coord-tui rebuilt + installed, 580 tests pass. **Next: leg 3** — verdict-driven routing (pass → pull artifact → merge/rework; fail → rework) + interactive incremental re-review; **leg 4** — all of it over SSH (Track B).
- 📋 **Next, in order:** **A3** interactive Smoke (`--smoke-of`, mirror A1/A2) → **Track B** remote (start with dellserver, read-only Review/Smoke). A1 follow-ups to fold in: the briefing emits both the `REVIEW_VERDICT` block and the report-result reminder (claude used the block); `coord report-result` needs a `--body-file` for full review bodies (see `project_a1_interactive_review`).
- 🧭 **Open design Q — where do automated tests gate?** CI is pytest-only, so Rust repos (tui/quadraui/vimcode) still have no automated gate; they need an explicit `cargo build && cargo test` gate (extend CI, or a pre-merge verify step).

## Horizon (beyond the deadline)

Once the local interactive lifecycle is solid, the direction is **coord-tui as a "control center"**: one developer driving human-attended interactive `claude` sessions across a **fleet of ssh-reachable machines** (cloud VMs / lab boxes over Tailscale or a corp network), to **scale what a single developer can do** — a local box can't run >1 compute-heavy job (Rust `cargo build`/`test`) at once. Two legs sharing **one ssh + tmux substrate**:

- **#486** — remote interactive sessions (revives #446): launch/drive `claude` on a selected remote machine, PTY into the TUI pane.
- **#487** — resilience: host sessions in tmux named sessions so they **survive a control-center crash and are reattachable** (today's local `pty.fork` dies with the TUI).
- **#517 + #518** — pipeline supervisor (stage-end triage + bounded autonomy) + control-center decision UX (quadrant tabs, decision cards): the brain auto-advances the clear transitions and **surfaces only the judgment calls with a recommendation**, so one developer triages a decision queue across many sessions instead of babysitting each. Absorbs #476, builds on #477 + the quadraui tab-groups primitive (#144/#349).

Enabled by #478 (result-out) + #480 (worktree isolation). Possibly a multi-tenant service later (monetization TBD). **Not a June-15 blocker** — the local MVP (#467) is the escape hatch; this is the scale-up. See `project_fleet_control_center_vision` in coordinator memory.

## How to use this doc

- **Agents / coordinator brain:** treat this as the standing objective behind all planning and triage. Bias proposals toward unblocking the critical path above; don't silently drift to unrelated backlog.
- **Humans:** edit freely as priorities shift; keep it short, re-date Status. Commit + push so every machine and every agent picks it up (it propagates via git, like all coordinator state).
- **Future:** surface + edit this directly in the coord-tui board, inject it into worker briefings (cross-repo reach), and bias `coord plan` toward it — tracked in **#469**.
