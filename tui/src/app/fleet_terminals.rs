//! Terminal-view left-pane machine-grouped tree of open terminals (#953).
//!
//! Discovery tree: parent nodes are fleet machines (`self.data.machines`),
//! child nodes are persistent `coord-term-*` terminals discovered via
//! `coord terminal list --json[--remote]` (`FleetTerminal`, populated in
//! `data.rs` — #952 backend). #954 adds the create+attach affordance on top
//! of this tree (`open_new_terminal_picker` / `create_and_attach_terminal`);
//! kill (#5) is still out of scope here.
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
                    if t.pending {
                        // #954: optimistic entry inserted by
                        // `create_and_attach_terminal`, not yet confirmed by
                        // a `coord terminal list` discovery sweep.
                        spans.push(StyledSpan::with_fg(
                            " (creating…)",
                            Color::rgb(200, 180, 90),
                        ));
                    } else if t.attached {
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

    // ── #954: create a new terminal on a chosen fleet machine ────────────

    /// Fleet machines eligible to host a NEW terminal. Unlike
    /// `fleet_machines_for_repo` (`sessions.rs`), a plain terminal carries
    /// no repo/issue, so EVERY configured machine qualifies — not just ones
    /// running a particular repo. Same local-first/reachable/name ordering
    /// so the picker (and the single-machine fast path) behaves familiarly.
    pub(crate) fn fleet_machines_for_terminal(&self) -> Vec<MachinePickEntry> {
        let local = self.data.local_machine.clone();
        let mut v: Vec<MachinePickEntry> = self
            .data
            .machines
            .iter()
            .map(|m| MachinePickEntry {
                name: m.name.clone(),
                host: m.host.clone(),
                reachable: m.reachable,
                is_local: m.name == local,
            })
            .collect();
        v.sort_by(|a, b| {
            b.is_local
                .cmp(&a.is_local)
                .then(b.reachable.cmp(&a.reachable))
                .then_with(|| a.name.cmp(&b.name))
        });
        v
    }

    /// Open the "new terminal" machine picker (`n` in the Terminal view).
    /// Skips straight to the name prompt when only one machine is
    /// configured — mirrors `launch_interactive_session_for_selected_issue`'s
    /// single-candidate fast path (`sessions.rs`).
    pub(crate) fn open_new_terminal_picker(&mut self) {
        let machines = self.fleet_machines_for_terminal();
        if machines.is_empty() {
            self.push_toast(
                "New terminal",
                "No fleet machines configured — cannot create a terminal.",
                ToastSeverity::Warning,
            );
            return;
        }
        if machines.len() == 1 {
            self.begin_new_terminal_name_prompt(machines[0].name.clone());
            return;
        }
        self.pending_new_terminal_picker = Some(machines);
    }

    /// A machine has been chosen (picker selection, or the single-machine
    /// fast path) — open the optional-name prompt before creating.
    pub(crate) fn begin_new_terminal_name_prompt(&mut self, machine: String) {
        self.pending_new_terminal = Some(PendingNewTerminal {
            machine,
            buf: String::new(),
        });
    }

    /// Create a terminal on `machine` (`coord terminal new <machine> --name
    /// <slug>`) and attach it in the standalone Terminal pane, exactly as
    /// `reattach_session_by_aid` (`sessions.rs`) drives an EXISTING session:
    /// the local PTY types the command line, `coord` does the ssh+tmux work
    /// on the target machine.
    ///
    /// `name_buf` is the operator's optional typed name (from the
    /// `pending_new_terminal` prompt); an empty/blank buffer auto-generates
    /// a slug. The slug is always resolved CLIENT-SIDE and passed explicitly
    /// via `--name` — never left to the backend's own auto-naming — because
    /// the exact slug is needed up front to chain the `attach` command, and
    /// there is no way to read the `new` subcommand's stdout back out of a
    /// PTY session it was typed into.
    ///
    /// Inserts an optimistic PENDING `FleetTerminal` immediately so the tree
    /// shows the new node without waiting for the next discovery sweep
    /// (mirrors the `"pending-"` `LiveTmuxSession` insert in
    /// `launch_interactive_session_on_machine_inner`); `poll_remote_terminals`
    /// reconciles/evicts it once a real `coord terminal list` result covers
    /// `(machine, slug)`.
    ///
    /// #954 bugs 3 & 4 (post-#955 rebase): the create+attach now runs in the
    /// new leaf's OWN cached fleet session — `fleet_terminal_sessions[(machine,
    /// slug)]` — exactly the map [`ensure_fleet_terminal_attached`] fills for
    /// an EXISTING leaf. The pre-#955 version typed the command into the bare
    /// `terminal_session` scratch shell, but #955 changed the main pane to
    /// render `standalone_pty_session()` (= the selected leaf's cached session)
    /// whenever a leaf is selected — the bare shell is shown ONLY when no leaf
    /// is selected, and `drive_terminal_pane` doesn't even poll it then. So the
    /// old path (a) sent `coord terminal new` into an invisible, unpolled shell
    /// while (b) `ensure_fleet_terminal_attached` independently spawned a
    /// SECOND session that ran a bare `attach` against a not-yet-created tmux
    /// session — a race that dropped the second terminal (bug 3) and left
    /// nothing durably created for restart discovery to find (bug 4). Creating
    /// + attaching in the one keyed session fixes both: it's the pane actually
    /// shown, it's polled, and its presence makes `ensure_fleet_terminal_attached`
    /// a no-op for this key (it early-returns when the key exists), so the
    /// terminal is created and attached exactly once.
    pub(crate) fn create_and_attach_terminal(&mut self, machine: String, name_buf: String) {
        let sanitized = sanitize_terminal_name(&name_buf);
        let slug = if sanitized.is_empty() {
            generate_terminal_slug()
        } else {
            sanitized
        };

        // Drop any stale pending entry for this (machine, slug) — e.g. the
        // operator retried after a failed attempt without an intervening
        // discovery sweep — before adding the fresh one.
        self.fleet_terminals
            .retain(|t| !(t.pending && t.machine == machine && t.name == slug));
        self.fleet_terminals.push(FleetTerminal {
            name: slug.clone(),
            machine: machine.clone(),
            attached: false,
            pending: true,
            pending_sweep_count: 0,
        });
        self.terminal_tree_expanded.insert(machine.clone(), true);
        if let Some(mi) = self.data.machines.iter().position(|m| m.name == machine) {
            let ti = self
                .fleet_terminals_for_machine(&machine)
                .iter()
                .position(|t| t.name == slug)
                .unwrap_or(0);
            self.terminal_tree_selected = Some(vec![mi as u16, ti as u16]);
        }

        // Switch to the standalone Terminal panel; the newly-selected leaf's
        // session (spawned just below) is what its main pane will render.
        self.active_view = SidebarView::Terminal;

        let key = (machine.clone(), slug.clone());
        // Second-create guard (bug 3): if a session is somehow already cached
        // for this exact key, don't spawn a duplicate — the existing one is
        // already attached / attaching.
        if self.fleet_terminal_sessions.contains_key(&key) {
            return;
        }
        // A prior failed attempt for this key must not permanently poison it.
        self.fleet_terminal_spawn_errors.remove(&key);

        // Use the last-rendered pane dims when known (a frame has painted),
        // else a sane default — the session resizes on the next
        // `drive_terminal_pane` tick once real dims are stashed.
        let (cols, rows) = self.terminal_pending_dims.get().unwrap_or((80, 24));
        let cwd = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("/"));
        let shell = quadraui::terminal_engine::default_shell();
        match quadraui::terminal_engine::TerminalSession::spawn(
            cols.max(20),
            rows.max(5),
            &shell,
            &cwd,
            10_000, // 10 000-line scrollback
        ) {
            Ok(mut sess) => {
                let cfg = self
                    .command_runner
                    .config_path
                    .as_ref()
                    .map(|p| format!("--config {} ", shell_quote_arg(&p.to_string_lossy())))
                    .unwrap_or_default();
                let target = format!("{machine}:{slug}");
                // Create THEN attach in one chained command: `&&` guarantees
                // the durable tmux session exists (bug 4) before we attach, so
                // there's no attach-before-create race. `coord terminal new`
                // does the ssh+tmux work on the target machine; `--name` pins
                // the client-resolved slug so this attach line matches it.
                let cmd = format!(
                    "coord terminal new {}{} --name {} && coord terminal attach {}{}\r",
                    cfg,
                    shell_quote_arg(&machine),
                    shell_quote_arg(&slug),
                    cfg,
                    shell_quote_arg(&target),
                );
                sess.send_str(&cmd);
                let ssh_host = self.resolve_fleet_terminal_ssh_host(&machine);
                self.fleet_terminal_sessions
                    .insert(key, FleetTerminalSession::new(sess, ssh_host, &slug));
            }
            Err(e) => {
                // Spawn failed — record it so the pane shows a readable banner
                // for this leaf instead of a silent blank.
                self.fleet_terminal_spawn_errors.insert(key, e.to_string());
            }
        }
    }
}

/// #954: client-side terminal slug generator. Always distinct enough for a
/// same-second double-`n` — millisecond-resolution suffix, same idiom used
/// elsewhere in this module for unique ids (`SystemTime`/`UNIX_EPOCH`).
fn generate_terminal_slug() -> String {
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("tui-{millis:x}")
}

/// #954: sanitize an operator-typed terminal name into a slug tmux/the
/// shell can swallow unquoted-adjacent — trims whitespace and replaces any
/// character that isn't alphanumeric/`-`/`_` with `-`.
fn sanitize_terminal_name(raw: &str) -> String {
    raw.trim()
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '-'
            }
        })
        .collect()
}
