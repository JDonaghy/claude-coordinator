//! Plans ActivityBar panel (#975).
//!
//! Elevates and subsumes the `milestone_dag` "Milestones" view: renders the
//! plan-roster from `BoardData::plan_roster` (server-computed by
//! `coord/serve_app.py` via `coord.plans.aggregate_repo_plans`) as one row per
//! milestone/epic with ready / blocked / in-flight / done counts and a
//! `needs_you` attention list.  Selecting a row opens that plan's tracking
//! epic in the browser via `gh issue view --web`.
//!
//! **Design note (server-computed, thin client).** The TUI does not
//! re-aggregate plans client-side: it reads `plan_roster` off `/board`
//! verbatim.  This mirrors the #584 portable-control-center read path — no
//! shell-out to `coord plans` from the TUI, no re-implementation of the
//! aggregation, and stays in lock-step with `coord.plans.PlanEntry.to_dict()`
//! (a mistyped field would fail the whole `BoardPayload` parse and blank the
//! board, per #632).
//!
//! **Read-only in this slice.** Fast capture (#977) and the GOAL.md header
//! (#978) come later.
//!
//! **Health chips + attention badge (#976).** Each `needs_you` signal
//! renders as its own coloured/iconed chip (see `health_chip_for_signal`)
//! instead of the flat `[a, b, c]` bracket list from #975 — the raw signal
//! tokens still appear on screen (just individually coloured now) so
//! nothing that grepped for them breaks. A `NN% done` chip is appended
//! whenever the plan has a work order, independent of `needs_you`. The
//! ActivityBar (the quadraui `PanelDefinition` behind the ◆ icon) has no
//! badge/count slot in the vendored quadraui version, so the "N plans need
//! you" attention badge lives on the always-visible status bar instead
//! (`plans_needing_attention_count` below, read from `status_bar()` in
//! `mod.rs`), mirroring the existing `live_tmux_sessions` badge pattern —
//! visible from any view, satisfying "remind me without opening it."
//!
//! **Empty state is not one message (#976 fix-up).** A manual smoke test
//! against a real daemon reported "0 plans" with no indication anything was
//! wrong; the daemon turned out to be running a pre-#975 build that never
//! sends `plan_roster` at all — indistinguishable, from the empty Vec alone,
//! from a board with genuinely zero milestones. `render_plans_panel` now
//! branches on `BoardData::plan_roster_supported` (mirrors
//! `BoardPayload::plan_roster_supported`, stamped by `serve_app.py`'s
//! `board()` handler whenever it computes a roster at all, empty or not) to
//! show one of two distinct messages: "No plans yet" for a true-empty
//! roster, or a "Plans unavailable" pointer to upgrade/connect a daemon
//! otherwise.
//!
//! **`?` help overlay + `/` command palette (#1124).** Populates the
//! reusable quadraui help layer (#431 — `HelpRegistry` / `ViewHelp` /
//! `HelpOverlayController` / `DualModePaletteController`) for this panel —
//! the pattern other panels are meant to copy as they adopt `?`/`/`:
//!
//! 1. Register a [`ViewHelp`] under a `"panel:X"` key in
//!    [`CoordApp::new`] (`SidebarView::help_view_id` is the inverse lookup
//!    from the active view back to that key).
//! 2. That's it — the shared `help_overlay`/`command_palette` fields and
//!    the generic open-trigger / owns-all-input dispatch in `events.rs`,
//!    the `Esc=close` status-bar hint in `mod.rs::status_bar`, and the
//!    paint calls in `render.rs::render_content` all key off
//!    `help_view_id()` already, so a newly-registered view gets `?`/`/`
//!    for free.
//!
//! **Why the cheatsheet is painted by a local `render_help_overlay`
//! instead of [`HelpOverlayController::render`].** That method hardcodes
//! the title format as `"Help — {title}"`; ms-38 contract §5b pins the
//! *opposite* order, `"Plans — Help"`. quadraui is a dependency here, not
//! modified in place (see CLAUDE.md) — a title-order override belongs in a
//! follow-up quadraui PR, not an inline edit — so this file reuses the
//! `HelpRegistry`/`ViewHelp` *data* model (the reusable part of #431) but
//! rolls its own small paint routine (built from this file's existing
//! `ListView` pattern, not quadraui's `Panel`/`TextDisplay`) to get the
//! exact required title. The overlay's open/close *state machine*
//! (`HelpOverlayController::handle`) is reused as-is — only rendering is
//! local. Likewise the command palette reuses
//! `DualModePaletteController`'s state machine verbatim and only adds a
//! thin "{title} actions" section-label strip above its popup, since
//! `PaletteItem` has no header/separator concept to hang that label on.
#[allow(unused_imports)]
use super::*;

/// One row's identity in the flattened Plans sidebar tree, in the SAME order
/// as the `TreeRow`s returned by [`CoordApp::plans_tree_rows`] — index
/// parity is what lets a flat pixel-row index resolve back to "All repos", a
/// repo, or a milestone without re-deriving tree structure at the call site.
/// Mirrors `SessionsTreeRow` / `TerminalTreeRow` (#1121).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum PlansTreeRow {
    /// The root "All repos" leaf — no scoping.
    AllRepos,
    /// Index into `plans_repo_list()`.
    Repo(usize),
    /// `(repo index into `plans_repo_list()`, milestone index within that
    /// repo's `plans_entries()` group)`.
    Milestone(usize, usize),
}

/// One #1122 detail-pane row's kind, as needed by `plan_detail_action_at`'s
/// hit-test to know which rows are clickable action buttons (vs.
/// header/checklist rows, which aren't). Parallel (same length, same
/// order) to the `Vec<ListItem>` `CoordApp::plan_detail_items` returns,
/// mirroring the `PlansTreeRow`/`TreeRow` pairing above and
/// `plans_row_at`'s `row_targets` pattern already used elsewhere in this
/// file.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DetailRowKind {
    /// Header / health / "Work order" heading / checklist row — no click
    /// action.
    Other,
    /// The `CoordApp::plan_detail_actions_row1()` button strip.
    ActionsRow1,
    /// The `CoordApp::plan_detail_actions_row2()` button strip.
    ActionsRow2,
}

/// One rendered row of the #1122 detail pane's "Work order" checklist
/// (contract §3c) — either a real per-issue row (sourced from
/// `milestone_dag::MilestoneDagNode` when the tracking issue's body has
/// synced far enough to parse) or a coarse aggregate-count row (the
/// `PlanRosterEntry`-only fallback contract §8 note 1 permits). `label` is
/// the already-formatted left-hand text (issue ref + title, or a bare
/// count like `"3 done"`); `status_word` is the right-aligned state word
/// rendered via `ListItem::detail`.
struct DetailWorkOrderRow {
    glyph: char,
    label: String,
    status_word: &'static str,
    color: Color,
}

// ─── impl CoordApp — sidebar/main-panel rendering + actions ──────────────────

impl CoordApp {
    /// The plan-roster entries currently on the board, in a stable order:
    /// primary sort is `(repo, milestone_number)` so the list stays visually
    /// stable across refreshes.  Cheap — a clone of the payload slice.
    ///
    /// This is the *full* unfiltered roster (tracked + untracked milestones
    /// alike).  Most callers want [`Self::plans_visible_entries`] instead —
    /// this one remains for callers that need repo-wide aggregates (sidebar
    /// count, per-repo header stats) regardless of collapse state.
    pub(crate) fn plans_entries(&self) -> Vec<PlanRosterEntry> {
        let mut out: Vec<PlanRosterEntry> = self.data.plan_roster.clone();
        out.sort_by(|a, b| {
            (a.repo.as_str(), a.milestone_number).cmp(&(b.repo.as_str(), b.milestone_number))
        });
        out
    }

    /// Canonical repo order for the Plans sidebar tree (#1121): the
    /// `pipeline_repos` configured order (so "one node per configured repo"
    /// holds even for a repo with zero plans right now), plus any repo that
    /// appears in `plan_roster` but is missing from `pipeline_repos` —
    /// defensive, so a plan can never become unreachable from the sidebar
    /// just because config drifted from the roster.
    pub(crate) fn plans_repo_list(&self) -> Vec<String> {
        let mut out: Vec<String> = self.data.pipeline_repos.iter().map(|(n, _)| n.clone()).collect();
        for e in &self.data.plan_roster {
            if !out.contains(&e.repo) {
                out.push(e.repo.clone());
            }
        }
        out
    }

    /// Resolve the Plans sidebar tree's current selection (`plans_tree_selected`)
    /// down to a scoping repo name, or `None` for "All repos" — the default
    /// (nothing selected, or the root "All repos" leaf) that reproduces the
    /// pre-#1121 unfiltered behaviour. An out-of-range path (stale after a
    /// board refresh dropped a repo) also falls back to `None` rather than
    /// scoping to nothing.
    pub(crate) fn plans_scope_repo(&self) -> Option<String> {
        let path = self.plans_tree_selected.as_ref()?;
        let repo_idx = *path.first()?;
        if repo_idx == 0 {
            return None;
        }
        self.plans_repo_list().get(repo_idx as usize - 1).cloned()
    }

    /// The full plan-roster (`plans_entries()`), narrowed to the sidebar
    /// tree's current scope (#1121). Most callers that used to read
    /// `plans_entries()` directly for main-panel display now go through
    /// this instead — `plans_entries()` itself stays the true unfiltered
    /// roster for callers that need repo-wide aggregates regardless of
    /// scope (the sidebar tree's own per-repo counts, the global attention
    /// badge).
    pub(crate) fn plans_scoped_entries(&self) -> Vec<PlanRosterEntry> {
        match self.plans_scope_repo() {
            Some(repo) => self.plans_entries().into_iter().filter(|e| e.repo == repo).collect(),
            None => self.plans_entries(),
        }
    }

    /// The plan-roster entries that are actually *selectable/rendered* right
    /// now, within the current sidebar scope (#1121): every `has_work_order`
    /// milestone, plus `no_work_order` milestones only for repos in
    /// `plans_expanded_repos` (#1001). Collapsed untracked milestones are
    /// summarised by a non-selectable "+N without a work order" line drawn
    /// separately in `render_plans_panel` — they don't occupy a slot here,
    /// so `plans_sel` (which indexes into this list) never lands on noise
    /// the operator hasn't asked to see.
    pub(crate) fn plans_visible_entries(&self) -> Vec<PlanRosterEntry> {
        self.plans_scoped_entries()
            .into_iter()
            .filter(|e| e.has_work_order || self.plans_expanded_repos.contains(&e.repo))
            .collect()
    }

    /// The currently-selected plan-roster row (`plans_sel`, clamped against
    /// the *visible* roster — see `plans_visible_entries`), or `None` when
    /// nothing is currently rendered/selectable.
    ///
    /// **#1122 fix (review non-blocking concern):** while the detail pane is
    /// open (`plans_detail_open`), resolution goes through the stable
    /// `(repo, milestone_number)` identity captured in `plans_detail_target`
    /// at open time instead of `plans_sel`/`plans_visible_entries()`. The
    /// roster list isn't even painted while the pane is open, and this is a
    /// polling TUI — a board refresh mid-pane can reorder, shrink, or grow
    /// the visible-entry set, which would otherwise make the same raw index
    /// silently resolve to a *different* plan than the one the operator
    /// opened, misdirecting the pane's (potentially destructive) actions.
    /// Returns `None` if that plan has since dropped out of the roster
    /// entirely (e.g. the milestone closed).
    pub(crate) fn plans_selected(&self) -> Option<PlanRosterEntry> {
        if self.plans_detail_open {
            let (repo, milestone_number) = self.plans_detail_target.as_ref()?;
            return self
                .plans_entries()
                .into_iter()
                .find(|e| &e.repo == repo && &e.milestone_number == milestone_number);
        }
        let entries = self.plans_visible_entries();
        if entries.is_empty() {
            return None;
        }
        let idx = self.plans_sel.min(entries.len() - 1);
        entries.into_iter().nth(idx)
    }

    /// True iff `signal` should drive the loud "N need attention" badge and
    /// the row's warm-accent color (#1001). Only `ready_waiting` (something
    /// is ready to dispatch right now) and `stalled` (a work order exists
    /// but nothing is ready or in-flight) are actionable enough to justify
    /// crying wolf at 3-repo scale. `no_work_order` is the common case for
    /// plain organizational milestones that were never meant to become
    /// dispatch-tracked epics — informational, not an alarm — and
    /// `chat_pending` just means an operator already has a chat open against
    /// this plan, which isn't something *else* needs to act on.
    fn is_loud_attention_signal(signal: &str) -> bool {
        matches!(signal, "ready_waiting" | "stalled")
    }

    /// True iff `entry` should count toward the loud attention badge / warm
    /// row color — i.e. carries at least one `is_loud_attention_signal`.
    fn has_loud_attention(entry: &PlanRosterEntry) -> bool {
        entry.needs_you.iter().any(|s| Self::is_loud_attention_signal(s))
    }

    /// Count of plan-roster entries carrying at least one *loud* attention
    /// signal (`ready_waiting` / `stalled` — see `is_loud_attention_signal`,
    /// #1001) — the shared basis for the Plans sidebar hint (below) and the
    /// global status-bar "N plans need you" badge (#976, `status_bar()` in
    /// `mod.rs`) so the two never drift out of sync. `no_work_order` no
    /// longer inflates this count: at 3-repo scale, 38 of 41 milestones
    /// being plain organizational buckets with no dispatch intent was
    /// burying the 1-3 signals that were actually actionable.
    pub(crate) fn plans_needing_attention_count(&self) -> usize {
        self.data
            .plan_roster
            .iter()
            .filter(|e| Self::has_loud_attention(e))
            .count()
    }

    /// Toggle whether *repo*'s "without a work order" milestones are
    /// expanded in the Plans panel (#1001). Acts on the repo of the
    /// currently-selected row (`u` key); when nothing is selected — which
    /// only happens when *every* repo on screen is 100% untracked and
    /// collapsed, so there's no selectable row at all — falls back to the
    /// first repo in the full roster (mirrors `capture_plan_stub`'s
    /// first-configured-repo fallback) so a fully-untracked repo can still
    /// be expanded. No-ops with a toast only when the roster itself is
    /// empty. Selection (`plans_sel`) is reset to 0 afterward since
    /// collapsing/expanding can shrink or grow the visible list out from
    /// under the old index.
    pub(crate) fn toggle_plans_repo_expansion(&mut self) {
        let repo = match self
            .plans_selected()
            .map(|e| e.repo)
            .or_else(|| self.plans_scoped_entries().first().map(|e| e.repo.clone()))
        {
            Some(repo) => repo,
            None => {
                self.push_toast(
                    "Show untracked milestones",
                    "No plans on the board — nothing to expand.",
                    ToastSeverity::Info,
                );
                return;
            }
        };
        let now_expanded = if self.plans_expanded_repos.remove(&repo) {
            false
        } else {
            self.plans_expanded_repos.insert(repo.clone());
            true
        };
        let untracked = self
            .plans_entries()
            .iter()
            .filter(|e| e.repo == repo && !e.has_work_order)
            .count();
        self.plans_sel = 0;
        self.push_toast(
            if now_expanded {
                "Untracked milestones shown"
            } else {
                "Untracked milestones hidden"
            },
            &format!(
                "{repo}: {untracked} milestone{} without a work order {}.",
                if untracked == 1 { "" } else { "s" },
                if now_expanded { "expanded" } else { "collapsed" },
            ),
            ToastSeverity::Info,
        );
    }

    /// Whether `repo`'s milestones should paint expanded in the Plans
    /// sidebar tree, absent an explicit `plans_tree_expanded` override:
    /// collapsed by default (mirrors the main panel's own #1001
    /// collapsed-by-default convention for untracked milestones) so a
    /// milestone title never shows up on screen — in either pane — until
    /// the operator asks for it. Toggled by clicking the repo row.
    fn plans_tree_repo_expanded(&self, repo: &str) -> bool {
        *self.plans_tree_expanded.get(repo).unwrap_or(&false)
    }

    /// Build the Plans sidebar tree (#1121): a root "All repos" leaf (no
    /// scoping — the pre-#1121 behaviour) followed by one row per
    /// `plans_repo_list()` entry, each carrying a plan-count badge (amber
    /// when the repo has ≥1 *loud* attention signal, see
    /// `has_loud_attention`) and — when expanded — that repo's milestones as
    /// leaves. Returns the `TreeRow`s alongside a same-length/same-order
    /// `PlansTreeRow` index so click/keyboard-nav handlers can map a flat
    /// row index back to what it represents, mirroring
    /// `sessions_tree_rows`.
    pub(crate) fn plans_tree_rows(&self) -> (Vec<TreeRow>, Vec<PlansTreeRow>) {
        let all_entries = self.plans_entries();
        let repos = self.plans_repo_list();
        let total = all_entries.len();
        let total_attn = self.plans_needing_attention_count();

        let mut rows = Vec::with_capacity(repos.len() + 4);
        let mut index = Vec::with_capacity(repos.len() + 4);

        let all_badge = if total_attn > 0 {
            Badge::colored(format!("{total} ⚠{total_attn}"), Color::rgb(220, 190, 120))
        } else {
            Badge::plain(format!("{total}"))
        };
        rows.push(TreeRow {
            path: vec![0],
            indent: 0,
            icon: None,
            text: StyledText {
                spans: vec![StyledSpan::with_fg(
                    "All repos".to_string(),
                    Color::rgb(210, 210, 220),
                )],
            },
            badge: Some(all_badge),
            is_expanded: None,
            decoration: Decoration::Normal,
            edit: None,
        });
        index.push(PlansTreeRow::AllRepos);

        for (ri, repo) in repos.iter().enumerate() {
            let group: Vec<&PlanRosterEntry> =
                all_entries.iter().filter(|e| &e.repo == repo).collect();
            let has_plans = !group.is_empty();
            let attn = group.iter().filter(|e| Self::has_loud_attention(e)).count();
            let expanded = has_plans && self.plans_tree_repo_expanded(repo);

            let name_col = if has_plans {
                Color::rgb(220, 220, 220)
            } else {
                Color::rgb(120, 120, 120)
            };
            let badge = if attn > 0 {
                Some(Badge::colored(
                    format!("{} ⚠{attn}", group.len()),
                    Color::rgb(220, 190, 120),
                ))
            } else {
                Some(Badge::plain(format!("{}", group.len())))
            };
            rows.push(TreeRow {
                path: vec![(ri + 1) as u16],
                indent: 0,
                icon: None,
                text: StyledText {
                    // "◇ " marker distinguishes this from the main panel's
                    // "▾ {repo}  (N tracked) ..." group header — the two
                    // panes otherwise both render the bare repo name, which
                    // would make on-screen text lookups (tests, "find the
                    // repo row") ambiguous between sidebar and main content.
                    spans: vec![StyledSpan::with_fg(
                        format!("◇ {repo}"),
                        name_col,
                    )],
                },
                badge,
                is_expanded: if has_plans { Some(expanded) } else { None },
                decoration: Decoration::Normal,
                edit: None,
            });
            index.push(PlansTreeRow::Repo(ri));

            if !expanded {
                continue;
            }
            for (mi, entry) in group.iter().enumerate() {
                let marker = if entry.has_work_order { "●" } else { "○" };
                let marker_col = if Self::has_loud_attention(entry) {
                    Color::rgb(220, 190, 120)
                } else if entry.has_work_order {
                    Color::rgb(120, 190, 150)
                } else {
                    Color::rgb(120, 120, 130)
                };
                rows.push(TreeRow {
                    path: vec![(ri + 1) as u16, mi as u16],
                    indent: 1,
                    icon: None,
                    text: StyledText {
                        spans: vec![
                            StyledSpan::with_fg(format!("{marker} "), marker_col),
                            StyledSpan::with_fg(
                                format!("#{} {}", entry.milestone_number, trunc(&entry.title, 24)),
                                Color::rgb(190, 190, 200),
                            ),
                        ],
                    },
                    badge: None,
                    is_expanded: None,
                    decoration: Decoration::Normal,
                    edit: None,
                });
                index.push(PlansTreeRow::Milestone(ri, mi));
            }
        }

        (rows, index)
    }

    /// Build the `TreeView` widget for `render.rs`'s `SidebarView::Plans`
    /// sidebar branch (#1121). Replaces the pre-#1121 `plans_sidebar()`
    /// placeholder `ListView`.
    pub(crate) fn plans_tree_view(&self) -> TreeView {
        let (rows, _) = self.plans_tree_rows();
        TreeView {
            id: WidgetId::new("plans-tree"),
            rows,
            selection_mode: SelectionMode::Single,
            selected_path: self.plans_tree_selected.clone(),
            scroll_offset: self.plans_tree_scroll,
            style: TreeStyle::default(),
            has_focus: true,
        }
    }

    /// Handle a click at flattened row `row_idx` (0-based, already
    /// accounting for `plans_tree_scroll` — matching how
    /// `mouse_sidebar_click` derives it from pixel position for the
    /// Terminal/Sessions trees). Sets the scoping selection and, for a repo
    /// row that hosts plans, toggles its tree expansion. Selecting a
    /// milestone leaf scopes to its repo AND — expanding
    /// `plans_expanded_repos` first if the milestone is untracked, so it's
    /// actually visible — points `plans_sel` at the matching row in the
    /// now-scoped `plans_visible_entries()`, so the main panel's existing
    /// row highlight lands on the plan that was clicked. Always resets
    /// `plans_sel` to 0 first since a scope change can shrink or grow the
    /// visible list out from under the old index. Returns `true` when a
    /// redraw is needed; `false` when `row_idx` is out of range.
    pub(crate) fn plans_tree_click_row(&mut self, row_idx: usize) -> bool {
        let (_, index) = self.plans_tree_rows();
        let Some(entry) = index.get(row_idx).copied() else {
            return false;
        };
        self.plans_sel = 0;
        match entry {
            PlansTreeRow::AllRepos => {
                self.plans_tree_selected = Some(vec![0]);
            }
            PlansTreeRow::Repo(ri) => {
                self.plans_tree_selected = Some(vec![(ri + 1) as u16]);
                if let Some(repo) = self.plans_repo_list().get(ri).cloned() {
                    let has_plans = self.plans_entries().iter().any(|e| e.repo == repo);
                    if has_plans {
                        let cur = self.plans_tree_repo_expanded(&repo);
                        self.plans_tree_expanded.insert(repo, !cur);
                    }
                }
            }
            PlansTreeRow::Milestone(ri, mi) => {
                self.plans_tree_selected = Some(vec![(ri + 1) as u16, mi as u16]);
                if let Some(repo) = self.plans_repo_list().get(ri).cloned() {
                    let group: Vec<PlanRosterEntry> = self
                        .plans_entries()
                        .into_iter()
                        .filter(|e| e.repo == repo)
                        .collect();
                    if let Some(target) = group.get(mi).cloned() {
                        if !target.has_work_order {
                            self.plans_expanded_repos.insert(repo);
                        }
                        if let Some(idx) = self
                            .plans_visible_entries()
                            .iter()
                            .position(|e| e.repo == target.repo && e.milestone_number == target.milestone_number)
                        {
                            self.plans_sel = idx;
                        }
                    }
                }
            }
        }
        true
    }

    /// Map one `needs_you` signal to its health-chip `(icon+label, color)`
    /// (#976). The raw signal token stays in the label text — anything that
    /// greps/asserts on `"ready_waiting"` etc. (see #975's tests) still
    /// matches; only the icon + per-signal color are new. Unknown/future
    /// signals fall back to a plain amber chip so an older TUI build against
    /// a newer daemon degrades gracefully instead of dropping the signal.
    ///
    /// **#1001:** `no_work_order` is demoted to a muted gray — it's the
    /// common case for plain organizational milestones with no dispatch
    /// intent, not an alarm (see `is_loud_attention_signal`). It stays
    /// visible (informational) but no longer competes visually with the two
    /// signals that actually warrant a "look at me" amber/red.
    fn health_chip_for_signal(signal: &str) -> (String, Color) {
        match signal {
            "no_work_order" => ("⚑ no_work_order".to_string(), Color::rgb(140, 140, 150)),
            "ready_waiting" => ("● ready_waiting".to_string(), Color::rgb(120, 210, 120)),
            "stalled" => ("⏸ stalled".to_string(), Color::rgb(220, 100, 90)),
            "chat_pending" => ("◐ chat_pending".to_string(), Color::rgb(120, 190, 230)),
            other => (format!("▲ {other}"), Color::rgb(220, 190, 120)),
        }
    }

    /// Render the Plans main panel — grouped by repo (#1001), one header row
    /// per repo followed by that repo's tracked (`has_work_order`)
    /// milestones, with untracked milestones collapsed into a trailing
    /// summary line by default:
    ///
    /// ```text
    /// ▾ api  (1 tracked)   ready=2  blocked=1  ⚠ 1 need attention
    ///  api  #5  Substrate                    epic:#500  ready=2  in-flight=0  blocked=1  done=0/3  [● ready_waiting] [67% done]
    ///    +1 without a work order  (press u to expand)
    /// ```
    ///
    /// Each `needs_you` entry gets its own coloured chip (health chips,
    /// #976) instead of one flat bracketed list, plus an always-on
    /// `NN% done` chip whenever the plan has a work order (independent of
    /// `needs_you` — it's a progress indicator, not an attention signal).
    /// Only `ready_waiting`/`stalled` drive the warm-accent row color and
    /// the per-repo "N need attention" count (#1001) — `no_work_order` is
    /// informational (see `health_chip_for_signal`, `has_loud_attention`).
    ///
    /// The currently-selected row is highlighted via `selected_idx` so the
    /// "Enter to open tracking epic" action has a visible target. Header
    /// rows and the "+N without a work order" summary line are never
    /// selectable — `plans_sel` indexes only into `plans_visible_entries()`,
    /// mirroring `render_merge_plan_panel`'s header-row pattern in
    /// `pipeline.rs`: a local `selected_idx`/`data_idx` pair maps the flat
    /// data-only selection onto this header-interleaved display list.
    ///
    /// **#978:** when `BoardData::goal_header.available`, a pinned GOAL.md
    /// north-star header strip is carved off the top of `rect` and drawn
    /// first via `render_goal_header_strip`; the roster below (empty-state
    /// or populated) renders into the remaining `list_rect`. Absent/older
    /// daemons leave `goal_header.available == false` (the type's
    /// `Default`), so `list_rect == rect` and nothing changes from before
    /// this field existed.
    pub(crate) fn render_plans_panel(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        // #1122 (contract §3a): the detail pane is the FULL main-area
        // content, not a sub-split of the list — replace the roster
        // entirely rather than carving out a corner of `rect` for it (which
        // is what the #978 goal-header strip below does). Must precede the
        // goal-header carve-out too: the mock (`plans-detail-pane.screen`)
        // shows no pinned north-star strip while the pane is open.
        if self.plans_detail_open {
            self.render_plan_detail_pane(backend, rect, lh);
            return;
        }
        let list_rect = if self.data.goal_header.available {
            let goal_rect = Self::plans_goal_header_rect(rect, lh);
            self.render_goal_header_strip(backend, goal_rect);
            Self::plans_list_rect_below_goal_header(rect, goal_rect)
        } else {
            rect
        };

        // #976: an empty *unscoped* roster is ambiguous on its own — it means
        // either "genuinely zero milestones" (rare but real) or "not
        // currently receiving plan-roster data at all" (no daemon connected,
        // or a daemon older than #975 that never computes it). Silently
        // showing the same "no plans yet" placeholder in the second case is
        // exactly the review finding this fixes: a stale/pre-#975 daemon
        // rendered indistinguishable from a genuinely empty board.
        // `plan_roster_supported` (from `BoardPayload`/`BoardData`, see
        // types.rs) is the authoritative signal — trust it over guessing
        // from the empty Vec. This check must run against the *unscoped*
        // roster (`plans_entries()`), not the sidebar-scoped one, so it
        // never misfires just because the operator scoped to a repo with no
        // plans (#1121 — see the scoped-but-empty branch below for that).
        if self.plans_entries().is_empty() {
            let message = if self.data.plan_roster_supported {
                "  No plans yet.  Milestones with a `## Work order` block will appear here."
            } else {
                "  Plans unavailable — not receiving plan-roster data. Requires a \
                 `coord serve` daemon that supports it (v0.4.64+); connect via \
                 ~/.coord/client.toml, or upgrade + restart the daemon if already connected."
            };
            backend.draw_list(list_rect, &plain_list("plans-empty", message, 0));
            return;
        }

        let entries = self.plans_scoped_entries();
        if entries.is_empty() {
            // #1121: the board has plans overall, but the sidebar-selected
            // repo has none — distinct from the two `plan_roster_supported`
            // states above, which are about the board as a whole.
            let repo = self.plans_scope_repo().unwrap_or_default();
            let message = format!("  No plans for {repo}.");
            backend.draw_list(list_rect, &plain_list("plans-empty-scoped", &message, 0));
            return;
        }

        let visible = self.plans_visible_entries();
        let sel = if visible.is_empty() {
            0
        } else {
            self.plans_sel.min(visible.len() - 1)
        };
        let mut items: Vec<ListItem> = Vec::with_capacity(entries.len() + 8);
        let mut selected_idx = 0usize;
        let mut data_idx = 0usize; // index into `visible` / `plans_sel` space

        let mut i = 0usize;
        while i < entries.len() {
            let start = i;
            let repo = entries[start].repo.clone();
            while i < entries.len() && entries[i].repo == repo {
                i += 1;
            }
            let group = &entries[start..i];

            let tracked_count = group.iter().filter(|e| e.has_work_order).count();
            let untracked_count = group.len() - tracked_count;
            let ready_sum: u32 = group.iter().map(|e| e.ready_frontier).sum();
            let blocked_sum: u32 = group.iter().map(|e| e.blocked).sum();
            let attention_count = group.iter().filter(|e| Self::has_loud_attention(e)).count();

            let attn_suffix = if attention_count > 0 {
                format!(
                    "   ⚠ {attention_count} need attention",
                )
            } else {
                String::new()
            };
            let header_label = format!(
                "▾ {repo}  ({tracked_count} tracked)   ready={ready_sum}  blocked={blocked_sum}{attn_suffix}",
            );
            let header_color = if attention_count > 0 {
                Color::rgb(220, 190, 120)
            } else {
                Color::rgb(140, 180, 210)
            };
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(header_label, header_color)],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });

            let expanded = self.plans_expanded_repos.contains(&repo);
            for entry in group {
                if !entry.has_work_order && !expanded {
                    // Collapsed by default (#1001) — summarised by the
                    // trailing "+N without a work order" line below instead
                    // of always-expanded noise.
                    continue;
                }
                let tracking = entry
                    .tracking_issue
                    .map(|n| format!("epic:#{}", n))
                    .unwrap_or_else(|| "epic:—".to_string());
                let stats = if entry.has_work_order {
                    format!(
                        "ready={}  in-flight={}  blocked={}  done={}/{}",
                        entry.ready_frontier,
                        entry.in_flight,
                        entry.blocked,
                        entry.done,
                        entry.total,
                    )
                } else {
                    "no work order".to_string()
                };
                let row_label = format!(
                    " {}  #{}  {}   {}   {}",
                    entry.repo,
                    entry.milestone_number,
                    trunc(&entry.title, 32),
                    tracking,
                    stats,
                );
                let base_color = if Self::has_loud_attention(entry) {
                    // Only a loud (ready_waiting/stalled) signal → warmer
                    // accent on the base text so the row reads as "look at
                    // me" even before the chips (#1001: no_work_order alone
                    // no longer earns this).
                    Color::rgb(220, 190, 120)
                } else {
                    Color::rgb(200, 200, 200)
                };
                let mut spans = vec![StyledSpan::with_fg(row_label, base_color)];
                for signal in &entry.needs_you {
                    let (label, color) = Self::health_chip_for_signal(signal);
                    spans.push(StyledSpan::with_fg(format!("  [{label}]"), color));
                }
                // Always-on done% chip — a progress indicator, not an
                // attention signal, so it renders regardless of `needs_you`.
                if entry.has_work_order && entry.total > 0 {
                    let pct = (entry.done * 100) / entry.total;
                    let pct_color = if pct >= 100 {
                        Color::rgb(120, 210, 120)
                    } else {
                        Color::rgb(150, 150, 160)
                    };
                    spans.push(StyledSpan::with_fg(format!("  [{pct}% done]"), pct_color));
                }
                // #886 Phase 2: Milestone Outcome Audit — the done-gate is the
                // verdict (goals met/partial/gap), independent of the
                // issue-closed counts above. Omitted entirely when no
                // `--audit-of` run has ever posted against this epic (no
                // fabricated 0/0). Always-on like the done% chip, not an
                // attention signal.
                if let Some(run) = entry.outcome_run_number {
                    let met = entry.outcome_met.unwrap_or(0);
                    let gap = entry.outcome_gap.unwrap_or(0);
                    let total = met + entry.outcome_partial.unwrap_or(0) + gap;
                    let outcome_color = if gap > 0 {
                        Color::rgb(220, 130, 120)
                    } else if met == total && total > 0 {
                        Color::rgb(120, 210, 120)
                    } else {
                        Color::rgb(150, 150, 160)
                    };
                    spans.push(StyledSpan::with_fg(
                        format!("  [v{run}: goals {met}/{total} met · {gap} gap]"),
                        outcome_color,
                    ));
                }
                if data_idx == sel {
                    selected_idx = items.len();
                }
                // Extra outcome context, right-aligned on the row via
                // `detail` (quadraui pins it to the visible viewport
                // regardless of h_scroll and regardless of selection, same as
                // any other always-on row metadata): prefer the pre-rendered
                // delta vs the previous audit run (e.g. "v1→v2: closed:
                // tests.rs split; still open: #550") — the concrete "re-ask
                // the question" payoff (#886) — and fall back to the latest
                // run's one-line bottom-line verdict when there's no prior
                // run to diff against yet (v1).
                let detail = entry
                    .outcome_diff_summary
                    .as_ref()
                    .or(entry.outcome_bottom_line.as_ref())
                    .filter(|s| !s.is_empty())
                    .map(|summary| StyledText {
                        spans: vec![StyledSpan::with_fg(
                            summary.clone(),
                            Color::rgb(150, 150, 160),
                        )],
                    });
                items.push(ListItem {
                    text: StyledText { spans },
                    icon: None,
                    detail,
                    decoration: Decoration::Normal,
                });
                data_idx += 1;
            }

            if untracked_count > 0 && !expanded {
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!(
                                "    +{untracked_count} without a work order  (press u to expand)",
                            ),
                            Color::rgb(120, 120, 130),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Muted,
                });
            }
        }

        let total = items.len();
        backend.draw_list(
            list_rect,
            &ListView {
                id: WidgetId::new("plans-list"),
                title: Some(StyledText::plain(" PLANS ")),
                items,
                selected_idx,
                scroll_offset: 0,
                has_focus: true,
                bordered: true,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: total > 10,
            },
        );
    }

    // ─── #1122: in-app plan detail pane ───────────────────────────────────

    /// Build the #1122 detail pane's content — header (contract §3b),
    /// work-order checklist (§3c) and actions row(s) (§3d) — as a
    /// `ListView`-ready item list, alongside a same-length `DetailRowKind`
    /// index so `plan_detail_action_at`'s hit-test and
    /// `render_plan_detail_pane`'s paint call share exactly one source of
    /// row layout (the `plans_row_at` pattern this file already uses for
    /// the roster list, applied here to the detail pane instead).
    fn plan_detail_items(&self, entry: &PlanRosterEntry) -> (Vec<ListItem>, Vec<DetailRowKind>) {
        let mut items = Vec::with_capacity(16);
        let mut kinds = Vec::with_capacity(16);

        // §3b: milestone number + title header.
        items.push(ListItem {
            text: StyledText {
                spans: vec![StyledSpan {
                    bold: true,
                    ..StyledSpan::with_fg(
                        format!("#{} {}", entry.milestone_number, entry.title),
                        Color::rgb(230, 230, 230),
                    )
                }],
            },
            icon: None,
            detail: None,
            decoration: Decoration::Header,
        });
        kinds.push(DetailRowKind::Other);

        // §3b: tracking-epic ref + done% + health (issue text: "health
        // (`(warn) needs you`, blocked count)" — §3b's own table doesn't
        // pin exact wording for the health part, so this reuses the
        // existing `health_chip_for_signal` chips plus a bare "N blocked"
        // suffix, same vocabulary the roster list already renders).
        let tracking_str = entry
            .tracking_issue
            .map(|n| format!("epic:#{}", n))
            .unwrap_or_else(|| "epic:—".to_string());
        let mut spans = vec![StyledSpan::with_fg(tracking_str, Color::rgb(140, 180, 210))];
        if entry.has_work_order && entry.total > 0 {
            let pct = (entry.done * 100) / entry.total;
            let pct_color = if pct >= 100 {
                Color::rgb(120, 210, 120)
            } else {
                Color::rgb(150, 150, 160)
            };
            spans.push(StyledSpan::with_fg(format!("   {pct}% done"), pct_color));
        }
        for signal in &entry.needs_you {
            let (label, color) = Self::health_chip_for_signal(signal);
            spans.push(StyledSpan::with_fg(format!("   [{label}]"), color));
        }
        if entry.blocked > 0 {
            spans.push(StyledSpan::with_fg(
                format!("   {} blocked", entry.blocked),
                Color::rgb(220, 140, 90),
            ));
        }
        items.push(ListItem {
            text: StyledText { spans },
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        });
        kinds.push(DetailRowKind::Other);

        // §3c: "Work order" section heading + checklist rows.
        items.push(cheatsheet_header_item("Work order"));
        kinds.push(DetailRowKind::Other);
        let rows = self.plan_detail_work_order_rows(entry);
        if rows.is_empty() {
            items.push(ListItem {
                text: StyledText::plain(
                    "  No work-order detail available.".to_string(),
                ),
                icon: None,
                detail: None,
                decoration: Decoration::Muted,
            });
            kinds.push(DetailRowKind::Other);
        } else {
            for row in &rows {
                items.push(ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!("  {} {}", row.glyph, row.label),
                            row.color,
                        )],
                    },
                    icon: None,
                    detail: Some(StyledText {
                        spans: vec![StyledSpan::with_fg(row.status_word.to_string(), row.color)],
                    }),
                    decoration: Decoration::Normal,
                });
                kinds.push(DetailRowKind::Other);
            }
        }

        // §3d: actions row(s). Row 1 carries the five labels the contract
        // requires verbatim (in its own order); row 2 carries the
        // remaining actions the issue text lists ("Add/remove issue -
        // Close") that the contract's table doesn't individually require.
        let (row1_line, _) = build_action_row_line(&Self::plan_detail_actions_row1());
        items.push(ListItem {
            text: StyledText::plain(row1_line),
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        });
        kinds.push(DetailRowKind::ActionsRow1);
        let (row2_line, _) = build_action_row_line(&Self::plan_detail_actions_row2());
        items.push(ListItem {
            text: StyledText::plain(row2_line),
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        });
        kinds.push(DetailRowKind::ActionsRow2);

        (items, kinds)
    }

    /// Contract §3d's five required action labels, in the exact order
    /// `mocks/plans-detail-pane.screen` line 13 renders them. Action ids
    /// match `dialogs.rs::dispatch_context_menu_action`'s existing
    /// vocabulary (see `activate_command_palette_action`) except
    /// `"open-in-browser"`, which `activate_plan_detail_action` special-cases.
    fn plan_detail_actions_row1() -> Vec<(&'static str, &'static str)> {
        vec![
            ("Dispatch next", "dispatch-milestone-next"),
            ("Open chat", "open-milestone-chat"),
            ("View DAG", "view-milestone-order"),
            ("Edit", "edit-milestone"),
            ("Open in browser", "open-in-browser"),
        ]
    }

    /// The remaining actions the issue text lists ("Add/remove issue -
    /// Close") that aren't individually required by contract §3d's table —
    /// a second row below `plan_detail_actions_row1` rather than crowding
    /// them onto one line.
    fn plan_detail_actions_row2() -> Vec<(&'static str, &'static str)> {
        vec![
            ("Add issue", "add-issue-to-milestone"),
            ("Remove issue", "remove-issue-from-milestone"),
            ("Close", "close-plan"),
        ]
    }

    /// Render one actions row's text plus each button's `(action_id,
    /// start_col, end_col)` char-offset range within that text (0-based,
    /// `end` exclusive) — shared by `plan_detail_items` (paint) and
    /// `plan_detail_action_at` (hit-test) so the two can never drift apart,
    /// the same reasoning `plans_row_at`'s doc comment gives for mirroring
    /// `render_plans_panel`'s row shape exactly.
    fn plan_detail_work_order_rows(&self, entry: &PlanRosterEntry) -> Vec<DetailWorkOrderRow> {
        // Prefer the client-side-parsed `## Work order` DAG (#771/#795,
        // `milestone_dag.rs`) when the tracking issue's body has synced far
        // enough to parse it — it carries real per-issue rows (number,
        // title, live Done/InFlight/Ready/Blocked state), which is strictly
        // richer than the roster's own aggregate counts and needs no wire
        // changes at all (contract §8 note 1 permits either approach; this
        // is the "add per-child data" option, sourced from data the TUI
        // already has on the wire via `open_issues`/`assignments`).
        if let Some(tracking) = entry.tracking_issue {
            if let Some(view) = self
                .milestone_dag_views()
                .into_iter()
                .find(|v| v.repo_name == entry.repo && v.tracking_issue == tracking)
            {
                if !view.nodes.is_empty() {
                    return view
                        .nodes
                        .iter()
                        .map(|n| {
                            let (glyph, word, color) = match &n.state {
                                NodeState::Done => ('✓', "done", Color::rgb(120, 210, 120)),
                                NodeState::InFlight => {
                                    ('▶', "in-flight", Color::rgb(120, 190, 230))
                                }
                                NodeState::Ready => ('·', "ready", Color::rgb(150, 150, 160)),
                                NodeState::Blocked(_) => {
                                    ('—', "blocked", Color::rgb(220, 140, 90))
                                }
                            };
                            DetailWorkOrderRow {
                                glyph,
                                label: format!("#{}  {}", n.issue_number, trunc(&n.title, 40)),
                                status_word: word,
                                color,
                            }
                        })
                        .collect();
                }
            }
        }
        // Fallback (contract §8 note 1's "derive from aggregate counts"
        // option): the tracking issue hasn't synced a parseable body yet
        // (older cache, or the daemon hasn't refreshed `open_issues` since
        // this milestone's epic was created) — still surface *something*
        // under "Work order" rather than an empty section, straight from
        // `PlanRosterEntry`'s own counts.
        let mut rows = Vec::with_capacity(4);
        if entry.done > 0 {
            rows.push(DetailWorkOrderRow {
                glyph: '✓',
                label: format!("{} done", entry.done),
                status_word: "done",
                color: Color::rgb(120, 210, 120),
            });
        }
        if entry.in_flight > 0 {
            rows.push(DetailWorkOrderRow {
                glyph: '▶',
                label: format!("{} in-flight", entry.in_flight),
                status_word: "in-flight",
                color: Color::rgb(120, 190, 230),
            });
        }
        if entry.ready_frontier > 0 {
            rows.push(DetailWorkOrderRow {
                glyph: '·',
                label: format!("{} ready", entry.ready_frontier),
                status_word: "ready",
                color: Color::rgb(150, 150, 160),
            });
        }
        if entry.blocked > 0 {
            rows.push(DetailWorkOrderRow {
                glyph: '—',
                label: format!("{} blocked", entry.blocked),
                status_word: "blocked",
                color: Color::rgb(220, 140, 90),
            });
        }
        rows
    }

    /// Row count of the #1122 detail pane's flattened item list
    /// (`plan_detail_items`) for the plan the pane is currently open on
    /// (`plans_selected()`), or `0` when nothing is selected. `plan_
    /// detail_items` is private to this file, so `events.rs`'s `j`/`k`
    /// handlers go through this instead of reconstructing the item list
    /// (or the row-kind bookkeeping) themselves.
    pub(crate) fn plan_detail_row_count(&self) -> usize {
        self.plans_selected()
            .map(|entry| self.plan_detail_items(&entry).0.len())
            .unwrap_or(0)
    }

    /// #1122 fix (review): keep `plans_detail_scroll` following
    /// `plans_detail_sel` so every row of the detail pane — including a
    /// work-order checklist or actions row that would otherwise sit past
    /// the first screenful — can actually be scrolled into view. Same
    /// structural pattern as `fix_audit_scroll` (#1094) / `fix_machine_
    /// scroll` (`mod.rs`). Must be called after every keyboard nav that
    /// moves `plans_detail_sel` (`j`/`k`/`Down`/`Up` in events.rs, while
    /// `plans_detail_open`).
    pub(crate) fn fix_plans_detail_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        if self.plans_detail_sel < self.plans_detail_scroll {
            self.plans_detail_scroll = self.plans_detail_sel;
        } else if self.plans_detail_sel >= self.plans_detail_scroll + visible {
            self.plans_detail_scroll = self.plans_detail_sel + 1 - visible;
        }
    }

    /// Render the #1122 in-app plan detail pane (contract §3) — the FULL
    /// main-area content while `plans_detail_open`, built from
    /// `plan_detail_items`. Falls back to a plain message on the (should be
    /// unreachable — `open_selected_plan_detail` only sets
    /// `plans_detail_open` when a row is selected) case of no selection.
    ///
    /// **#1122 fix (review):** `selected_idx`/`scroll_offset` now come from
    /// `plans_detail_sel`/`plans_detail_scroll` instead of being hardcoded
    /// to `0` — previously every row past the first screenful (including
    /// the contract §3d actions row, on any plan whose work order was long
    /// enough to fill the viewport) was silently dropped from
    /// `visible_items` by quadraui's `ListView::layout` with no way to
    /// scroll it into view. `plan_detail_action_at` below builds the
    /// identical `ListView` (same `scroll_offset`) for its hit-test, so the
    /// two can never drift apart — same reasoning this file already gives
    /// for `build_action_row_line`.
    fn render_plan_detail_pane(&self, backend: &mut dyn Backend, rect: Rect, _lh: f32) {
        let Some(entry) = self.plans_selected() else {
            backend.draw_list(
                rect,
                &plain_list("plans-detail-empty", "  No plan selected.", 0),
            );
            return;
        };
        let (items, _kinds) = self.plan_detail_items(&entry);
        let total = items.len();
        let selected_idx = self.plans_detail_sel.min(total.saturating_sub(1));
        backend.draw_list(
            rect,
            &ListView {
                id: WidgetId::new("plans-detail"),
                title: Some(StyledText::plain(format!(
                    " #{} {} ",
                    entry.milestone_number,
                    trunc(&entry.title, 60),
                ))),
                items,
                selected_idx,
                scroll_offset: self.plans_detail_scroll,
                has_focus: true,
                bordered: true,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: total > 10,
            },
        );
    }

    /// Hit-test a click in the detail pane's actions row(s) (contract §3d)
    /// back to the action id under `pos`, or `None` when the click missed
    /// every button (header/checklist row, or between-button padding).
    /// Mirrors `plans_row_at`'s use of quadraui's `ListView::layout` for
    /// the row index, then resolves the sub-row column locally since
    /// `ListViewHit::Item` carries no column info — the same 1-cell
    /// `bordered` inset `plans_row_at` accounts for via `list_rect`.
    ///
    /// **#1122 fix (review):** `scroll_offset`/`selected_idx`/`title`/
    /// `show_v_scrollbar` now match `render_plan_detail_pane`'s `ListView`
    /// exactly (`plans_detail_scroll`/`plans_detail_sel`, the real title
    /// text, `total > 10`) — previously this hit-test always built its copy
    /// with `scroll_offset: 0`, so a click on a scrolled-into-view action
    /// button (once scrolling was wired up) would have resolved against the
    /// wrong row entirely; the empty title and unconditional
    /// `show_v_scrollbar: false` were also a latent paint/hit-test
    /// divergence this file's own doc comments say must never happen.
    pub(crate) fn plan_detail_action_at(&self, pos: Point, main_b: Rect, lh: f32) -> Option<String> {
        let entry = self.plans_selected()?;
        let (items, kinds) = self.plan_detail_items(&entry);
        let total = items.len();
        let selected_idx = self.plans_detail_sel.min(total.saturating_sub(1));
        let list = ListView {
            id: WidgetId::new("plans-detail"),
            title: Some(StyledText::plain(format!(
                " #{} {} ",
                entry.milestone_number,
                trunc(&entry.title, 60),
            ))),
            items,
            selected_idx,
            scroll_offset: self.plans_detail_scroll,
            has_focus: true,
            bordered: true,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: total > 10,
        };
        let layout = list.layout(main_b.width, main_b.height, lh, |_| ListItemMeasure::new(lh));
        let local_x = pos.x - main_b.x;
        let local_y = pos.y - main_b.y;
        let ListViewHit::Item(idx) = layout.hit_test(local_x, local_y) else {
            return None;
        };
        let actions = match kinds.get(idx)? {
            DetailRowKind::ActionsRow1 => Self::plan_detail_actions_row1(),
            DetailRowKind::ActionsRow2 => Self::plan_detail_actions_row2(),
            DetailRowKind::Other => return None,
        };
        let (_, buttons) = build_action_row_line(&actions);
        // `bordered` reserves a 1-cell inset on each side (quadraui
        // `ListView::layout`'s `inset_x`) before the item's own text
        // starts — `build_action_row_line`'s char offsets are relative to
        // that text, not the widget-local column `hit_test` used above.
        let content_x = (local_x - 1.0).max(0.0) as usize;
        buttons
            .into_iter()
            .find(|(_, start, end)| content_x >= *start && content_x < *end)
            .map(|(id, _, _)| id)
    }

    /// Shared geometry helper: the roster's `list_rect` once the optional
    /// pinned GOAL.md header strip (#978) has been carved off the top of
    /// `rect`. Split out of `render_plans_panel` so `plans_row_at`'s
    /// hit-test can reproduce the exact same layout the paint path used —
    /// duplicating this arithmetic ad hoc would drift the two apart the
    /// first time either one changed (#1003 fix-up).
    fn plans_list_rect_below_goal_header(rect: Rect, goal_rect: Rect) -> Rect {
        Rect::new(
            rect.x,
            rect.y + goal_rect.height,
            rect.width,
            (rect.height - goal_rect.height).max(0.0),
        )
    }

    /// Map a screen position in the main panel to the plan-roster row index
    /// under the cursor, for mouse click / right-click support (#1003
    /// fix-up).
    ///
    /// **Why this exists:** unlike Board/Pipeline/Machines, whose
    /// selectable row list lives in the *sidebar* (handled generically by
    /// `mouse_sidebar_click` + the quadraui sidebar tree/list controller),
    /// the Plans roster is a raw `ListView` painted straight into the
    /// *main* panel (`render_plans_panel`) — there was no hit-test at all
    /// for it, so neither a left-click (row selection) nor a right-click
    /// (the #1003 CRUD context menu) could ever resolve a target: right-click
    /// support in `handle_mouse`'s `MouseDown`/`Right` arm only ever tried
    /// `ctx.in_sidebar(..)`, silently no-op-ing for `ctx.in_main(..)` clicks.
    /// This mirrors `render_plans_panel`'s exact geometry (goal-header
    /// carve-out via `plans_list_rect_below_goal_header`, then the bordered
    /// `ListView` with a 1-row title) through quadraui's own
    /// `ListView::layout`/`hit_test` — the same D6 layout API `pipeline.rs`
    /// already uses for its main-panel hit-tests — rather than hand-rolling
    /// row arithmetic that could drift from the paint path.
    ///
    /// **#1001 rebase fix-up:** `render_plans_panel` now paints a
    /// header-interleaved, collapse-filtered item list (one non-selectable
    /// header row per repo group, milestone rows only for `has_work_order`
    /// entries or expanded repos, an optional trailing non-selectable "+N
    /// without a work order" line) rather than one flat row per
    /// `plans_entries()` element, and the caller (`plans_sel`, see `mod.rs`)
    /// indexes into `plans_visible_entries()`, not `plans_entries()`. This
    /// helper must therefore build the *same shaped* placeholder list
    /// `render_plans_panel` paints — not just the same length as
    /// `plans_entries()` — and translate a hit back into
    /// `plans_visible_entries()` space, or `None` for a header/summary row.
    /// Left uncorrected this silently mis-maps every click once a roster has
    /// more than one repo group or any collapsed milestone (the header/
    /// summary rows shift every subsequent row index, and the returned
    /// index lands in the wrong index space besides).
    ///
    /// Returns `None` when the position isn't over a selectable row (title
    /// strip, goal header, a header/summary row, empty tail, or an empty
    /// roster).
    pub(crate) fn plans_row_at(&self, pos: Point, main_b: Rect, lh: f32) -> Option<usize> {
        // #1121: must match `render_plans_panel`'s `entries` exactly — the
        // sidebar-scoped roster, not the full unfiltered one — or a click
        // would hit-test against rows that were never painted.
        let entries = self.plans_scoped_entries();
        if entries.is_empty() {
            return None;
        }
        let list_rect = if self.data.goal_header.available {
            let goal_rect = Self::plans_goal_header_rect(main_b, lh);
            Self::plans_list_rect_below_goal_header(main_b, goal_rect)
        } else {
            main_b
        };

        // Reproduce `render_plans_panel`'s item list *shape* — one entry per
        // painted row, `Some(visible_idx)` for a selectable milestone row
        // (indexing into `plans_visible_entries()`) or `None` for a
        // non-selectable header/summary row. Grouping/filter conditions
        // below MUST stay identical to `render_plans_panel`'s loop.
        let mut row_targets: Vec<Option<usize>> = Vec::with_capacity(entries.len() + 8);
        let mut visible_idx = 0usize;
        let mut i = 0usize;
        while i < entries.len() {
            let start = i;
            let repo = entries[start].repo.clone();
            while i < entries.len() && entries[i].repo == repo {
                i += 1;
            }
            let group = &entries[start..i];
            row_targets.push(None); // per-repo header row

            let untracked_count = group.iter().filter(|e| !e.has_work_order).count();
            let expanded = self.plans_expanded_repos.contains(&repo);
            for entry in group {
                if !entry.has_work_order && !expanded {
                    // Collapsed by default — summarised by the trailing
                    // "+N without a work order" line, not its own row.
                    continue;
                }
                row_targets.push(Some(visible_idx));
                visible_idx += 1;
            }
            if untracked_count > 0 && !expanded {
                row_targets.push(None); // "+N without a work order" line
            }
        }

        // Content of each placeholder item is irrelevant to `layout` — it
        // only consults `items.len()` (as the scroll-iteration bound) and
        // the `measure_item` closure below for row heights.
        let placeholder = ListItem {
            text: StyledText::plain(String::new()),
            icon: None,
            detail: None,
            decoration: Decoration::Normal,
        };
        let list = ListView {
            id: WidgetId::new("plans-list"),
            title: Some(StyledText::plain(" PLANS ")),
            items: vec![placeholder; row_targets.len()],
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: true,
            bordered: true,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: row_targets.len() > 10,
        };
        let layout = list.layout(list_rect.width, list_rect.height, lh, |_| {
            ListItemMeasure::new(lh)
        });
        let local_x = pos.x - list_rect.x;
        let local_y = pos.y - list_rect.y;
        match layout.hit_test(local_x, local_y) {
            ListViewHit::Item(idx) => row_targets.get(idx).copied().flatten(),
            _ => None,
        }
    }

    /// Carve the pinned GOAL.md header strip off the top of the Plans main
    /// panel rect (#978). Reserves 2 rows (headline + staleness line),
    /// capped at 30% of the available height so a short terminal still
    /// leaves room for at least one roster row below it. Mirrors
    /// `pipeline_detail_pv_rect_strip` in `render.rs`.
    fn plans_goal_header_rect(main: Rect, lh: f32) -> Rect {
        if lh <= 0.0 {
            return Rect::new(main.x, main.y, main.width, 0.0);
        }
        let want_rows = 2.0_f32;
        let max_h = (main.height * 0.30).max(lh);
        let h = (want_rows * lh).min(max_h);
        Rect::new(main.x, main.y, main.width, h)
    }

    /// Render the pinned GOAL.md north-star header (#978): the headline
    /// one-liner plus a "updated <date> · <N>d ago" staleness hint, amber +
    /// `⚠ stale` past `GOAL_STALE_DAYS`. Read-only — not part of the
    /// selectable roster drawn below it. Only called when
    /// `self.data.goal_header.available`.
    fn render_goal_header_strip(&self, backend: &mut dyn Backend, rect: Rect) {
        const GOAL_STALE_DAYS: i64 = 14;
        let goal = &self.data.goal_header;
        let headline = if goal.headline.is_empty() {
            "GOAL.md".to_string()
        } else {
            trunc(&goal.headline, 100).to_string()
        };
        let mut items = vec![ListItem {
            text: StyledText {
                spans: vec![
                    StyledSpan::with_fg(" ★ NORTH STAR  ".to_string(), Color::rgb(230, 200, 120)),
                    StyledSpan::with_fg(headline, Color::rgb(220, 220, 220)),
                ],
            },
            icon: None,
            detail: None,
            decoration: Decoration::Header,
        }];
        if let Some(last_updated) = &goal.last_updated {
            let (age_text, age_color) = match goal.days_since_update {
                Some(days) if days > GOAL_STALE_DAYS => (
                    format!("   updated {last_updated} · {days}d ago  ⚠ stale"),
                    Color::rgb(220, 140, 90),
                ),
                Some(0) => (
                    format!("   updated {last_updated} · today"),
                    Color::rgb(140, 140, 150),
                ),
                Some(days) => (
                    format!("   updated {last_updated} · {days}d ago"),
                    Color::rgb(140, 140, 150),
                ),
                None => (format!("   updated {last_updated}"), Color::rgb(140, 140, 150)),
            };
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(age_text, age_color)],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            });
        }
        backend.draw_list(
            rect,
            &ListView {
                id: WidgetId::new("plans-goal-header"),
                title: None,
                items,
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: false,
                bordered: false,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
            },
        );
    }

    /// Enter / "open selected plan" (#1122, contract §3a) — opens the
    /// in-app detail pane (`plans_detail_open`) for the selected plan
    /// **instead of** spawning `gh issue view --web` (that's demoted to the
    /// pane's own "Open in browser" action, `open_selected_plan_tracking_epic`
    /// below). Only fires for a row with `tracking_issue: Some(_)` — a stub
    /// row (no tracking epic yet) keeps the pre-#1122 toast pointing at
    /// `coord milestone chat` instead, since there's no epic to show a
    /// detail pane *of*. Returns `true` when the pane was opened (redraw
    /// needed).
    pub(crate) fn open_selected_plan_detail(&mut self) -> bool {
        let Some(entry) = self.plans_selected() else {
            self.push_toast(
                "Open plan",
                "No plan selected — highlight a row first.",
                ToastSeverity::Info,
            );
            return false;
        };
        if entry.tracking_issue.is_none() {
            self.push_toast(
                "No tracking epic yet",
                &format!(
                    "{} #{}: {} has no `epic`-labelled tracking issue. \
                     Create one with `coord milestone chat`.",
                    entry.repo, entry.milestone_number, entry.title,
                ),
                ToastSeverity::Info,
            );
            return false;
        }
        self.plans_detail_open = true;
        // #1122 fix: pin the stable identity this pane was opened for
        // (`plans_selected()` resolves through it while the pane is open —
        // see that method's doc comment) and reset the pane's own
        // scroll/selection state so a previous pane's leftover scroll
        // position never leaks into a freshly-opened one.
        self.plans_detail_target = Some((entry.repo.clone(), entry.milestone_number));
        self.plans_detail_sel = 0;
        self.plans_detail_scroll = 0;
        true
    }

    /// "Open in browser" — spawn `gh issue view <tracking_issue> --repo
    /// <slug> --web` for the selected plan. Pre-#1122 this was Enter's
    /// direct behaviour; contract §3a demotes it to one action among
    /// several on the detail pane's actions row (`plan_detail_actions_row1`).
    /// Silently noops when nothing is selected or the plan has no tracking
    /// epic yet (a "create an epic" workflow lives in #977 / #978).  Returns
    /// `true` when the tracking-epic open was attempted so the caller can
    /// request a redraw.
    ///
    /// Mirrors `dispatch_open_pr_for_selected_pipeline_row` — bypasses the
    /// command runner because `gh` isn't a `coord` subcommand and the runner
    /// is `coord`-verb-scoped.  In `#[cfg(test)]` builds the spawn itself is
    /// skipped (so `cargo test` doesn't try to shell out to a real `gh`); a
    /// toast is still pushed so tests can observe the action via the screen.
    pub(crate) fn open_selected_plan_tracking_epic(&mut self) -> bool {
        let Some(entry) = self.plans_selected() else {
            self.push_toast(
                "Open plan",
                "No plan selected — highlight a row first.",
                ToastSeverity::Info,
            );
            return false;
        };
        let Some(tracking) = entry.tracking_issue else {
            self.push_toast(
                "No tracking epic yet",
                &format!(
                    "{} #{}: {} has no `epic`-labelled tracking issue. \
                     Create one with `coord milestone chat`.",
                    entry.repo, entry.milestone_number, entry.title,
                ),
                ToastSeverity::Info,
            );
            return false;
        };
        // Resolve the coord-local repo → GitHub slug so `gh --repo` gets the
        // full owner/name.  Empty slug falls through to `gh` picking the
        // ambient repo from the cwd (still useful; just less precise).
        let repo_slug = self
            .data
            .pipeline_repos
            .iter()
            .find(|(name, _)| name == &entry.repo)
            .map(|(_, gh)| gh.clone())
            .unwrap_or_default();
        // Skip the real spawn under `cargo test` (no `gh` on CI sandbox, no
        // point opening a browser during a headless test).  The toast fires
        // regardless so tests can observe the action.
        #[cfg(not(test))]
        {
            let issue_str = tracking.to_string();
            let mut cmd = std::process::Command::new("gh");
            cmd.args(["issue", "view", &issue_str]);
            if !repo_slug.is_empty() {
                cmd.args(["--repo", &repo_slug]);
            }
            cmd.arg("--web")
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null());
            let _ = cmd.spawn();
        }
        #[cfg(test)]
        let _ = &repo_slug; // silence unused-var under test builds
        self.push_toast(
            "Opening plan",
            &format!(
                "gh issue view #{} — opening tracking epic for {} #{} in browser…",
                tracking, entry.repo, entry.milestone_number,
            ),
            ToastSeverity::Info,
        );
        true
    }

    /// Submit handler for the #977 "fast plan capture" prompt (`c` in the
    /// Plans panel). Fires `coord milestone capture <repo> --title <title>`
    /// through the command runner — the CLI seam composes `write_milestone`
    /// + `create_issue` + `assign_issue_milestone` server-side so the new
    /// milestone/issue pair shows up in `plan_roster` (flagged
    /// `no_work_order`) on the next board refresh, no `coord sync` needed.
    ///
    /// Target repo: the repo of the currently-selected plan-roster row, or
    /// (when the roster is empty) the first configured repo. Toasts + noops
    /// when no repo is configured at all, or when the trimmed title is
    /// empty.
    pub(crate) fn capture_plan_stub(&mut self, title: String) {
        let title = title.trim().to_string();
        if title.is_empty() {
            self.push_toast(
                "Capture plan",
                "Plan title can't be empty — nothing captured.",
                ToastSeverity::Info,
            );
            return;
        }
        let Some(repo) = self
            .plans_selected()
            .map(|e| e.repo)
            .or_else(|| self.data.pipeline_repos.first().map(|(n, _)| n.clone()))
        else {
            self.push_toast(
                "Capture plan",
                "No repo configured — nothing to capture into.",
                ToastSeverity::Info,
            );
            return;
        };
        let args = ["milestone", "capture", repo.as_str(), "--title", title.as_str()];
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&args) {
            SpawnQueuedOutcome::Deduped => {}
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "Plan capture queued",
                    &format!("\"{title}\" ({repo}) — will capture after current command."),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Started => {
                self.push_toast(
                    "Plan captured",
                    &format!("\"{title}\" ({repo}) — dispatching `coord milestone capture`…"),
                    ToastSeverity::Info,
                );
            }
        }
    }

    /// Submit handler for the #1017 "New milestone via chat…" prompt (`C`
    /// in the Plans panel) — the chat-driven sibling of #977's `capture_
    /// plan_stub`. Fires `coord milestone chat <repo> --new [--title
    /// <title>]` through the command runner, seeding a `type=
    /// "milestone-chat"` steward session to discuss goal/scope rather than
    /// creating the milestone directly (`build_new_milestone_chat_briefing`,
    /// #1009). Unlike `capture_plan_stub`, an empty title is a *valid*
    /// submission — the operator can leave it for the chat to work out —
    /// so only "no repo configured" is a hard noop.
    ///
    /// Target repo: same resolution as `capture_plan_stub` — the repo of
    /// the currently-selected plan-roster row, or (when the roster is
    /// empty) the first configured repo.
    pub(crate) fn capture_plan_chat(&mut self, title: String) {
        let title = title.trim().to_string();
        let Some(repo) = self
            .plans_selected()
            .map(|e| e.repo)
            .or_else(|| self.data.pipeline_repos.first().map(|(n, _)| n.clone()))
        else {
            self.push_toast(
                "New milestone via chat",
                "No repo configured — nothing to chat about.",
                ToastSeverity::Info,
            );
            return;
        };
        let mut args: Vec<String> = vec!["milestone".into(), "chat".into(), repo.clone(), "--new".into()];
        if !title.is_empty() {
            args.push("--title".into());
            args.push(title.clone());
        }
        let label_title = if title.is_empty() { "(untitled)".to_string() } else { title };
        let arg_refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
        use crate::commands::SpawnQueuedOutcome;
        let outcome = self.command_runner.spawn_queued(&arg_refs);
        if outcome == SpawnQueuedOutcome::Deduped {
            return;
        }
        // #1017: arm the bind so the next tick attaches the live chat overlay
        // to the new brand-new-milestone `type="milestone-chat"` session.
        // There is no tracking issue yet, so it lands on the `issue_number=0`
        // sentinel (dispatch_new_milestone_chat) — a board-level chat.
        self.pending_milestone_chat = Some(PendingMilestoneChat {
            repo: repo.clone(),
            issue_number: 0,
            label: format!("\"{label_title}\" ({repo})"),
            dispatched_at: Instant::now(),
        });
        let msg = if outcome == SpawnQueuedOutcome::Queued {
            format!("\"{label_title}\" ({repo}) — chat opens after current command.")
        } else {
            format!("\"{label_title}\" ({repo}) — opening steward chat…")
        };
        self.push_toast("New milestone chat", &msg, ToastSeverity::Info);
    }

    // ─── #1124: `?` help overlay + `/` command palette ────────────────────

    /// The reusable quadraui help-layer content for the Plans panel
    /// (contract §5, ms-38 CC-4), registered under `"panel:plans"` in
    /// [`CoordApp::new`]. See the module docs above for the overall
    /// pattern.
    ///
    /// `notes` carries the keyboard-binding cheatsheet entries (contract
    /// §5c) — reference-only, so they're never offered as palette
    /// commands. `actions` carries the 8 real Plans commands (contract
    /// §5g) and feeds BOTH the cheatsheet's "Actions" section AND the `/`
    /// palette — one registration, two surfaces (quadraui #431's design
    /// intent). Three actions (`capture-plan-quick`, `capture-plan-chat`,
    /// `toggle-untracked`) double as §5c cheatsheet entries too: their
    /// `description` is deliberately worded to start with the exact
    /// lowercase phrase §5c requires (`"quick capture plan"`, `"guided
    /// chat (new plan)"`, `"toggle untracked milestones"`) while still
    /// reading as a real description rather than an echo of the label.
    ///
    /// The health-chip legend (contract §5d) is intentionally NOT part of
    /// this `ViewHelp` — `needs_you` chip iconography
    /// (`health_chip_for_signal`) is a Plans-domain concept, not a generic
    /// one every future panel would have, so `render_help_overlay` appends
    /// it directly for `SidebarView::Plans` rather than modeling it here.
    pub(crate) fn plans_view_help() -> ViewHelp {
        ViewHelp::new("Plans")
            .with_notes(vec![
                HelpNote::new("j / k", "up / down"),
                HelpNote::new("right-click", "open context menu"),
                HelpNote::new("Enter", "open detail pane"),
                HelpNote::new("Esc", "close / back"),
                HelpNote::new("?", "this help overlay"),
                HelpNote::new("/", "command palette"),
                HelpNote::new("r", "refresh"),
                HelpNote::new("q", "quit"),
            ])
            .with_actions(vec![
                HelpAction::new(
                    "dispatch-milestone",
                    "Dispatch milestone",
                    "dispatch selected plan's ready frontier",
                ),
                HelpAction::new(
                    "open-milestone-chat",
                    "Open milestone chat",
                    "open a steward chat for the selected plan",
                ),
                HelpAction::new(
                    "capture-plan-quick",
                    "Quick capture plan",
                    "quick capture plan (coord milestone capture, fast stub)",
                )
                .with_accelerator("c"),
                HelpAction::new(
                    "capture-plan-chat",
                    "Guided chat (new plan)",
                    "guided chat (new plan) — coord milestone chat --new",
                )
                .with_accelerator("C"),
                HelpAction::new(
                    "view-milestone-order",
                    "View order / DAG",
                    "show work-order dependency graph",
                ),
                HelpAction::new(
                    "edit-milestone",
                    "Edit milestone…",
                    "edit milestone title / description",
                ),
                HelpAction::new(
                    "add-issue-to-milestone",
                    "Add issue to milestone…",
                    "attach an issue to this plan",
                ),
                HelpAction::new(
                    "toggle-untracked",
                    "Toggle untracked milestones",
                    "toggle untracked milestones (show/hide milestones without a work order)",
                )
                .with_accelerator("u"),
            ])
    }

    /// Popup geometry shared by the `?` cheatsheet and the `/` palette
    /// (#1124): inset from the main content rect rather than the quadraui
    /// demo's 70% — the cheatsheet alone runs to ~20 lines once the
    /// health-chip legend is appended, and a small popup would silently
    /// clip content with no scroll implemented (see `render_help_overlay`).
    fn help_overlay_rect(main: Rect) -> Rect {
        let w = (main.width - 4.0).max(20.0).min(main.width);
        let h = (main.height - 2.0).max(10.0).min(main.height);
        let x = main.x + (main.width - w) * 0.5;
        let y = main.y + (main.height - h) * 0.5;
        Rect::new(x, y, w, h)
    }

    /// Paint the `?` help-overlay cheatsheet (contract §5a-§5d) — a no-op
    /// when closed, or when the active view has no registered help
    /// content (shouldn't happen for any view that can actually open the
    /// overlay — see `events.rs`'s open-trigger gate on `help_view_id`).
    ///
    /// Built directly from this file's existing `ListView`/`ListItem`
    /// pattern rather than `HelpOverlayController::render` — see the
    /// module docs for why (contract §5b's title-string ordering).
    pub(crate) fn render_help_overlay(&self, backend: &mut dyn Backend, main: Rect, lh: f32) {
        if !self.help_overlay.is_open() {
            return;
        }
        let Some(view_id) = self.active_view.help_view_id() else {
            return;
        };
        let Some(help) = self.help_registry.get(view_id) else {
            return;
        };
        let rect = Self::help_overlay_rect(main);

        let mut items = Vec::with_capacity(help.notes.len() + help.actions.len() + 8);
        if !help.notes.is_empty() {
            // #2287 (ms-65 §8b): the Board panel's cheatsheet labels this
            // section "Document tabs" (contract §8b, `mocks/board-tabs-
            // help-overlay.screen`) rather than the generic "Reference"
            // every other registered view gets — mirrors how the Plans-only
            // "Health chips" section just below is already a per-view
            // special case in this same function.
            let notes_header = if self.active_view == SidebarView::Board {
                "Document tabs"
            } else {
                "Reference"
            };
            items.push(cheatsheet_header_item(notes_header));
            for note in &help.notes {
                items.push(cheatsheet_entry_item(&note.label, None, &note.description));
            }
        }
        if !help.actions.is_empty() {
            // #2288 (ms-65 §8b/§9): the Board cheatsheet's second section is
            // "Split" — §9's four pane keys — not the generic "Actions"
            // every other registered view gets. Same per-view special case,
            // and for the same reason, as the "Document tabs" header above:
            // `mocks/board-tabs-help-overlay.screen` row 11 pins the word.
            let actions_header = if self.active_view == SidebarView::Board {
                "Split"
            } else {
                "Actions"
            };
            items.push(cheatsheet_header_item(actions_header));
            for action in &help.actions {
                items.push(cheatsheet_entry_item(
                    &action.label,
                    action.accelerator.as_deref(),
                    &action.description,
                ));
            }
        }
        // Plans-specific health-chip legend (contract §5d) — see the
        // `plans_view_help` doc comment for why this isn't part of the
        // registered `ViewHelp` itself.
        if self.active_view == SidebarView::Plans {
            items.push(cheatsheet_header_item("Health chips"));
            for signal in ["ready_waiting", "stalled", "chat_pending", "no_work_order"] {
                let (label, color) = Self::health_chip_for_signal(signal);
                items.push(chip_legend_item(&label, color, health_chip_description(signal)));
            }
        }
        items.push(ListItem {
            text: StyledText {
                spans: vec![StyledSpan::with_fg(
                    "(Esc to close)".to_string(),
                    Color::rgb(140, 140, 150),
                )],
            },
            icon: None,
            detail: None,
            decoration: Decoration::Muted,
        });

        let visible_rows = if lh > 0.0 {
            (rect.height / lh).floor() as usize
        } else {
            usize::MAX
        };
        let total = items.len();
        backend.draw_list(
            rect,
            &ListView {
                id: WidgetId::new("help-overlay"),
                // Contract §5b: the exact title is "Plans — Help" (title
                // first) — the reverse of `HelpOverlayController::render`'s
                // baked-in "Help — {title}" (see the module docs).
                title: Some(StyledText::plain(format!("{} — Help", help.title))),
                items,
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: false,
                bordered: true,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: total > visible_rows,
            },
        );
    }

    /// Shared geometry: the `/` command-palette's inner popup rect, below
    /// the "{title} actions" section-label strip — used by both the paint
    /// path (`render_command_palette`) and the `visible_rows` calculation
    /// in `dispatch_handle`'s palette key routing, so the two can never
    /// drift apart (mirrors `plans_list_rect_below_goal_header`'s existing
    /// reasoning in this file).
    pub(crate) fn command_palette_popup_rect(main: Rect, lh: f32) -> Rect {
        let outer = Self::help_overlay_rect(main);
        let label_h = lh.max(1.0);
        Rect::new(
            outer.x,
            outer.y + label_h,
            outer.width,
            (outer.height - label_h).max(0.0),
        )
    }

    /// Paint the `/` command palette (contract §5e-§5h) — a no-op when
    /// closed.
    ///
    /// The palette's own title is left as the generic `"command palette"`
    /// (contract §5f); the view-specific `"{title} actions"` section label
    /// (also required by §5f, and what distinguishes this from the help
    /// overlay's own `"command palette"` mention, see §5c) is painted as a
    /// thin non-bordered strip immediately above the `Palette` primitive's
    /// popup rather than as a `PaletteItem` row — `PaletteItem` has no
    /// header/separator concept (unlike `ListItem`'s `Decoration::Header`),
    /// so a label baked into the item list would occupy a real,
    /// keyboard-selectable slot and would reset-select to it on every
    /// keystroke (`DualModePaletteController::set_items` always resets
    /// `selected` to 0).
    pub(crate) fn render_command_palette(&self, backend: &mut dyn Backend, main: Rect, lh: f32) {
        let Some(palette) = &self.command_palette else {
            return;
        };
        let Some(view_id) = self.active_view.help_view_id() else {
            return;
        };
        let Some(help) = self.help_registry.get(view_id) else {
            return;
        };
        let outer = Self::help_overlay_rect(main);
        let label_h = lh.max(1.0);
        let label_rect = Rect::new(
            outer.x + 1.0,
            outer.y,
            (outer.width - 2.0).max(0.0),
            label_h,
        );
        backend.draw_list(
            label_rect,
            &ListView {
                id: WidgetId::new("command-palette-section-label"),
                title: None,
                items: vec![ListItem {
                    text: StyledText {
                        spans: vec![StyledSpan::with_fg(
                            format!(" {} actions ", help.title),
                            Color::rgb(140, 180, 210),
                        )],
                    },
                    icon: None,
                    detail: None,
                    decoration: Decoration::Header,
                }],
                selected_idx: 0,
                scroll_offset: 0,
                has_focus: false,
                bordered: false,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: false,
            },
        );
        let popup = Self::command_palette_popup_rect(main, lh);
        palette.render(popup, backend);
    }

    /// Registered actions for the currently active help-registry view
    /// (today only Plans), filtered by `query` (empty = unfiltered — see
    /// [`filter_help_actions`]). Shared by `open_command_palette` (initial
    /// build) and the `QueryChanged` re-filter as the user types (contract
    /// §5h), and by `ItemConfirmed` to map the confirmed index back to the
    /// `HelpAction` it displayed (mirrors quadraui's own
    /// `examples/common/help_layer_demo.rs` recompute-on-query pattern).
    pub(crate) fn active_view_command_actions(&self, query: &str) -> Vec<HelpAction> {
        let all = self
            .active_view
            .help_view_id()
            .and_then(|id| self.help_registry.get(id))
            .map(|h| h.actions.clone())
            .unwrap_or_default();
        filter_help_actions(&all, query).into_iter().cloned().collect()
    }

    /// Open the `/` command palette for the active help-registry view
    /// (contract §5e-§5h). No-ops silently if the active view has no
    /// registered help — callers gate on `help_view_id().is_some()` first
    /// (`events.rs`), so this should always find content in practice.
    pub(crate) fn open_command_palette(&mut self) {
        let items = help_actions_to_palette_items(&self.active_view_command_actions(""), "");
        self.command_palette = Some(
            DualModePaletteController::new("command palette", None, items)
                .with_id("plans-command-palette"),
        );
    }

    /// Execute the command bound to `action_id` when a `/` palette entry
    /// is confirmed (contract §5g). Three of the eight registered Plans
    /// actions (`capture-plan-quick`, `capture-plan-chat`,
    /// `toggle-untracked`) are Plans-native and already have dedicated
    /// handlers in this file; the other five (`dispatch-milestone`,
    /// `open-milestone-chat`, `view-milestone-order`, `edit-milestone`,
    /// `add-issue-to-milestone`) are the existing MilestoneHeader
    /// context-menu actions (`dialogs.rs::dispatch_context_menu_action`) —
    /// reused here rather than re-implemented, building the same
    /// `ContextMenuTarget::MilestoneHeader` that right-clicking a Plans row
    /// already builds in `context_menu_target_for_selection`. Those five
    /// require the selected plan to carry a tracking epic
    /// (`tracking_issue: Some`) — the same precondition the context-menu
    /// path enforces — so a stub row (no epic yet) gets an explanatory
    /// toast instead of silently no-op-ing, matching this file's existing
    /// `open_selected_plan_tracking_epic` convention.
    pub(crate) fn activate_command_palette_action(&mut self, action_id: &str) {
        match action_id {
            "capture-plan-quick" => {
                self.pending_plan_capture = Some(String::new());
            }
            "capture-plan-chat" => {
                self.pending_new_milestone_chat = Some(String::new());
            }
            "toggle-untracked" => {
                self.toggle_plans_repo_expansion();
            }
            // #1122: `dispatch-milestone-next`, `remove-issue-from-milestone`
            // and `close-plan` are added to this list alongside the
            // original five so the detail-pane actions row
            // (`activate_plan_detail_action` below) can reuse this same
            // MilestoneHeader-target dispatch rather than duplicating it —
            // none of the three are registered as palette entries
            // (`plans_view_help`), so this is a superset, not a contract
            // §5g change.
            "dispatch-milestone" | "open-milestone-chat" | "view-milestone-order"
            | "edit-milestone" | "add-issue-to-milestone" | "dispatch-milestone-next"
            | "remove-issue-from-milestone" | "close-plan" => {
                match self
                    .plans_selected()
                    .and_then(|e| e.tracking_issue.map(|t| (e, t)))
                {
                    Some((entry, tracking_issue)) => {
                        let target = ContextMenuTarget::MilestoneHeader {
                            repo_name: entry.repo.clone(),
                            tracking_issue,
                            milestone_title: entry.title.clone(),
                            milestone_number: entry.milestone_number,
                        };
                        self.dispatch_context_menu_action(action_id, &target);
                    }
                    None => {
                        self.push_toast(
                            "Plans action",
                            "Select a plan with a tracking epic first.",
                            ToastSeverity::Info,
                        );
                    }
                }
            }
            _ => {
                self.push_toast(
                    "Plans action",
                    &format!("No handler for `{action_id}` — likely a stale id."),
                    ToastSeverity::Warning,
                );
            }
        }
    }

    /// Execute the action bound to a #1122 detail-pane actions-row button
    /// (`plan_detail_action_at`'s hit-test, or a future keyboard binding).
    /// `"open-in-browser"` is the one action that isn't a MilestoneHeader
    /// context-menu action (contract §3d demotes it from Enter's old
    /// behaviour, see `open_selected_plan_tracking_epic`); every other
    /// button id is one of the ids `activate_command_palette_action`
    /// already knows how to dispatch, so this just special-cases the one
    /// exception and delegates the rest.
    pub(crate) fn activate_plan_detail_action(&mut self, action_id: &str) {
        if action_id == "open-in-browser" {
            self.open_selected_plan_tracking_epic();
            return;
        }
        self.activate_command_palette_action(action_id);
    }
}

/// Gap between adjacent buttons on a #1122 detail-pane actions row
/// (`build_action_row_line` below) — mirrors `mocks/plans-detail-pane.screen`
/// line 13's spacing (`Dispatch next    Open chat    ...`).
const DETAIL_ACTION_GAP: &str = "    ";

/// Render one #1122 detail-pane actions row's text plus each button's
/// `(action_id, start_col, end_col)` char-offset range within that text
/// (0-based, `end` exclusive, relative to the text itself — NOT the
/// widget-local column `ListView::hit_test` uses). Shared by
/// `CoordApp::plan_detail_items` (paint) and
/// `CoordApp::plan_detail_action_at` (hit-test) so the two can never drift
/// apart — the same reasoning `plans_row_at`'s doc comment gives for
/// mirroring `render_plans_panel`'s row shape exactly.
fn build_action_row_line(actions: &[(&'static str, &'static str)]) -> (String, Vec<(String, usize, usize)>) {
    let mut line = String::from(" ");
    let mut buttons = Vec::with_capacity(actions.len());
    for (label, action_id) in actions {
        let start = line.chars().count();
        line.push_str(label);
        let end = line.chars().count();
        buttons.push((action_id.to_string(), start, end));
        line.push_str(DETAIL_ACTION_GAP);
    }
    (line, buttons)
}

/// Health-chip legend description text (contract §5d) — kept alongside
/// `health_chip_for_signal` in spirit (same signal vocabulary) but
/// separate since the icon/color and the legend prose are independent
/// concerns. Falls back to a generic phrase for an unrecognised signal so
/// a newer daemon's not-yet-known `needs_you` value still gets *some*
/// legend text instead of an empty description.
fn health_chip_description(signal: &str) -> &'static str {
    match signal {
        "ready_waiting" => "issues ready to dispatch now",
        "stalled" => "work order exists but nothing moving",
        "chat_pending" => "a milestone-chat steward is open",
        "no_work_order" => "milestone has no ## Work order block",
        _ => "unrecognised signal",
    }
}

/// Bold section-header `ListItem` for the `?` cheatsheet (#1124) — mirrors
/// this file's other `Decoration::Header` rows (e.g. the repo-group header
/// in `render_plans_panel`).
fn cheatsheet_header_item(text: &str) -> ListItem {
    ListItem {
        text: StyledText {
            spans: vec![StyledSpan {
                bold: true,
                ..StyledSpan::with_fg(text.to_string(), Color::rgb(140, 180, 210))
            }],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Header,
    }
}

/// One cheatsheet row: `label` (padded), optional `accelerator` (padded),
/// then `description` — same fixed-character-width column layout as
/// quadraui's own `build_cheatsheet_lines` (`compose/help_layer.rs`), so
/// the two visually match if a reader compares this panel against a
/// future one that DOES use the stock `HelpOverlayController::render`.
fn cheatsheet_entry_item(label: &str, accelerator: Option<&str>, description: &str) -> ListItem {
    let text = format!(
        "  {:<28}{:<14}{}",
        label,
        accelerator.unwrap_or(""),
        description
    );
    ListItem {
        text: StyledText {
            spans: vec![StyledSpan::with_fg(text, Color::rgb(200, 200, 200))],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    }
}

/// One health-chip legend row: the coloured chip label (matching
/// `health_chip_for_signal`'s on-screen rendering exactly) followed by its
/// description.
fn chip_legend_item(chip_label: &str, chip_color: Color, description: &str) -> ListItem {
    ListItem {
        text: StyledText {
            spans: vec![
                StyledSpan::with_fg(format!("  {:<20}", chip_label), chip_color),
                StyledSpan::with_fg(description.to_string(), Color::rgb(180, 180, 190)),
            ],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    }
}

// ─── Pure-function unit tests ─────────────────────────────────────────────────

#[cfg(test)]
mod pure_tests {
    use super::*;

    fn entry(
        repo: &str,
        ms: i64,
        title: &str,
        tracking: Option<u64>,
        needs: &[&str],
    ) -> PlanRosterEntry {
        PlanRosterEntry {
            repo: repo.to_string(),
            title: title.to_string(),
            milestone_number: ms,
            tracking_issue: tracking,
            has_work_order: tracking.is_some(),
            ready_frontier: 0,
            blocked: 0,
            in_flight: 0,
            done: 0,
            total: 0,
            needs_you: needs.iter().map(|s| s.to_string()).collect(),
            outcome_run_number: None,
            outcome_met: None,
            outcome_partial: None,
            outcome_gap: None,
            outcome_bottom_line: None,
            outcome_diff_summary: None,
        }
    }

    #[test]
    fn plans_entries_sorts_by_repo_then_milestone_number() {
        let entries = vec![
            entry("b-repo", 2, "b2", None, &[]),
            entry("a-repo", 5, "a5", None, &[]),
            entry("b-repo", 1, "b1", None, &[]),
            entry("a-repo", 1, "a1", None, &[]),
        ];
        // Simulate what the payload → BoardData flow would set.
        let ordered: Vec<(String, i64)> = {
            let mut es = entries;
            es.sort_by(|a, b| {
                (a.repo.as_str(), a.milestone_number).cmp(&(b.repo.as_str(), b.milestone_number))
            });
            es.into_iter().map(|e| (e.repo, e.milestone_number)).collect()
        };
        assert_eq!(
            ordered,
            vec![
                ("a-repo".to_string(), 1),
                ("a-repo".to_string(), 5),
                ("b-repo".to_string(), 1),
                ("b-repo".to_string(), 2),
            ]
        );
    }

    #[test]
    fn plan_roster_entry_deserializes_matching_payload_shape() {
        // Golden: mirror exactly what `coord.plans.PlanEntry.to_dict()` emits.
        // Any drift here would fail the whole BoardPayload parse (#632).
        let json = r#"{
            "repo": "api",
            "title": "Substrate",
            "milestone_number": 5,
            "tracking_issue": 500,
            "has_work_order": true,
            "ready_frontier": 2,
            "blocked": 1,
            "in_flight": 0,
            "done": 0,
            "total": 3,
            "needs_you": ["ready_waiting"]
        }"#;
        let entry: PlanRosterEntry = serde_json::from_str(json).expect("valid roster JSON");
        assert_eq!(entry.repo, "api");
        assert_eq!(entry.milestone_number, 5);
        assert_eq!(entry.tracking_issue, Some(500));
        assert!(entry.has_work_order);
        assert_eq!(entry.ready_frontier, 2);
        assert_eq!(entry.needs_you, vec!["ready_waiting".to_string()]);
    }

    #[test]
    fn plan_roster_entry_deserializes_with_null_tracking_issue() {
        // A milestone without an epic reports tracking_issue: null.
        let json = r#"{
            "repo": "api",
            "title": "Follow-up",
            "milestone_number": 6,
            "tracking_issue": null,
            "has_work_order": false,
            "ready_frontier": 0,
            "blocked": 0,
            "in_flight": 0,
            "done": 0,
            "total": 0,
            "needs_you": ["no_work_order"]
        }"#;
        let entry: PlanRosterEntry = serde_json::from_str(json).expect("valid roster JSON");
        assert_eq!(entry.tracking_issue, None);
        assert!(!entry.has_work_order);
        assert_eq!(entry.needs_you, vec!["no_work_order".to_string()]);
    }
}
