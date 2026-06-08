# Current Goal — North Star

> **The living, cross-repo / cross-machine objective for the coordinator and every agent it dispatches.**
> This is *meta-level*: above any single issue, repo, or session (and broader than Claude's own per-session goal feature). Both humans and agents may edit it as priorities evolve — keep it short, current, and re-date the Status line. `coordinator.yml` is the source of truth for *topology*; **this file is the source of truth for *intent*.**
>
> _Last updated: 2026-06-07_

## 🎯 North star

**Make human-attended interactive `claude` sessions drivable end-to-end from the coord-tui board** — run the full lifecycle **Work → Test → Review → Smoke-test → Merge** through interactive sessions, not `claude -p` workers.

## Why now (the deadline)

On **2026-06-15** Anthropic begins billing `claude -p` / Agent SDK at full API rates, which kills the "free subscription" worker model. **Human-attended interactive sessions are the ToS-compliant escape hatch** — interactive means a human is attending; automation stays on metered API-key `claude -p`. So this must work *before* June 15 to be useful. (See #322, #437; no TTY scraping — #426 was closed on ToS.)

It hinges on the **embedded terminal feeling like a real terminal** — selection, paste, mouse, scrollback.

## Critical path

| Leg | State | Issues |
|---|---|---|
| **Seed params in** | 🟡 `coord assign --interactive` pastes the briefing (done); needs the TUI launch button to be board-driven | #467 |
| **Paste fallback** | 🟢 merged | #468 |
| **Result out** — verdict/summary/commits back to the board | 🟢 keystone merged (git-floor + `coord report-result` through the IssueStore seam) — agent-side bits pending fleet release | #466, #448 |
| **Terminal feel** | 🟡 mouse/wheel (#454/#455) + Log-tab select+copy (#283, on develop) merged; in-terminal select-vs-forward open | #464, #283, quadraui #293 |
| **Gate reorder** — interactive Work→Test→Review→Smoke→Merge | ⚪ designed | #465 |

## Status (2026-06-07)

- ✅ Landed today: **#466** result-out keystone · **#448** reap honesty · **#468** paste · **#283** Log-tab select+copy (→ develop, confirmed working)
- 📋 Next, in order: **#467** Start-interactive button (makes the lifecycle launchable from the board — the literal north star) · **release + `coord agent update`** to propagate #448/#466 agent-side changes to the fleet · then **#465** gate reorder · then **#464** in-terminal selection · quadraui develop→main to close #283 fleet-wide
- 🛡 Hardening from the 06-07 auto-loop token-burn: **#477** (TUI owns the loop — visible + killable; aligns with TUI-as-control-surface) · **#476** (cap auto-fix at N=2, then human-gate)

## How to use this doc

- **Agents / coordinator brain:** treat this as the standing objective behind all planning and triage. Bias proposals toward unblocking the critical path above; don't silently drift to unrelated backlog.
- **Humans:** edit freely as priorities shift; keep it short, re-date Status. Commit + push so every machine and every agent picks it up (it propagates via git, like all coordinator state).
- **Future:** surface + edit this directly in the coord-tui board, inject it into worker briefings (cross-repo reach), and bias `coord plan` toward it — tracked in **#469**.
