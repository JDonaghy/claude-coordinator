# Current Goal — North Star

> **The living, cross-repo / cross-machine objective for the coordinator and every agent it dispatches.**
> This is *meta-level*: above any single issue, repo, or session (and broader than Claude's own per-session goal feature). Both humans and agents may edit it as priorities evolve — keep it short, current, and re-date the Status line. `coordinator.yml` is the source of truth for *topology*; **this file is the source of truth for *intent*.**
>
> _Last updated: 2026-06-06_

## 🎯 North star

**Make human-attended interactive `claude` sessions drivable end-to-end from the coord-tui board** — run the full lifecycle **Work → Test → Review → Smoke-test → Merge** through interactive sessions, not `claude -p` workers.

## Why now (the deadline)

On **2026-06-15** Anthropic begins billing `claude -p` / Agent SDK at full API rates, which kills the "free subscription" worker model. **Human-attended interactive sessions are the ToS-compliant escape hatch** — interactive means a human is attending; automation stays on metered API-key `claude -p`. So this must work *before* June 15 to be useful. (See #322, #437; no TTY scraping — #426 was closed on ToS.)

It hinges on the **embedded terminal feeling like a real terminal** — selection, paste, mouse, scrollback.

## Critical path

| Leg | State | Issues |
|---|---|---|
| **Seed params in** | 🟢 mostly built — `coord assign --interactive` pastes the briefing; needs a TUI launch button | #467 |
| **Paste fallback** | 🟡 done, pre-merge | #468 |
| **Result out** — verdict/summary/commits back to the board | 🔴 keystone gap, in build | #466 (git-floor + `coord report-result` through a thin IssueStore seam → MCP later), needs #448 |
| **Terminal feel** | 🟡 mouse/wheel merged (#454/#455); selection in review (#283) + select-vs-forward | #283, #464, quadraui #293 |
| **Gate reorder** — interactive Work→Test→Review→Smoke→Merge | ⚪ designed | #465 |

## Status (2026-06-06)

- ✅ #455 merged (cross-repo terminal-session keying)
- 🔄 #283 selection (re-review) · #448 commits-primitive (running, unblocks #466) · #468 paste (done, pre-merge)
- 📋 Next: land #448 → dispatch #466 keystone · merge #283/#468 → one coord-tui rebuild → live smoke · then #467 button · then #465 gate reorder

## How to use this doc

- **Agents / coordinator brain:** treat this as the standing objective behind all planning and triage. Bias proposals toward unblocking the critical path above; don't silently drift to unrelated backlog.
- **Humans:** edit freely as priorities shift; keep it short, re-date Status. Commit + push so every machine and every agent picks it up (it propagates via git, like all coordinator state).
- **Future:** surface + edit this directly in the coord-tui board so the goal is visible and changeable where planning happens (design under discussion — see issue once filed).
