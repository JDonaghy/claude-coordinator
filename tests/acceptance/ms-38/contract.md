# Gate A Contract — ms-38: Plans panel → rich client

_Mock-authored 2026-07-19 for milestone #38 (tracking issue #1120)._  
_Issues in scope: **#1122** (detail pane) · **#1123** (right-click menus) · **#1124** (help overlay + palette)._  
_CC-1 (#1121, repo→plan tree sidebar) **already shipped** — its surface is the baseline; this contract covers the three remaining children._

---

## 1. Panel registration (shell_config)

| Field | Value |
|---|---|
| `PanelDefinition.id` | `"panel:plans"` |
| `PanelDefinition.icon` | `"◆"` |
| `PanelDefinition.tooltip` | `"Plans"` |
| `PanelDefinition.title` | `"PLANS"` |
| Activity bar position | After `"panel:mergequeue"` (`≣`), before `"panel:sessions"` (`◉`) |
| `SidebarView` variant | `SidebarView::Plans` |

**Testable:** `shell_config()` panels list contains a `PanelDefinition` whose `id.as_str() == "panel:plans"` and `icon == "◆"`.

---

## 2. Sidebar tree — shipped baseline (CC-1 #1121)

The sidebar tree is already live post-CC-1. Pinned here for the test-author's reference.

| Node kind | Rendered text (must contain) | Notes |
|---|---|---|
| Root | `"All repos"` | Always present; selects unscoped view |
| Repo (collapsed) | `"▶ ◇ <repo>"` + badge | `▶` = collapsed, `◇` prefix distinguishes from main panel headers |
| Repo (expanded) | `"▼ ◇ <repo>"` + badge | `▼` = expanded |
| Milestone leaf (tracked) | `"● #<N> <title>"` | `●` = has `## Work order` |
| Milestone leaf (untracked) | `"○ #<N> <title>"` | `○` = no `## Work order` |

Repo badge: `"[<count> ⚠<attn>]"` when attention count > 0; `"[<count>]"` otherwise.

**Testable:** `driver.screen_contains("All repos")` is `true` when `active_view == SidebarView::Plans`.  
**Testable:** `driver.screen_contains("◇ claude-coordinator")` is `true` (sidebar repo node, using the `◇` prefix).

---

## 3. CC-2 (#1122) — rich in-app detail pane

### 3a. Trigger

Pressing **Enter** on a selected plan row whose `tracking_issue` is `Some(_)` opens the detail pane in the main area **instead of** spawning `gh issue view --web`.

The detail pane is the full main-area content (not a sub-split of the list — the list itself is replaced or pushed offscreen by the detail). Pressing **Esc** returns to the list view.

### 3b. Detail pane — required header strings

For a plan with `tracking_issue = Some(N)`, the detail pane must show:

| String | Source | Required to contain |
|---|---|---|
| `"#<N>"` | `entry.milestone_number` | yes — milestone number in header |
| `"<title>"` | `entry.title` | yes — milestone title in header |
| `"epic:#<tracking>"` | `entry.tracking_issue` | yes — tracking issue ref |
| `"<pct>% done"` | `entry.done / entry.total` | yes — when `entry.total > 0` |

Example header for fixture milestone #38:  
**Required:** `driver.screen_contains("#38")` and `driver.screen_contains("Plans panel -> rich")` and `driver.screen_contains("epic:#1120")` and `driver.screen_contains("% done")`.

### 3c. Detail pane — work order checklist

When the plan has `has_work_order == true`, the detail pane renders a work-order checklist. Each child issue row renders one of:

| Glyph | Meaning | When |
|---|---|---|
| `✓` | done | issue closed |
| `▶` | in-flight | active assignment or remote branch |
| `·` | ready | on ready frontier |
| `—` | blocked | blocked by unmet deps |

**Required strings in the checklist area:** `"Work order"` (section heading) and at least one of `"✓"`, `"▶"`, `"·"`, or `"—"` in a work-order row.

### 3d. Detail pane — actions row

The detail pane renders an actions row. Required action labels (exact strings):

| Label | Purpose |
|---|---|
| `"Dispatch next"` | `dispatch-milestone-next` action |
| `"Open chat"` | `open-milestone-chat` action |
| `"View DAG"` | `view-milestone-order` action |
| `"Edit"` | `edit-milestone` action |
| `"Open in browser"` | demoted `gh issue view --web` action |

**Required:** `driver.screen_contains("Open in browser")` is `true` when detail pane is showing.  
**Required:** `driver.screen_contains("Dispatch next")` is `true` when detail pane is showing.

### 3e. Detail pane — data fields

The detail pane pulls all data from `PlanRosterEntry` fields already on `/board`. If per-child work-order issue detail (e.g. individual issue titles/states) is not present on the current `/board` payload, the implementor must extend `coord/serve_app.py`'s plan_roster projection and `coord/plans.py` to add those fields — **wire shape changes must be mirrored in `PlanRosterEntry` in `tui/src/app/types.rs`** (a mistyped field blanks the board per #632).

_Open question (see Notes §8.1):_ Whether per-child issue detail is added to the wire format is an implementation decision. The acceptance test only verifies the strings in §3b–3d above, not the exact position of work-order rows.

### 3f. Status bar when detail pane is open

**Required:** `driver.screen_contains("Esc=back")` is `true` when the detail pane is open.  
**Required:** `driver.screen_contains("Open in browser")` or `driver.screen_contains("Enter=detail")` may be absent — the hint set changes when the detail pane replaces the list.

---

## 4. CC-3 (#1123) — right-click menus everywhere

### 4a. Drop the `tracking_issue` gate

The gate at `context_menu_target_for_selection` (currently `tui/src/app/dialogs.rs` ~line 904) that filters `SidebarView::Plans` rows to only those with `tracking_issue: Some(_)` must be **removed**. After this change, **every** plan row is right-clickable.

### 4b. Right-click on a stub row (no tracking epic)

A plan row with `tracking_issue == None` (or `has_work_order == false` and no epic) shows a menu containing:

| Item | Action ID |
|---|---|
| `"Create work order / promote to epic…"` | TBD — new action |
| `"Refresh"` with shortcut `r` | `"refresh"` |

**Required:** `driver.screen_contains("Create work order / promote to epic…")` is `true` after right-clicking a stub row.

### 4c. Right-click on a repo header row or empty space

Right-clicking the repo group header (e.g. `▾ claude-coordinator  (2 tracked)…`) or empty space in the main panel shows:

| Item | Action ID | Description |
|---|---|---|
| `"New plan > Quick capture"` | `"capture-plan-quick"` | fires `coord milestone capture` |
| `"New plan > Guided chat…"` | `"capture-plan-chat"` | fires `coord milestone chat --new` |
| `"Refresh"` with shortcut `r` | `"refresh"` | |

**Required:** `driver.screen_contains("New plan > Quick capture")` is `true` after right-clicking a repo header.  
**Required:** `driver.screen_contains("New plan > Guided chat…")` is `true` after right-clicking a repo header.

### 4d. Right-click on a full epic row (has tracking issue)

A plan row with `tracking_issue: Some(_)` shows the full CRUD menu. Required items (exact label strings, inherited from `milestone_dag_context_menu_items`):

| Label | Action ID | Shortcut |
|---|---|---|
| `"Open milestone chat"` | `"open-milestone-chat"` | — |
| `"Dispatch milestone"` | `"dispatch-milestone"` | `d` |
| `"Dispatch next…"` | `"dispatch-milestone-next"` | — |
| `"View order / DAG"` | `"view-milestone-order"` | — |
| `"Edit milestone…"` | `"edit-milestone"` | — |
| `"Add issue to milestone…"` | `"add-issue-to-milestone"` | — |
| `"Remove issue from milestone…"` | `"remove-issue-from-milestone"` | — |
| `"Add sub-issue to epic…"` | `"add-sub-issue-to-epic"` | — |
| `"Add sub-issue via chat…"` | `"add-sub-issue-to-epic-chat"` | — |
| `"Close / archive plan"` | `"close-plan"` | — |
| `"Refresh"` | `"refresh"` | `r` |

**Required:** `driver.screen_contains("Open milestone chat")` is `true` after right-clicking an epic row.  
**Required:** `driver.screen_contains("Dispatch milestone")` is `true` after right-clicking an epic row.  
**Required:** `driver.screen_contains("Close / archive plan")` is `true` after right-clicking an epic row.

### 4e. Single-letter keys become accelerators inside the menu

After CC-3, single-letter keys (`c`, `C`, `d`, `u`, `r`) remain as accelerators shown **inside** the right-click menu (`with_shortcut()`), not as standalone bindings advertised in the status bar.

### 4f. Status bar update (CC-3)

**Before CC-3** (current, CC-1 state):  
`" j/k=nav  Enter=open epic  c=capture plan  u=toggle untracked  q=quit "`

**After CC-3** (required):  
**Required:** `driver.screen_contains("right-click=menu")` is `true`.  
**Required:** `driver.screen_contains("?=help")` is `true`.  
The cryptic `c=capture plan` and `u=toggle untracked` hints may be removed from the status bar (they remain as menu accelerators).

---

## 5. CC-4 (#1124) — `?` help overlay + command palette

### 5a. Help overlay — trigger

Pressing **`?`** while `active_view == SidebarView::Plans` opens a cheatsheet modal that overlays the main area. Pressing **Esc** closes it.

### 5b. Help overlay — required title

**Required:** `driver.screen_contains("Plans — Help")` is `true` when the help overlay is open.  
(The exact modal title string is `"Plans — Help"`.)

### 5c. Help overlay — required key entries

The cheatsheet must list these entries (each an exact substring that must appear):

| Required string | Meaning |
|---|---|
| `"right-click"` | menu hint |
| `"Enter"` | open detail pane |
| `"Esc"` | close / back |
| `"c"` | quick capture |
| `"C"` | guided chat |
| `"u"` | toggle untracked |
| `"r"` | refresh |
| `"q"` | quit |

**Testable:** for each string above, `driver.screen_contains(<string>)` is `true` while the overlay is open.

### 5d. Help overlay — health chip legend

The overlay includes a health-chip legend section. Required substrings:

| Required string | Chip meaning |
|---|---|
| `"ready_waiting"` | issues on the ready frontier |
| `"stalled"` | work order exists but nothing moving |
| `"chat_pending"` | milestone-chat steward open |
| `"no_work_order"` | no `## Work order` block |

**Testable:** `driver.screen_contains("ready_waiting")` is `true` while overlay is open.

### 5e. Command palette — trigger

Opening the command palette from the Plans panel (via the registered quadraui `Palette` key binding — `/` or `Ctrl+P`, implementation choice) shows a searchable list of Plans actions. Pressing **Esc** closes it.

### 5f. Command palette — required title

**Required:** `driver.screen_contains("command palette")` is `true` when the palette is open.

### 5g. Command palette — required Plans action entries

The Plans panel registers these entries with the quadraui `Palette`. Each entry's label is an exact string that must appear in the palette while it is open and unfiltered:

| Label | Bound action |
|---|---|
| `"Dispatch milestone"` | `dispatch-milestone` |
| `"Open milestone chat"` | `open-milestone-chat` |
| `"Quick capture plan"` | `capture-plan-quick` |
| `"Guided chat (new plan)"` | `capture-plan-chat` |
| `"View order / DAG"` | `view-milestone-order` |
| `"Edit milestone…"` | `edit-milestone` |
| `"Add issue to milestone…"` | `add-issue-to-milestone` |
| `"Toggle untracked milestones"` | toggle untracked (current `u` binding) |

**Required:** `driver.screen_contains("Dispatch milestone")` is `true` while palette is open.  
**Required:** `driver.screen_contains("Quick capture plan")` is `true` while palette is open.  
**Required:** `driver.screen_contains("Toggle untracked milestones")` is `true` while palette is open.

### 5h. Palette — search filtering

Typing a search string (e.g. `"dispatch"`) narrows the palette to matching entries.  
**Required:** after typing `"dispatch"`, `driver.screen_contains("Dispatch milestone")` is still `true` and entries not matching `"dispatch"` are absent from the palette display.

### 5i. Status bar when overlay/palette is open

**Required:** `driver.screen_contains("Esc=close")` is `true` when either the help overlay or command palette is open.

---

## 6. Test-support seam

All three children share the existing fixture seam for Plans tests (established by CC-1):

```rust
// in tui/src/app/fixtures.rs (feature "test-support")
make_test_app(data: BoardData) -> CoordApp
make_app_with_assignments(data: BoardData, ...) -> CoordApp
```

The `BoardData` must carry `plan_roster: Vec<PlanRosterEntry>` with at least:
- One entry with `tracking_issue: Some(1120)`, `has_work_order: true`, `needs_you: ["ready_waiting"]` (represents #38)
- One entry with `tracking_issue: None`, `has_work_order: false`, `needs_you: ["no_work_order"]` (stub — exercises CC-3 §4b)

Wire field reference for `PlanRosterEntry` (from `tui/src/app/types.rs`):

```json
{
  "repo":             "claude-coordinator",
  "title":            "Plans panel -> rich client",
  "milestone_number": 38,
  "tracking_issue":   1120,
  "has_work_order":   true,
  "ready_frontier":   2,
  "blocked":          1,
  "in_flight":        1,
  "done":             2,
  "total":            6,
  "needs_you":        ["ready_waiting"]
}
```

Optional outcome fields (`outcome_run_number`, `outcome_met`, `outcome_partial`, `outcome_gap`, `outcome_bottom_line`, `outcome_diff_summary`) are `null`/absent if not tested.

---

## 7. Mocks index

| File | Scenario | Issues covered |
|---|---|---|
| `mocks/plans-base.screen` | Plans panel active, all repos collapsed, unscoped list view | baseline / CC-1 reference |
| `mocks/plans-detail-pane.screen` | #38 selected + expanded in sidebar, detail pane open in main area | #1122 CC-2 |
| `mocks/plans-rightclick-stub.screen` | Right-click on epic-less stub row — "Create work order…" menu | #1123 CC-3 §4b |
| `mocks/plans-rightclick-header.screen` | Right-click on repo header — "New plan > Quick capture / Guided chat…" menu | #1123 CC-3 §4c |
| `mocks/plans-rightclick-epic.screen` | Right-click on epic row (#38, has tracking issue) — full CRUD menu | #1123 CC-3 §4d |
| `mocks/plans-help-overlay.screen` | `?` pressed — help cheatsheet modal with key/chip legend | #1124 CC-4 §5a–5d |
| `mocks/plans-palette.screen` | Command palette open — Plans actions listed | #1124 CC-4 §5e–5h |

All mocks: 120 × 40 terminal, `driver_with_shell(app, CoordApp::shell_config(), 120, 40)`.  
Tests are **in-crate** (`#[cfg(test)]` in `tui/src/app/tests.rs` or a nearby module) to access `make_test_app` and related `#[cfg(test)]`-only fixtures.

---

## 8. Notes / open questions

1. **CC-2 work-order wire format.** `PlanRosterEntry` carries per-plan aggregate counts (`done`, `total`, `in_flight`, `blocked`, `ready_frontier`) but not per-child issue rows (title, state, number). If the detail pane renders a checklist with individual issue rows, the implementor must decide whether to add per-child data to the `/board` payload and extend `PlanRosterEntry`, or derive a simplified checklist from the aggregate counts alone. If new fields are added, they must be mirrored in `types.rs` and the wire contract in §6 above must be amended before acceptance tests are authored for CC-2. The acceptance test assertions in §3c only require `"Work order"` and one status glyph — not per-issue strings — to allow both approaches.

2. **CC-3 "Create work order" action semantics.** The `"Create work order / promote to epic…"` menu item on a stub row is a new action with no existing CLI binding. The implementor must decide whether it opens a dialog (prompting for an epic title, then calling `coord milestone chat`/`capture` to bootstrap a tracking issue) or routes directly to `coord milestone chat --new`. This choice affects the action ID and the follow-on CC-3 acceptance slice. The contract pins only the menu label, not the action implementation.

3. **CC-3 status-bar hint set.** The exact post-CC-3 status bar string is: `" right-click=menu  ?=help  c=capture  q=quit "`. If the implementor adds additional hints, the contract's assertions remain satisfied as long as the required substrings appear.

4. **CC-4 palette key binding.** The trigger key for the command palette is left to the implementor (common choices: `/`, `Ctrl+P`, `Ctrl+Shift+P`). The contract requires only that the palette can be opened from Plans and that `driver.screen_contains("command palette")` is `true` while it is open. The test author will need to know the actual binding; if not yet decided at JIT time, this item must be resolved first. **Note:** `mocks/plans-palette.screen` shows `/=palette` in the status bar — this `/` is a **placeholder** for illustration purposes only, not a locked-in key choice. The actual binding is CC-3's decision; the test author must confirm the real key with the CC-3 implementor before authoring acceptance tests for §5e.

5. **Quadraui help layer (CC-4 dep).** `JDonaghy/quadraui#431` (help registry + `?` modal + Palette integration) is already merged to `develop` as of 2026-07-19. The coord-tui checkout's `~/src/quadraui` must be on the branch carrying it before `cargo build`. See CLAUDE.md §"coord-tui depends on quadraui by a relative path".

6. **Serialize CC-4, CC-2, CC-3.** All three children touch `tui/src/app/plans.rs`; CC-2 and CC-3 also touch `events.rs`. Run them one at a time. Order per tracking issue: **CC-4 first** (#1124), then CC-2 (#1122) or CC-3 (#1123) in operator-decided order.
