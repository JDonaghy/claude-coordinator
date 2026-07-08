//! Terminal-view left-pane machine-grouped tree of open terminals (#953).
//!
//! Read-only discovery tree: parent nodes are fleet machines
//! (`self.data.machines`), child nodes are persistent `coord-term-*`
//! terminals discovered via `coord terminal list --json[--remote]`
//! (`FleetTerminal`, populated in `data.rs` — #952 backend). Create / kill /
//! attach are out of scope here — see #953's non-goals (later issues build
//! on this tree).
//!
//! This is the app's first sidebar built from a raw quadraui `TreeView`
//! drawn via `backend.draw_tree` directly, rather than through the
//! `SidebarSystem` compose widget used by the Board/Pipeline panels.
//! `SidebarSystem` owns its own hit-testing (chevron-vs-row split regions,
//! multi-section search forms, etc.) which is overkill for one flat
//! machine→terminal tree — so click dispatch here uses the same simple
//! flat pixel-row math the Machines panel already uses (`mouse_sidebar_click`
//! → `SidebarView::Machines`), not `TreeView::layout`/`TreeViewLayout`
//! (which would pull in backend-specific row-measurement code the app is
//! meant to stay free of — see the `app/mod.rs` module doc).
//!
//! **Import pattern:** `use super::*` is intentional — the impl methods
//! live on `CoordApp` and need the full parent namespace. See
//! `sessions.rs` / `terminal.rs` for the same rationale.
#[allow(unused_imports)]
use super::*;

/// One row's identity in the flattened Terminal-view tree, in the SAME
/// order as the `TreeRow`s returned by [`CoordApp::terminal_tree_rows`] —
/// index parity is what lets a flat pixel-row index resolve back to a
/// machine or a terminal without re-deriving tree structure at the call
/// site.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum TerminalTreeRow {
    /// Index into `self.data.machines`.
    Machine(usize),
    /// `(machine index, index into that machine's sorted terminal list)`.
    Terminal(usize, usize),
}

impl CoordApp {
    /// Terminals hosted on `machine_name`, sorted by name for a stable,
    /// deterministic display and click-index mapping.
    fn fleet_terminals_for_machine(&self, machine_name: &str) -> Vec<&FleetTerminal> {
        let mut v: Vec<&FleetTerminal> = self
            .fleet_terminals
            .iter()
            .filter(|t| t.machine == machine_name)
            .collect();
        v.sort_by(|a, b| a.name.cmp(&b.name));
        v
    }

    /// Whether a machine row should paint expanded, absent an explicit
    /// toggle in `terminal_tree_expanded`: expanded once it hosts ≥1
    /// terminal (so newly-discovered terminals aren't hidden by default),
    /// collapsed otherwise (#953: "machines with no terminals render
    /// collapsed/greyed").
    fn terminal_tree_machine_expanded(&self, machine_name: &str, has_terminals: bool) -> bool {
        *self
            .terminal_tree_expanded
            .get(machine_name)
            .unwrap_or(&has_terminals)
    }

    /// Build the Terminal-view left-pane tree: one `TreeRow` per fleet
    /// machine (parent, reachability-colored dot), with that machine's
    /// terminals nested beneath when expanded. Returns the `TreeRow`s
    /// alongside a same-length/same-order `TerminalTreeRow` index so click
    /// / keyboard-nav handlers can map a flat row index back to the
    /// machine/terminal it represents.
    pub(crate) fn terminal_tree_rows(&self) -> (Vec<TreeRow>, Vec<TerminalTreeRow>) {
        let mut rows = Vec::new();
        let mut index = Vec::new();
        for (mi, m) in self.data.machines.iter().enumerate() {
            let terms = self.fleet_terminals_for_machine(&m.name);
            let has_terms = !terms.is_empty();
            let expanded = self.terminal_tree_machine_expanded(&m.name, has_terms);

            let (dot_col, dot) = if m.reachable {
                (Color::rgb(70, 210, 70), "\u{25cf} ")
            } else {
                (Color::rgb(90, 90, 90), "\u{25cb} ")
            };
            let name_col = if has_terms {
                Color::rgb(220, 220, 220)
            } else {
                Color::rgb(120, 120, 120)
            };
            let mut spans = vec![
                StyledSpan::with_fg(dot, dot_col),
                StyledSpan::with_fg(m.name.clone(), name_col),
            ];
            if has_terms {
                spans.push(StyledSpan::with_fg(
                    format!(" ({})", terms.len()),
                    Color::rgb(140, 140, 140),
                ));
            }
            rows.push(TreeRow {
                path: vec![mi as u16],
                indent: 0,
                icon: None,
                text: StyledText { spans },
                badge: None,
                // Only branch (chevron-bearing) when there's something to
                // expand — an empty machine has no affordance to toggle.
                is_expanded: if has_terms { Some(expanded) } else { None },
                decoration: Decoration::Normal,
                edit: None,
            });
            index.push(TerminalTreeRow::Machine(mi));

            if has_terms && expanded {
                for (ti, t) in terms.iter().enumerate() {
                    let mut spans = vec![StyledSpan::plain(format!("  {}", t.name))];
                    if t.attached {
                        spans.push(StyledSpan::with_fg(
                            " [attached]",
                            Color::rgb(140, 200, 140),
                        ));
                    }
                    rows.push(TreeRow {
                        path: vec![mi as u16, ti as u16],
                        indent: 1,
                        icon: None,
                        text: StyledText { spans },
                        badge: None,
                        is_expanded: None,
                        decoration: Decoration::Normal,
                        edit: None,
                    });
                    index.push(TerminalTreeRow::Terminal(mi, ti));
                }
            }
        }
        (rows, index)
    }

    /// Build the `TreeView` widget for `render.rs`'s `SidebarView::Terminal`
    /// sidebar branch.
    pub(crate) fn terminal_tree_view(&self) -> TreeView {
        let (rows, _) = self.terminal_tree_rows();
        TreeView {
            id: WidgetId::new("terminal-tree"),
            rows,
            selection_mode: SelectionMode::Single,
            selected_path: self.terminal_tree_selected.clone(),
            scroll_offset: self.terminal_tree_scroll,
            style: TreeStyle::default(),
            // The tree only reads as "focused" (selection highlight) when
            // the PTY pane itself isn't soaking up keyboard focus (#424).
            has_focus: !self.terminal_focused,
        }
    }

    /// #955: `(machine, name)` identity of the fleet terminal selected in
    /// the Terminal-view tree, or `None` when nothing is selected, the
    /// selection is a machine row rather than a terminal leaf, or the
    /// selected leaf no longer resolves (e.g. after a discovery refresh
    /// reshuffled the sorted per-machine terminal list). Drives which PTY
    /// (`fleet_terminal_sessions[key]` vs. the bare `terminal_session`
    /// fallback) the standalone Terminal view's main pane shows —
    /// `render.rs` and `terminal.rs`'s `drive_terminal_pane` both call this
    /// rather than re-deriving the tree lookup.
    pub(crate) fn selected_fleet_terminal_key(&self) -> Option<(String, String)> {
        let path = self.terminal_tree_selected.as_ref()?;
        let [mi, ti] = path.as_slice() else {
            return None;
        };
        let m = self.data.machines.get(*mi as usize)?;
        let t = self
            .fleet_terminals_for_machine(&m.name)
            .get(*ti as usize)
            .copied()?;
        Some((t.machine.clone(), t.name.clone()))
    }

    /// Flattened index of `terminal_tree_selected` within `index`, if any
    /// (and if it still resolves — e.g. after a collapse changed the row
    /// count).
    fn terminal_tree_selected_flat_index(&self, index: &[TerminalTreeRow]) -> Option<usize> {
        let path = self.terminal_tree_selected.as_ref()?;
        index.iter().position(|e| match (e, path.as_slice()) {
            (TerminalTreeRow::Machine(mi), [p0]) => *mi as u16 == *p0,
            (TerminalTreeRow::Terminal(mi, ti), [p0, p1]) => *mi as u16 == *p0 && *ti as u16 == *p1,
            _ => false,
        })
    }

    /// Handle a click at flattened row `row_idx` (0-based, already
    /// accounting for `terminal_tree_scroll` — matching how
    /// `mouse_sidebar_click` derives it from pixel position). Toggles
    /// expand state on a machine row that has terminals, and always moves
    /// the selected-node cursor onto the clicked row. Returns `true` when a
    /// redraw is needed; `false` when `row_idx` is out of range (empty
    /// click area below the last row).
    pub(crate) fn terminal_tree_click_row(&mut self, row_idx: usize) -> bool {
        let (_, index) = self.terminal_tree_rows();
        let Some(entry) = index.get(row_idx).copied() else {
            return false;
        };
        match entry {
            TerminalTreeRow::Machine(mi) => {
                self.terminal_tree_selected = Some(vec![mi as u16]);
                if let Some(m) = self.data.machines.get(mi) {
                    let name = m.name.clone();
                    if !self.fleet_terminals_for_machine(&name).is_empty() {
                        let cur = self.terminal_tree_machine_expanded(&name, true);
                        self.terminal_tree_expanded.insert(name, !cur);
                    }
                }
                true
            }
            TerminalTreeRow::Terminal(mi, ti) => {
                self.terminal_tree_selected = Some(vec![mi as u16, ti as u16]);
                true
            }
        }
    }

    /// Move the selected-node cursor by one flattened row (`delta > 0` for
    /// Down/j, `delta < 0` for Up/k). Clamps at the top/bottom rather than
    /// wrapping. No-op when the tree is empty. Returns `true` on change.
    pub(crate) fn terminal_tree_move_selection(&mut self, delta: i32) -> bool {
        let (_, index) = self.terminal_tree_rows();
        if index.is_empty() {
            return false;
        }
        let cur = self
            .terminal_tree_selected_flat_index(&index)
            .unwrap_or(0);
        let next = if delta < 0 {
            cur.saturating_sub(1)
        } else {
            (cur + 1).min(index.len() - 1)
        };
        if next == cur && self.terminal_tree_selected.is_some() {
            return false;
        }
        self.terminal_tree_selected = Some(match index[next] {
            TerminalTreeRow::Machine(mi) => vec![mi as u16],
            TerminalTreeRow::Terminal(mi, ti) => vec![mi as u16, ti as u16],
        });
        true
    }

    /// Keep the selected Terminal-tree row inside the visible window —
    /// mirrors `fix_machine_scroll`. Must be called after every
    /// click/j/k navigation.
    pub(crate) fn fix_terminal_tree_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        let (_, index) = self.terminal_tree_rows();
        let Some(sel) = self.terminal_tree_selected_flat_index(&index) else {
            return;
        };
        if sel < self.terminal_tree_scroll {
            self.terminal_tree_scroll = sel;
        } else if sel >= self.terminal_tree_scroll + visible {
            self.terminal_tree_scroll = sel + 1 - visible;
        }
    }
}
