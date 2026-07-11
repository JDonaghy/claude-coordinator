//! Sessions-view left-pane machine → repo grouped tree of live claude work
//! sessions (#1032), plus the attach / kill / stop actions on its selected
//! leaf (#1033).
//!
//! Extends the #953 Terminal-view tree pattern (`fleet_terminals.rs`) one
//! level deeper: parent nodes are fleet machines (`self.data.machines`),
//! middle nodes group that machine's live `coord-<aid>` sessions
//! (`self.live_tmux_sessions`, #487 discovery) by `repo_name`, and leaf
//! nodes are the sessions themselves. #1032 was nav/select only; #1033 wires
//! the selected leaf to the EXISTING verbs in `sessions.rs`
//! (`reattach_session_by_aid`, `kill_session_by_aid`) rather than
//! reimplementing them — mirroring the Terminal tree's own staged rollout
//! (#953 discovery-only → #954 create+attach → #956 kill).
//!
//! **Import pattern:** `use super::*` is intentional — the impl methods
//! live on `CoordApp` and need the full parent namespace. See
//! `sessions.rs` / `fleet_terminals.rs` for the same rationale.
#[allow(unused_imports)]
use super::*;

/// One row's identity in the flattened Sessions-view tree, in the SAME
/// order as the `TreeRow`s returned by [`CoordApp::sessions_tree_rows`] —
/// index parity is what lets a flat pixel-row index resolve back to a
/// machine, repo group, or session without re-deriving tree structure at
/// the call site.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum SessionsTreeRow {
    /// Index into `self.data.machines`.
    Machine(usize),
    /// `(machine index, repo-group index within that machine's sessions —
    /// grouped and sorted by `repo_name`)`.
    Repo(usize, usize),
    /// `(machine index, repo-group index, session index within that
    /// repo group's sorted session list)`.
    Session(usize, usize, usize),
}

/// #1033: pending "Kill session" confirmation — carries everything
/// `confirm_kill_session` needs to fire `kill_session_by_aid` (`sessions.rs`)
/// without re-deriving the target from the (possibly already-changed) tree
/// selection. Mirrors `fleet_terminals::PendingKillTerminal`'s shape.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PendingKillSession {
    pub(crate) aid: String,
    pub(crate) machine: Option<String>,
    /// Display label for the confirm dialog body — `"#<issue> · <repo>"`,
    /// falling back to the bare aid when issue/repo are unknown.
    pub(crate) label: String,
}

impl CoordApp {
    /// Live sessions hosted on `machine_name`, sorted by `(repo_name,
    /// assignment_id)` for a stable, deterministic display and click-index
    /// mapping. Resolves via `session_machine_for` (not the raw `machine`
    /// field directly) so sessions discovered before a `machine` tag
    /// existed still group under the right fleet machine.
    fn live_sessions_for_machine(&self, machine_name: &str) -> Vec<&LiveTmuxSession> {
        let mut v: Vec<&LiveTmuxSession> = self
            .live_tmux_sessions
            .iter()
            .filter(|s| self.session_machine_for(s) == machine_name)
            .collect();
        v.sort_by(|a, b| {
            let ra = a.repo_name.as_deref().unwrap_or("");
            let rb = b.repo_name.as_deref().unwrap_or("");
            ra.cmp(rb).then_with(|| a.assignment_id.cmp(&b.assignment_id))
        });
        v
    }

    /// Group an already `repo_name`-sorted session slice into
    /// `(repo_name, sessions)` buckets, preserving sort order — so the repo
    /// groups themselves come out alphabetically. Sessions with no known
    /// `repo_name` land in a trailing `"(unknown)"` bucket.
    fn sessions_grouped_by_repo<'a>(
        &self,
        sessions: &[&'a LiveTmuxSession],
    ) -> Vec<(String, Vec<&'a LiveTmuxSession>)> {
        let mut groups: Vec<(String, Vec<&'a LiveTmuxSession>)> = Vec::new();
        for s in sessions {
            let repo = s
                .repo_name
                .clone()
                .unwrap_or_else(|| "(unknown)".to_string());
            match groups.last_mut() {
                Some((r, v)) if *r == repo => v.push(s),
                _ => groups.push((repo, vec![s])),
            }
        }
        groups
    }

    /// Whether a machine row should paint expanded, absent an explicit
    /// toggle in `sessions_tree_expanded`: expanded once it hosts ≥1 live
    /// session (so newly-discovered sessions aren't hidden by default),
    /// collapsed otherwise (mirrors `terminal_tree_machine_expanded`).
    fn sessions_tree_machine_expanded(&self, machine_name: &str, has_sessions: bool) -> bool {
        *self
            .sessions_tree_expanded
            .get(machine_name)
            .unwrap_or(&has_sessions)
    }

    /// Build the Sessions-view left-pane tree: one `TreeRow` per fleet
    /// machine (parent, reachability-colored dot), with that machine's live
    /// sessions grouped by repo beneath when expanded, and each session as
    /// a leaf beneath its repo group. Returns the `TreeRow`s alongside a
    /// same-length/same-order `SessionsTreeRow` index so click/keyboard-nav
    /// handlers can map a flat row index back to the machine/repo/session
    /// it represents.
    pub(crate) fn sessions_tree_rows(&self) -> (Vec<TreeRow>, Vec<SessionsTreeRow>) {
        let mut rows = Vec::new();
        let mut index = Vec::new();
        for (mi, m) in self.data.machines.iter().enumerate() {
            let sessions = self.live_sessions_for_machine(&m.name);
            let has_sessions = !sessions.is_empty();
            let expanded = self.sessions_tree_machine_expanded(&m.name, has_sessions);

            let (dot_col, dot) = if m.reachable {
                (Color::rgb(70, 210, 70), "\u{25cf} ")
            } else {
                (Color::rgb(90, 90, 90), "\u{25cb} ")
            };
            let name_col = if has_sessions {
                Color::rgb(220, 220, 220)
            } else {
                Color::rgb(120, 120, 120)
            };
            let mut spans = vec![
                StyledSpan::with_fg(dot, dot_col),
                StyledSpan::with_fg(m.name.clone(), name_col),
            ];
            if has_sessions {
                spans.push(StyledSpan::with_fg(
                    format!(" ({})", sessions.len()),
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
                is_expanded: if has_sessions { Some(expanded) } else { None },
                decoration: Decoration::Normal,
                edit: None,
            });
            index.push(SessionsTreeRow::Machine(mi));

            if !has_sessions || !expanded {
                continue;
            }
            let groups = self.sessions_grouped_by_repo(&sessions);
            for (ri, (repo, group)) in groups.iter().enumerate() {
                rows.push(TreeRow {
                    path: vec![mi as u16, ri as u16],
                    indent: 1,
                    icon: None,
                    text: StyledText {
                        spans: vec![
                            StyledSpan::with_fg(
                                format!("  {}", repo),
                                Color::rgb(190, 190, 210),
                            ),
                            StyledSpan::with_fg(
                                format!(" ({})", group.len()),
                                Color::rgb(130, 130, 150),
                            ),
                        ],
                    },
                    badge: None,
                    // Repo groups are plain organizational rows in this
                    // read-only slice — no independent collapse toggle.
                    is_expanded: None,
                    decoration: Decoration::Normal,
                    edit: None,
                });
                index.push(SessionsTreeRow::Repo(mi, ri));

                for (si, s) in group.iter().enumerate() {
                    let issue_str = s
                        .issue_number
                        .map(|n| format!("#{}", n))
                        .unwrap_or_else(|| "#?".to_string());
                    // Short — the sidebar's default width (35 cols, #782
                    // `shell_config`) has to fit `issue# · aid_short ·
                    // [attached] · (dead)` all at once at max tag length.
                    let aid_short = trunc(&s.assignment_id, 8);
                    let mut spans = vec![StyledSpan::plain(format!(
                        "  {} · {}",
                        issue_str, aid_short
                    ))];
                    if s.attached {
                        spans.push(StyledSpan::with_fg(
                            " [attached]",
                            Color::rgb(140, 200, 140),
                        ));
                    }
                    if s.pane_dead {
                        spans.push(StyledSpan::with_fg(" (dead)", Color::rgb(200, 110, 110)));
                    }
                    rows.push(TreeRow {
                        path: vec![mi as u16, ri as u16, si as u16],
                        indent: 2,
                        icon: None,
                        text: StyledText { spans },
                        badge: None,
                        is_expanded: None,
                        decoration: Decoration::Normal,
                        edit: None,
                    });
                    index.push(SessionsTreeRow::Session(mi, ri, si));
                }
            }
        }
        (rows, index)
    }

    /// Build the `TreeView` widget for `render.rs`'s `SidebarView::Sessions`
    /// sidebar branch.
    pub(crate) fn sessions_tree_view(&self) -> TreeView {
        let (rows, _) = self.sessions_tree_rows();
        TreeView {
            id: WidgetId::new("sessions-tree"),
            rows,
            selection_mode: SelectionMode::Single,
            selected_path: self.sessions_tree_selected.clone(),
            scroll_offset: self.sessions_tree_scroll,
            style: TreeStyle::default(),
            has_focus: true,
        }
    }

    /// The `LiveTmuxSession` selected in the Sessions-view tree, or `None`
    /// when nothing is selected, the selection is a machine/repo row rather
    /// than a session leaf, or the selected leaf no longer resolves (e.g.
    /// after a discovery refresh reshuffled the sorted per-machine/per-repo
    /// session list). Drives the main-panel detail view.
    pub(crate) fn selected_fleet_session(&self) -> Option<&LiveTmuxSession> {
        let path = self.sessions_tree_selected.as_ref()?;
        let [mi, ri, si] = path.as_slice() else {
            return None;
        };
        let m = self.data.machines.get(*mi as usize)?;
        let sessions = self.live_sessions_for_machine(&m.name);
        let groups = self.sessions_grouped_by_repo(&sessions);
        let (_, group) = groups.get(*ri as usize)?;
        group.get(*si as usize).copied()
    }

    /// Flattened index of `sessions_tree_selected` within `index`, if any
    /// (and if it still resolves — e.g. after a collapse changed the row
    /// count).
    fn sessions_tree_selected_flat_index(&self, index: &[SessionsTreeRow]) -> Option<usize> {
        let path = self.sessions_tree_selected.as_ref()?;
        index.iter().position(|e| match (e, path.as_slice()) {
            (SessionsTreeRow::Machine(mi), [p0]) => *mi as u16 == *p0,
            (SessionsTreeRow::Repo(mi, ri), [p0, p1]) => *mi as u16 == *p0 && *ri as u16 == *p1,
            (SessionsTreeRow::Session(mi, ri, si), [p0, p1, p2]) => {
                *mi as u16 == *p0 && *ri as u16 == *p1 && *si as u16 == *p2
            }
            _ => false,
        })
    }

    /// Handle a click at flattened row `row_idx` (0-based, already
    /// accounting for `sessions_tree_scroll` — matching how
    /// `mouse_sidebar_click` derives it from pixel position). Toggles
    /// expand state on a machine row that has sessions, and always moves
    /// the selected-node cursor onto the clicked row. Returns `true` when a
    /// redraw is needed; `false` when `row_idx` is out of range (empty
    /// click area below the last row).
    pub(crate) fn sessions_tree_click_row(&mut self, row_idx: usize) -> bool {
        let (_, index) = self.sessions_tree_rows();
        let Some(entry) = index.get(row_idx).copied() else {
            return false;
        };
        match entry {
            SessionsTreeRow::Machine(mi) => {
                self.sessions_tree_selected = Some(vec![mi as u16]);
                if let Some(m) = self.data.machines.get(mi) {
                    let name = m.name.clone();
                    if !self.live_sessions_for_machine(&name).is_empty() {
                        let cur = self.sessions_tree_machine_expanded(&name, true);
                        self.sessions_tree_expanded.insert(name, !cur);
                    }
                }
                true
            }
            SessionsTreeRow::Repo(mi, ri) => {
                self.sessions_tree_selected = Some(vec![mi as u16, ri as u16]);
                true
            }
            SessionsTreeRow::Session(mi, ri, si) => {
                self.sessions_tree_selected = Some(vec![mi as u16, ri as u16, si as u16]);
                true
            }
        }
    }

    /// Move the selected-node cursor by one flattened row (`delta > 0` for
    /// Down/j, `delta < 0` for Up/k). Clamps at the top/bottom rather than
    /// wrapping. No-op when the tree is empty. Returns `true` on change.
    pub(crate) fn sessions_tree_move_selection(&mut self, delta: i32) -> bool {
        let (_, index) = self.sessions_tree_rows();
        if index.is_empty() {
            return false;
        }
        let cur = self
            .sessions_tree_selected_flat_index(&index)
            .unwrap_or(0);
        let next = if delta < 0 {
            cur.saturating_sub(1)
        } else {
            (cur + 1).min(index.len() - 1)
        };
        if next == cur && self.sessions_tree_selected.is_some() {
            return false;
        }
        self.sessions_tree_selected = Some(match index[next] {
            SessionsTreeRow::Machine(mi) => vec![mi as u16],
            SessionsTreeRow::Repo(mi, ri) => vec![mi as u16, ri as u16],
            SessionsTreeRow::Session(mi, ri, si) => vec![mi as u16, ri as u16, si as u16],
        });
        true
    }

    /// Keep the selected Sessions-tree row inside the visible window —
    /// mirrors `fix_terminal_tree_scroll`. Must be called after every
    /// click/j/k navigation.
    pub(crate) fn fix_sessions_tree_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        let (_, index) = self.sessions_tree_rows();
        let Some(sel) = self.sessions_tree_selected_flat_index(&index) else {
            return;
        };
        if sel < self.sessions_tree_scroll {
            self.sessions_tree_scroll = sel;
        } else if sel >= self.sessions_tree_scroll + visible {
            self.sessions_tree_scroll = sel + 1 - visible;
        }
    }

    // ── #1033: Sessions-panel actions (attach / kill / stop) ────────────────
    // Wires the tree's selected leaf to the EXISTING verbs in `sessions.rs`
    // (`reattach_session_by_aid`, `kill_session_by_aid`) — this file adds no
    // new attach/kill/stop mechanics, only the tree-selection plumbing and,
    // for kill, a confirm step (the `L` overlay killed directly on `K`; the
    // Sessions panel is the primary, always-visible view so an accidental
    // keypress is more costly — hence the extra confirm, same discipline as
    // `fleet_terminals::open_kill_terminal_confirm`).

    /// Arm the "Kill session" confirm dialog for the currently-selected
    /// Sessions-tree leaf. No-op (returns `false`) when the selection isn't
    /// a session row — e.g. a machine/repo row or nothing selected.
    pub(crate) fn open_kill_session_confirm(&mut self) -> bool {
        let Some(s) = self.selected_fleet_session() else {
            return false;
        };
        let label = match s.issue_number {
            Some(n) => format!("#{} · {}", n, s.repo_name.as_deref().unwrap_or("(unknown)")),
            None => s.assignment_id.clone(),
        };
        self.pending_kill_session = Some(PendingKillSession {
            aid: s.assignment_id.clone(),
            machine: s.machine.clone(),
            label,
        });
        true
    }

    /// Fire the confirmed kill (#1033): delegates to `kill_session_by_aid`
    /// (`sessions.rs`) — reuse, not reimplement — then re-resolves the
    /// tree's selected-node cursor so it doesn't point at a stale path once
    /// the killed leaf (and possibly its now-empty repo/machine group)
    /// disappears. Mirrors `fleet_terminals::confirm_kill_terminal`'s
    /// post-kill selection fixup.
    pub(crate) fn confirm_kill_session(&mut self, pending: PendingKillSession) {
        self.kill_session_by_aid(&pending.aid, pending.machine.as_deref());

        let (_, index) = self.sessions_tree_rows();
        if self.sessions_tree_selected_flat_index(&index).is_none() {
            self.sessions_tree_selected = index.last().map(|entry| match *entry {
                SessionsTreeRow::Machine(mi) => vec![mi as u16],
                SessionsTreeRow::Repo(mi, ri) => vec![mi as u16, ri as u16],
                SessionsTreeRow::Session(mi, ri, si) => vec![mi as u16, ri as u16, si as u16],
            });
        }
    }

    /// Reattach to the currently-selected Sessions-tree leaf. No-op when the
    /// selection isn't a session row. Delegates entirely to
    /// [`CoordApp::reattach_session_by_aid`] — zero new reattach code.
    pub(crate) fn reattach_selected_fleet_session(&mut self) -> bool {
        let Some(aid) = self.selected_fleet_session().map(|s| s.assignment_id.clone()) else {
            return false;
        };
        self.reattach_session_by_aid(&aid);
        true
    }

    /// Stop (finalize) the assignment behind the currently-selected
    /// Sessions-tree leaf via `coord stop <aid>`. No-op (returns `false`)
    /// when the selection isn't a session row — mirrors the `L` overlay's
    /// `f` handler, no extra confirm (stop is non-destructive to the
    /// worktree, unlike kill).
    ///
    /// #1033 fix: the previous version fired `coord stop` with NO feedback
    /// at all — no status-bar message, no toast, on success OR failure —
    /// which made the action indistinguishable from "not wired" (the
    /// reviewer's repro). Uses a toast (not `pipeline_status`, which only
    /// renders inside the Pipeline detail view) so the confirmation is
    /// actually visible from the Sessions panel itself — `pipeline_status`
    /// is still set too, mirroring `dispatch_stop_for_selected_pipeline_row`
    /// / `kill_watched`, in case the operator switches to Pipeline shortly
    /// after.
    ///
    /// Also guards against firing `coord stop` on a leaf whose board
    /// assignment is already non-`running`: the Sessions tree can select a
    /// "zombie" leaf — a discovered tmux session still alive after its
    /// board assignment already finished (see
    /// `selected_issue_any_session_id`'s doc comment) — and `coord stop`'s
    /// CLI unconditionally POSTs `/cancel` + marks the assignment `failed`
    /// regardless of its current status. Firing it blindly on an
    /// already-finalized assignment (e.g. `review done`) would silently
    /// flip a completed assignment to `failed` instead of no-op'ing, so we
    /// check first and surface a clear "nothing to stop" message instead.
    pub(crate) fn stop_selected_fleet_session(&mut self) -> bool {
        let Some(s) = self.selected_fleet_session() else {
            return false;
        };
        let aid = s.assignment_id.clone();
        let issue_label = s
            .issue_number
            .map(|n| format!("#{}", n))
            .unwrap_or_else(|| aid.clone());
        if !self.session_assignment_is_running(&aid) {
            self.push_toast(
                "Nothing to stop",
                &format!("{issue_label}: assignment already finished — nothing to stop."),
                ToastSeverity::Info,
            );
            return true;
        }
        use crate::commands::SpawnQueuedOutcome;
        match self.command_runner.spawn_queued(&["stop", &aid]) {
            SpawnQueuedOutcome::Started => {
                self.pipeline_status = Some((
                    format!("stop dispatched for {issue_label}"),
                    Instant::now(),
                ));
                self.push_toast(
                    "Stop dispatched",
                    &format!("{issue_label}: `coord stop` running…"),
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Queued => {
                self.push_toast(
                    "⏳ Queued",
                    "stop runs after current command",
                    ToastSeverity::Info,
                );
            }
            SpawnQueuedOutcome::Deduped => {}
        }
        true
    }
}
