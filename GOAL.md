# Current Goal — North Star

> **The living, cross-repo / cross-machine objective for the coordinator and every agent it dispatches.**
> This is *meta-level*: above any single issue, repo, or session (and broader than Claude's own per-session goal feature). Both humans and agents may edit it as priorities evolve — keep it short, current, and re-date the Status line. `coordinator.yml` is the source of truth for *topology*; **this file is the source of truth for *intent*.**
>
> _Last updated: 2026-06-08_

## 🎯 North star

**Make human-attended interactive `claude` sessions drivable end-to-end from the coord-tui board** — run the full lifecycle **Work → Test → Review → Smoke-test → Merge** through interactive sessions, not `claude -p` workers. The board *launches* sessions today; the remaining work is the **stage-to-stage handoff** so each stage's result feeds the next.

## Why now (the deadline)

On **2026-06-15** Anthropic begins billing `claude -p` / Agent SDK at full API rates, which kills the "free subscription" worker model. **Human-attended interactive sessions are the ToS-compliant escape hatch** — interactive means a human is attending; automation stays on metered API-key `claude -p`. So this must work *before* June 15. (See #322, #437; no TTY scraping — #426 closed on ToS.)

## Critical path

| Leg | State | Issues |
|---|---|---|
| **Launch from the board** — Work/Plan interactive session, right repo, isolated worktree | 🟢 merged | #467, #480 |
| **Paste fallback** | 🟢 merged | #468 |
| **Terminal feel** — selection, mouse/wheel, paste, scrollback | 🟢 merged | #464, #454/#455, #283 |
| **Result out — basic** — git-floor backstop + `coord report-result` through the IssueStore seam | 🟢 merged | #466, #448 |
| **Result out — MCP handoff** — agent → hosted MCP server → IssueStore, so each stage's result drives the next (e.g. an interactive review session updates the issue → the rework session knows what to fix). The eventual form of result-out; agent still never touches `gh`. | 🔴 **NEXT** | #478, #183 |
| **Gate: manual smoke after review-approve** — reorder to Work→Review→**Smoke**→Merge, with a fail→fix→re-review loop | ⚪ designed | #465 |
| **Reliability — artifact-pull `[a]` badge intermittently missing** | 🔴 bug | #433 |

## Status (2026-06-08)

- ✅ **The board can now LAUNCH interactive Work/Plan sessions safely** — #467 (launch) + Bug A (resolve repo from the selected row, not by number) + #480 (isolated worktree per session) merged to `main`, adversarially reviewed; #464 selection in; `coord-tui` rebuilt + installed.
- 📋 **Next, in order:** **#478 MCP result-out** — the stage-to-stage handoff; the prerequisite for driving the *full* pipeline (not just single sessions) through the board → **#465 two-tier test gate** (manual smoke after review-approve) → **#433** artifact-pull fix.
- 🧭 **Open design Q — where do automated tests gate?** Recommendation: keep automated tests a *work-stage* expectation (workers already run build+test) enforced by **CI on the PR** — but **CI is pytest-only**, so Rust repos (tui/quadraui/vimcode) have no automated gate today; they need an explicit `cargo build && cargo test` gate (extend CI, or a pre-merge verify step) rather than a separate pre-PR stage for Python. The distinct lifecycle **Test/Smoke** stage is the *human* smoke after review-approve (#465).
- 🛡 **Open reliability:** precision agent unreachable (paused — needs an AGENT_OPERATIONS look); a PyPI release + `coord agent update` still pending to propagate agent-side #480/#448/#466 bits to the fleet.

## How to use this doc

- **Agents / coordinator brain:** treat this as the standing objective behind all planning and triage. Bias proposals toward unblocking the critical path above; don't silently drift to unrelated backlog.
- **Humans:** edit freely as priorities shift; keep it short, re-date Status. Commit + push so every machine and every agent picks it up (it propagates via git, like all coordinator state).
- **Future:** surface + edit this directly in the coord-tui board, inject it into worker briefings (cross-repo reach), and bias `coord plan` toward it — tracked in **#469**.
