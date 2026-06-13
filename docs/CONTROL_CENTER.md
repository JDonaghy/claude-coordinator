# Control Center — coord-tui consolidation

**Status:** design (milestone *TUI: Control Center*, #15). **Epic: #571.**
**Panels:** Backlog #572 · Kanban #573 · Sessions #574 · Conversations #575 ·
quick-peek popup #576. **Gating deps:** #559 (live roster), #551 (key routing).
**Substrate:** #518 (tab-groups). **Carried-forward semantics:** #256.
**Supersedes the framing of:** #256 (two-panel Board ↔ Pipeline). The *label and
transition semantics* in #256 carry forward unchanged; only the panel structure
changes.

---

## 0. North star — what this tool is actually for

The primary job of coord-tui is **not** dispatching work or tracking lifecycle.
It is **keeping the human oriented across many fast-moving Claude sessions, so a
developer can go fast *safely*.** Claude lets you move quickly; the failure mode
is a human who has lost track of what every session is doing. Every surface in
this document is judged against one question:

> **Does this help me not lose the thread?**

Three surfaces answer three different questions, and that division is the whole
design:

| Surface            | Question it answers                       | Orientation role         |
| ------------------ | ----------------------------------------- | ------------------------ |
| **Kanban** panel   | *Where is everything in the lifecycle?*   | nothing silently stalls  |
| **Sessions** panel | *What are the live sessions doing now?*   | the anti-lost core       |
| **Issue-view** tabs| *What is this one doing / meant to do?*   | deep re-orientation      |
| **Conversations**  | *Zoom all the way out — what should I do?* | plan / triage / decide  |

---

## 1. Vocabulary (grounded in quadraui types)

We use quadraui's real component names so design and code share one language.

| Concept (plain)                              | VSCode analogue   | quadraui type                                  |
| -------------------------------------------- | ----------------- | ---------------------------------------------- |
| Left vertical icon strip                     | Activity Bar      | `ActivityBar` / `ActivityItem`                 |
| A **Panel** — one strip entry (tree + detail)| View Container    | `PanelDefinition` in `AppShell` (`active_panel`)|
| **Sections** within a panel's tree           | stacked Views     | `MultiSectionView` / `Section` (+ `HeaderAction`, `InlineInput`, `EmptyBody`) |
| The tree itself                              | Tree View         | `TreeView`                                      |
| Right-side detail + its sub-tabs / splits    | Editor group      | `TabGroup` + `tab_bar`                          |
| Bottom strip (optional)                      | Bottom Panel      | `BottomPanel`                                   |

**The rule that decides panel vs. section:**

> **Same entity, grouped → a Section. Different *entity* or different *layout* → a Panel.**

This is why the Backlog's lifecycle states are *sections* (all issues), while
Machines is its own *panel* (a different entity — a machine cannot be a node in
an issue tree), and Kanban/Sessions are panels (different *layout* — a grid, not
tree+detail).

---

## 2. The ActivityBar — the panels

The old `Board` / `Machines` / `Pipeline` view split collapses into a set of
purpose-built panels:

| Panel             | Entity      | Layout             | Replaces / notes                                  |
| ----------------- | ----------- | ------------------ | ------------------------------------------------- |
| **Backlog**       | issues      | TreeView + detail  | the scoping half of the old Board                 |
| **Kanban**        | issues      | **grid** (board)   | the old Pipeline's execution view                 |
| **Sessions**      | live PTYs   | **grid** of terms  | new — the multi-session "wall" (builds on #518)    |
| **Conversations** | chats       | chat + named ctx   | new — zoom-out planning/triage surface             |
| **Machines**      | machines    | TreeView + detail  | the old Machines view; also a Kanban swimlane key  |
| **Settings**      | config      | form               | later                                              |
| **Reports**       | rollups     | tables/charts      | later (#546 cost-per-issue is the seed)            |

Only **Kanban** and **Sessions** break the tree+detail mould (they are grids);
the `AppShell` panel body must allow a non-tree widget for those two. The rest
are conventional `TreeView` + detail.

---

## 3. The detail area — Issue-view tabs & tab groups

The right-hand detail area is a `TabGroup` of **issue-views**.

- An **issue-view** is an *inseparable bundle of sub-tabs* for one issue:
  **Issue · Log · Pipeline · Stages · Terminal · History (Summary, #558).**
  These sub-tabs cannot be torn apart — they are one welded unit per issue.
- **Tab groups** split the detail area (left/right, top/bottom, 2×2) so several
  issue-views are visible at once — e.g. watch two sessions side by side. Tab
  groups are **layout only**; they carry no semantics (they are *not* milestones
  or swimlanes — the board owns that).
- **Reopenable & stateless.** Five of the six sub-tabs are pure projections of
  server/DB state and reconstruct on reopen for free. The **Terminal** sub-tab
  is the exception — it is a live attach (see §6); it only "reopens the same"
  because the session is tmux-backed (#487, done).
- **Invariant: an issue-view is open at most once** — prevents two tmux clients
  fighting over one session's window size (§6).
- **Entry sub-tab is lifecycle-inferred:** running → Terminal/Log; in review →
  Issue/diff; backlog → Issue body.
- **No hand-editing in-app.** There is never anything to "save". If issue-body
  editing is ever wanted, shell out to `$EDITOR`.
- **Tab count: soft guidance, not a hard cap.** Hint past ~4 ("focus/perf may
  suffer"); let the user open more and learn from their own lag.
- **Do not auto-open tabs.** A 12-worker fan-out must not spawn 12 tabs. Tabs
  are deliberate focus; the **board + a notification rail** carry "something
  changed over there" (see §10).

This is the substrate of **#518** (polymorphic tabs + quadrant tab-groups +
decision lane), which depends on quadraui #144 (TabGroup) + #349
(TabGroupController drag/merge/split).

---

## 4. Backlog panel (tree)

A `TreeView` with `MultiSectionView` sections, ordered by lifecycle:

| Section      | Definition (label-driven, from #256)             |
| ------------ | ------------------------------------------------ |
| **New**      | open, no `status:*` label                         |
| **Refining** | `status:refining`                                 |
| **Refined**  | `status:ready`, no `coord` label yet (groomed, *awaiting your push*) |

Inside each section, group **repo → milestone → issue** so **milestones are not
lost** (the one thing we must preserve from the old Board).

- **Create an issue in-place:** the `Section` `HeaderAction` `[+]` opens an
  `InlineInput` / interactive new-issue chat (reuse `dispatch_board_chat_new_issue`,
  `type="new-issue-chat"`). `EmptyBody` shows a CTA when a section is empty.
- **Per-item context menu:** Refine, Refine-with-chat, Mark Refined, Drop,
  **Send** (→ Kanban). These already exist on the old Board row menu
  (`context_menu_items_for_board_row`); this panel inherits them.
- **Backlog source = ALL open issues in coordinated repos**, not just
  `status:ready` ones. This closes the `status:ready` limbo (#359) and the
  "raw backlog disappears" gap — without it, deleting the old Board would hide
  every un-refined issue.
- **The `status:ready` flip is the boundary to Kanban.** Dragging a card from
  Backlog into Kanban's first column == `coord ready`. Name **"Refined"**
  (groomed, in backlog) distinctly from **"Ready"** (on the board) — they are
  one keystroke apart in meaning and will be conflated otherwise.

---

## 5. Kanban panel (grid)

Columns are lifecycle **stages**; cards are issues.

```
 Ready    │ Work     │ Review    │ Test     │ Merge     │ Done
 (drag-in │ (worker- │ (worker-  │ (gated:  │ (queue +  │
  =coord  │  driven) │  driven)  │  Pass)   │  CI)      │
  ready)  │          │           │          │           │
```

- **Swimlanes are a pluggable key:** **Milestone** (default) | **Machine** |
  **Repo**, toggled. A **"No milestone"** lane must exist (freshly-created
  issues have no milestone yet). Machine-as-swimlane is how "what's running
  where" surfaces without a separate panel.
- **Column transitions are mixed — be explicit per column:**
  - **Ready** is the only freely drag-*into* column (the gesture = `coord ready`).
  - **Work / Review / Test / Merge** are **worker- or gate-driven**, not free
    drag. The UI must *not* afford dragging a card forward into them, or it
    implies you can push work that only a worker can advance.
- **A card shows its *latest active stage* + a blocker/bounce badge.** Kanban
  assumes one-card-one-column; our workflow is non-linear (request-changes
  bounces Review→Work→Review; merge-queue stalls). So:
  - badge a bounced card (`⚠ bounce`) so "already failed review" isn't hidden,
  - badge a merge-stuck card with *why* (CI red / test-gate not passed / queue
    clog) — this is the "Go does nothing" class of confusion
    (`docs/ARCHITECTURE.md`), and a bare "Merge" column hides it.
- **Done** needs archival / "recent" scoping or it grows unbounded.
- **Card click → opens that issue's issue-view as a tab** in the detail area.

Carries forward the transition table and label definitions of **#256**.

---

## 6. Sessions panel — "the wall"

The surface the developer actually lives in: watch many live interactive
sessions at once, like a tmux video wall.

```
 roster (all live, fleet-wide)        composed grid (your pick)
 ● #514 vimcode@precision   │  #514 vimcode@precision·Work ▶   #532 coord@local ▶
 ● #532 coord  @local       │  ┌─────────────────────────┐   ┌────────────────┐
 ● #561 coord  @dellserver  │  │ live terminal           │   │ live terminal  │
 ○ #560 coord  @local       │  └─────────────────────────┘   └────────────────┘
 ○ #207 vimcode@azure       │  #561 coord@dellserver·Work     ┏━━━━━━━━━━━━━━┓
  ●=on wall ○=available      │  ┌─────────────────────────┐   ┃ FOCUSED       ┃
  click ○→add  ●→remove      │  │ …                       │   ┃ proceed? y/n_ ┃
                             │  └─────────────────────────┘   ┗━━━━━━━━━━━━━━┛
```

**Structure: roster + composed grid.**
- The **roster** lists every live session fleet-wide, from `coord sessions
  --remote` (parallel fleet probe, already built), each tagged `@machine`.
- The **grid** shows the *subset you pin*. Add/remove/swap freely. Removed
  sessions **keep running detached** (the tmux-backing payoff, #487) — dropping
  a tile stops watching, it does not kill the work.

**Tiles are live and focusable (corrects an earlier read-only proposal).**
- The **focused tile receives your keystrokes directly** — click a tile, type
  `y`, move on. No zoom required for quick answers. This is exactly tmux pane
  focus, and it is the core interaction.
- **Key routing needs a prefix.** When a tile is focused, wall-control keys
  (switch tile, fullscreen) must not leak into the PTY — that leak *is* bug
  **#551** (picker digit-keys reaching the terminal). A tmux-style **prefix**
  (or explicit focus/command mode) is required from day one.
- **Fullscreen** one tile with `prefix+z` (resizes the same attacher; not a
  second client). Optional real-estate, never a gate on input.

**Fleet (5+ machines).** A tile == "attach to `coord-<aid>` on machine M":
local `tmux attach`, remote `ssh -t M tmux attach`. SSH ControlMaster
multiplexing (`_SSH_MUX_OPTS`, already built) keeps N remote tiles to one host
on a single connection. Each remote tile costs an ssh+PTY → **curate, don't
show-all** at fleet scale.

**Pairing / 2-person team — split into two layers:**
- **Watching together is nearly free** via tmux's native multi-client: both
  operators attach to the same `coord-<aid>` (one local, one over ssh); tmux
  mirrors. Either can drive (last-writer-wins → social "who's driving"), or the
  observer attaches read-only (`tmux attach -r`). Both TUIs see the same roster
  because both probe the same fleet.
- **Shared board / state / auth is a separate epic — #282** (multi-user/team
  mode: centralized state, OAuth, agent TLS). Out of scope here.

**Sizing footnote.** The two-clients-fight-over-size issue only exists in the
pairing case (tmux shrinks to the smaller client — acceptable). Solo, each
session has exactly one attacher (its tile) — no conflict.

**Issue-content quick-peek (point 2 — "zoom back in").** Before answering a
prompt or judging a review failure, you need to know *what this session is
working on*. From a focused tile, a **scrollable popup** shows that one issue's
content/intent — **one issue at a time**, no need to leave the wall or open a
full issue-view tab. ("Is this review failure something I care about?" is
answerable in two seconds.) See §8.

**Dependencies:** #559 (live roster refresh — a startup-only roster is useless),
#551 (key routing), #486 (remote sessions), #491 (session hygiene/reaping),
builds on #518 (tab-groups substrate). Team layer: #282.

---

## 7. Conversations panel (point 1 — "zoom all the way out")

A place inside the TUI for **conversations like this one** — design, planning,
triage, "what should I work on next", "help me think about X" — that are *above*
any single issue or session.

- **Driven from inside the TUI**, as a human-attended interactive Claude session
  (Claude Max, not metered `claude -p`) — same tmux-backed session machinery,
  but seeded with a *context bundle* instead of a code briefing.
- **Pre-seeded with context.** A conversation can be opened with selectable
  context injected: repo + `CLAUDE.md`, `GOAL.md`, the graphify graph, current
  board/Kanban state, the selected issue or milestone.
- **User-defined named contexts.** The user can define and name the kinds of
  conversation they have regularly — e.g. *"Design review"*, *"Triage the
  backlog"*, *"Plan a milestone"*, *"Daily standup"* — each a saved template of
  **(seed prompt + which context to inject)**. Pick a named context and the
  conversation opens primed.
- **Closes the loop.** A conversation can spawn issues / refinements back into
  the Backlog (this very session is the worked example: it should have been one
  click from a "Design review" context to here).

This is the highest zoom level; it is its own entity (a chat, not an issue or a
session) and its own layout → its own panel.

---

## 8. Issue-content quick-peek popup (point 2)

A small, cross-cutting primitive used from the Sessions wall **and** Kanban
cards: a **scrollable popup that renders one issue's content** (title, body,
acceptance, latest review verdict/comment) without navigating away.

- **One issue at a time** — it is a peek, not a workspace.
- Invoked from a focused Sessions tile or a Kanban card.
- Answers "what am I working on / do I care about this failure?" in-flow.
- Distinct from the full **Issue** sub-tab (which lives inside a heavyweight
  issue-view tab); the popup is the lightweight, in-context version.

---

## 9. Machines panel

- **Fleet health, glanceable:** reachable? agent up? version drift (the
  recurring editable-vs-PyPI failure)? This wants to stay *visible*, possibly a
  status strip/header rather than only a collapsible node.
- **Per-machine running sessions** (what each box is doing).
- The "what's running where" view is *also* available as Kanban
  **swimlane-by-machine** — same data, different question.

---

## 10. Cross-cutting

- **Notification rail / tab badges.** Because tabs are deliberate (not
  auto-opened), backgrounded state changes must surface elsewhere: a thin rail +
  per-tab badges ("review landed on #514", "worker stuck on #532"). This is the
  generalization of the existing `▶` liveness dot, and it is what keeps the
  "watch many, focus few" model from regressing observability (#518's decision
  lane is the richer version of this).
- **tmux-back-everything is the keystone**, and it is already true: #487 made
  local sessions tmux-backed (`coord-<aid>`), survivable, reattachable. The
  whole multi-pane / reopen / wall vision stands on it.

---

## 11. Dependencies & suggested sequencing

1. **#559** — live session-roster refresh (today it is startup-only). *Gate for
   the Sessions wall*; also fixes reattach staleness.
2. **Backlog source = all open issues** (§4) — prerequisite before retiring the
   old Board, or raw backlog vanishes (#359).
3. **Backlog panel** + **Kanban panel** — the issue-side consolidation (reuse
   #256 semantics).
4. **#551 / key-routing prefix** — gate for interactive Sessions tiles.
5. **Sessions wall** (§6) — on top of #518 tab-groups, #486 remote, #491 hygiene.
6. **Issue-content popup** (§8) — small; unblocks "decide in-flow" on the wall.
7. **Conversations panel + named contexts** (§7).
8. Later: **Machines** strip refinement, **Settings**, **Reports** (#546).

---

## 12. Open questions

- Swimlane-key default and whether it persists per-user.
- Same-issue sub-tab split (tear-off a single sub-tab) — deliberately
  foreclosed by the welded-bundle rule; revisit if "terminal next to its own
  log" becomes a real want.
- Done-column archival policy (recent N / by date / collapse).
- Tab-group layout persistence across TUI restart (cheap for projections; gated
  on tmux for the Terminal tab).
- Named-context schema for the Conversations panel (what context sources, how
  stored, how shared in team mode).
