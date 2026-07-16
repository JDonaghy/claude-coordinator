//! Event dispatch and mouse/PTY handling extracted from `app/render.rs` and `app/mod.rs` (#745).
//!
//! Covers the main `dispatch_handle` event router (body of `ShellApp::handle`),
//! the `action_for_key` keybinding lookup, and all mouse/PTY input methods.
//!
//! **Import pattern:** `use super::*` is intentional — these methods live on `CoordApp`
//! and need the full parent namespace (all quadraui types, app-field types, and bindings
//! from other extracted modules). Pure-function submodules (`format.rs`, `data.rs`) use
//! explicit imports because their dependency surface is small and stable.
#[allow(unused_imports)]
use super::*;

// ─── Event dispatch ───────────────────────────────────────────────────────────

impl CoordApp {

    /// Create a new app.
    /// Check whether the given key+modifiers match a named action in the user's
    /// keybindings table.  Returns the action name when matched.
    pub(crate) fn action_for_key<'a>(&'a self, key: &Key, modifiers: &quadraui::Modifiers) -> Option<&'a str> {
        let key_str = key_to_binding_str(key);
        if key_str.is_empty() {
            return None;
        }
        self.parsed_keybindings
            .iter()
            .find(|(_, binding)| binding.key == key_str && binding.modifiers == *modifiers)
            .map(|(action, _)| action.as_str())
    }

    /// §4 (#782): scroll the currently-focused **content** pane by one line.
    ///
    /// Invoked for a bare `j`/`k`/`Up`/`Down` while `focused_region` is `Main`
    /// or `Detail` — i.e. the operator has moved focus off the sidebar with
    /// `Ctrl-W`.  Before this existed the focus indicator was *cosmetic only*:
    /// the status bar reported `[Main]`/`[Detail]` but keys still drove the
    /// sidebar tree, because the default Board/Pipeline tabs have no dedicated
    /// j/k scroll arm and fell through to the generic sidebar-nav arm (#782
    /// review: "focus has no functional effect on keyboard routing").
    ///
    /// Mutates the exact scroll-offset field the active view's content pane
    /// reads in `render_content`, so the movement is visible:
    /// - **Board** → `detail_scroll` (assignment summary / issue body list).
    /// - **Machines** → `machine_detail_scroll`.
    /// - **Pipeline** default stage view / Stages tab → `pipeline_stage_content_scroll`
    ///   (the field `pipeline_tab_body_list` renders with); every other
    ///   Pipeline tab → `pipeline_detail_scroll`.
    ///
    /// Returns `true` when a field was updated (every list+detail view); the
    /// caller then requests a redraw.  Views without a scrollable content pane
    /// (Terminal, Kanban, Merge Queue, Settings) return `false`.
    pub(crate) fn scroll_focused_content(&mut self, down: bool) -> bool {
        fn step(v: usize, down: bool) -> usize {
            if down {
                v.saturating_add(1)
            } else {
                v.saturating_sub(1)
            }
        }
        match self.active_view {
            SidebarView::Board => {
                self.detail_scroll = step(self.detail_scroll, down);
                true
            }
            SidebarView::Machines => {
                self.machine_detail_scroll = step(self.machine_detail_scroll, down);
                true
            }
            SidebarView::Pipeline => {
                match self.pipeline_detail_tab {
                    // #818: Overview renders `pipeline_tab_body_list`, whose
                    // scroll_offset is `pipeline_stage_content_scroll`.
                    PipelineDetailTab::Overview => {
                        self.pipeline_stage_content_scroll =
                            step(self.pipeline_stage_content_scroll, down);
                    }
                    // Issue / Log / Summary / Terminal bodies read
                    // `pipeline_detail_scroll`.
                    _ => {
                        self.pipeline_detail_scroll = step(self.pipeline_detail_scroll, down);
                    }
                }
                true
            }
            _ => false,
        }
    }

    /// Route a UI event to the appropriate handler. Called by []
    /// in render.rs — the body is extracted here so the ShellApp impl stays thin
    /// and tests can call dispatch_handle directly without going through the trait.
    pub(crate) fn dispatch_handle(
        &mut self,
        event: UiEvent,
        backend: &mut dyn Backend,
        ctx: &ShellContext,
    ) -> Reaction {
        let mut needs_redraw = false;

        // ── Drain pending background data load ──────────────────────────
        if self.apply_pending_data() {
            needs_redraw = true;
        }

        // ── Expire stale toasts ─────────────────────────────────────────
        needs_redraw |= self.run_periodic_work();

        // ── #790: drop a stale terminal copy mode ───────────────────────
        // Copy mode (F9) is only meaningful in a copy-capable terminal
        // context.  If the view changed out from under it, clear the flag so
        // a lingering toggle can't silently swallow the next drag.
        if self.terminal_copy_mode && !self.terminal_copy_mode_available() {
            self.exit_terminal_copy_mode();
        }

        // ── #464: Ctrl-C copies active terminal host-selection ──────────
        // Fires for BOTH terminal views (standalone Terminal and
        // Pipeline/Terminal tab) regardless of whether PTY-passthrough is
        // active. When there is an active host selection, Ctrl-C copies the
        // text and is consumed — it does NOT propagate to the PTY (so no
        // accidental interrupt of the running process). When there is no
        // selection, this block is a no-op and Ctrl-C falls through to the
        // normal PTY-passthrough path below.
        if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
            if matches!(key, Key::Char('c') | Key::Char('C'))
                && modifiers.ctrl
                && !modifiers.alt
            {
                if let Some(text) = self.active_terminal_selected_text() {
                    backend.services().clipboard().write_text(&text);
                    self.clear_active_terminal_selection();
                    // #790: a successful copy ends copy mode (F9) — the gesture
                    // is complete, so restore normal PTY mouse forwarding.
                    self.terminal_copy_mode = false;
                    self.terminal_host_sel_dragging = false;
                    // Platform contract (#464): emit TextCopied so copy-confirmation
                    // UI and future listeners observe terminal copies, matching the
                    // quadraui built-in text-selection copy path (tui/run.rs:258).
                    let _ = self.handle(UiEvent::TextCopied(text), backend, ctx);
                    return Reaction::Redraw;
                }
            }
        }

        // ── #790: F9 toggles terminal copy mode (keyboard, tmux-proof) ──
        // Shift+drag can't reach coord-tui when it runs inside an outer tmux
        // (tmux consumes Shift for its own cross-pane selection), so a
        // key-driven toggle is the only reliable trigger.  Caught BEFORE the
        // terminal-focus PTY-forward blocks below so F9/Esc are not swallowed
        // by a focused PTY.  Scoped to the copy-capable contexts (standalone
        // Terminal view + Pipeline detail Terminal tab).
        if self.terminal_copy_mode_available() {
            if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
                if matches!(key, Key::Named(NamedKey::F(9)))
                    && !modifiers.ctrl
                    && !modifiers.alt
                    && !modifiers.shift
                {
                    if self.terminal_copy_mode {
                        self.exit_terminal_copy_mode();
                    } else {
                        self.terminal_copy_mode = true;
                    }
                    return Reaction::Redraw;
                }
                // Esc leaves copy mode (discarding the selection) without
                // copying — only owned here while copy mode is actually on so
                // Esc keeps its normal meaning otherwise.
                if self.terminal_copy_mode
                    && matches!(key, Key::Named(NamedKey::Escape))
                {
                    self.exit_terminal_copy_mode();
                    return Reaction::Redraw;
                }
            }
        }

        // ── #605: Ctrl-W pane leader (keyboard focus move — no mouse) ────
        // Caught BEFORE the terminal-focus blocks below so the chord is not
        // swallowed by a focused PTY's key-forward.  Skipped while a blocking
        // modal or the fuzzy finder owns the keyboard (they consume keys and
        // a stray leader latch would surprise the user).
        if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
            if !self.any_blocking_modal_active() && self.issue_finder.is_none() {
                if let Some(reaction) = self.handle_ctrl_w_leader(key, modifiers) {
                    return reaction;
                }
            }
        }

        // ── #424: embedded terminal pane focus arbitration ──────────────
        // PROTOCOL:
        //   - When `active_view == SidebarView::Terminal`:
        //     * F12 toggles `terminal_focused` (handled here always,
        //       regardless of current focus state).
        //     * When `terminal_focused == true`, every other KeyPressed
        //       is encoded via `key_to_pty_bytes` and forwarded to the
        //       PTY — TUI chrome (view-switch hotkeys 1/2/3/4/5, etc.)
        //       is INACTIVE in this mode.
        //     * When `terminal_focused == false`, keys flow through to
        //       the normal TUI dispatch (1/2/3/4/5 work).
        //   - When the Terminal view is NOT active, this block is a no-op.
        //
        // Mouse/window events fall through to normal handlers — the
        // Terminal view doesn't need special mouse handling for the MVP
        // (copy-out is now implemented in #464 above).
        //
        // #954 bug 1 (focus-stealing fix): skip the whole PTY-passthrough
        // arbitration while a blocking modal is open (the new-terminal
        // machine picker / name prompt, or any other). Otherwise a
        // PTY-focused Terminal view — the common case, since entering the
        // view auto-focuses the shell (#424/#646) — swallows every key and
        // returns before the modal handlers below ever run, so number-key
        // selection and Esc never reach the picker/name dialog. The modal
        // owns ALL input while open (the codebase-wide one-modal invariant);
        // `any_blocking_modal_active()` already includes both new-terminal
        // dialogs, so this hands keyboard input back to them.
        if self.active_view == SidebarView::Terminal && !self.any_blocking_modal_active() {
            if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
                // F12 = focus-toggle — always handled, independent of
                // current focus state, so the user can escape from the
                // PTY-passthrough mode.
                if matches!(key, Key::Named(NamedKey::F(12)))
                    && !modifiers.ctrl
                    && !modifiers.alt
                    && !modifiers.shift
                {
                    self.terminal_focused = !self.terminal_focused;
                    return Reaction::Redraw;
                }
                if self.terminal_focused {
                    // Forward every other key to the PTY; ignore the
                    // return value (a missing PTY just swallows the key
                    // — the placeholder pane shows why).
                    let _ = self.forward_key_to_pty(key, modifiers);
                    return Reaction::Redraw;
                }
                // #1029 bug B: Esc, once PTY focus has been released,
                // returns the operator to wherever they were before an
                // entry point like milestone chat jumped them into this
                // standalone Terminal view — the issue's explicit
                // "Esc/detach returns focus to the originating
                // Plans/milestone view" promise. Only fires when a return
                // view was actually bookmarked: `launch_milestone_chat_session`
                // is the only site that sets `terminal_return_view` today,
                // so an ordinary ActivityBar-driven visit to Terminal has
                // nothing to return to and Esc falls through unchanged
                // (e.g. to whatever else consumes it, up to quitting).
                if matches!(key, Key::Named(NamedKey::Escape))
                    && !modifiers.ctrl
                    && !modifiers.alt
                    && !modifiers.shift
                {
                    if let Some(origin) = self.terminal_return_view.take() {
                        self.switch_active_view(origin);
                        return Reaction::Redraw;
                    }
                }
            }
            // #468: forward host-clipboard paste to the PTY when focused.
            // Wraps in ESC[200~…ESC[201~ when the PTY has bracketed-paste
            // mode enabled; otherwise sends raw bytes.  No trailing \r —
            // let the human press Enter.
            if self.terminal_focused {
                if let UiEvent::ClipboardPaste(text) = &event {
                    let _ = self.forward_paste_to_pty(text);
                    return Reaction::Redraw;
                }
            }
        }

        // ── Test → Review confirm (Test precedes Review) ─────────────────
        // When the smoke test passes (board-driven, never scraped) the
        // detector raises `pending_auto_review`.  Own Enter (confirm →
        // launch the interactive review) and Esc/n (dismiss) here, BEFORE
        // the detail-terminal focus block — otherwise a still-focused shell
        // (the one that ran `coord assign`) would eat the Enter.  Other keys
        // fall through so the shell stays usable if the operator ignores it.
        if self.pending_auto_review.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        self.confirm_auto_review();
                        return Reaction::Redraw;
                    }
                    Key::Named(NamedKey::Escape) | Key::Char('n') | Key::Char('N') => {
                        // #722: when the blocking dialog is showing (live session
                        // is still running), Esc must NOT destroy the pending
                        // offer — the operator is expected to reattach and /exit
                        // first, at which point the offer re-fires automatically.
                        if let Some(ref p) = self.pending_auto_review {
                            if self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo) {
                                let n = p.issue_num;
                                self.push_toast(
                                    "Reattach first",
                                    &format!(
                                        "Close the live session for #{n} first; \
                                         the review offer will re-appear automatically.",
                                    ),
                                    ToastSeverity::Warning,
                                );
                                return Reaction::Redraw;
                            }
                        }
                        self.pending_auto_review = None;
                        self.push_toast(
                            "Review deferred",
                            "Start it any time from the row's right-click menu.",
                            ToastSeverity::Info,
                        );
                        return Reaction::Redraw;
                    }
                    _ => {}
                }
            }
        }

        // ── Post-review one-key stage offer (Fix / Test) confirm ─────────
        // Own Enter (→ launch the next stage) and Esc/n (defer).  No text
        // input, so other keys fall through and the shell stays usable.
        if self.pending_stage_launch.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        self.confirm_stage_launch();
                        return Reaction::Redraw;
                    }
                    Key::Named(NamedKey::Escape) | Key::Char('n') | Key::Char('N') => {
                        // #722: preserve the offer when the blocking dialog is
                        // showing — same guard as pending_auto_review above.
                        if let Some(ref p) = self.pending_stage_launch {
                            if self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo) {
                                let n = p.issue_num;
                                self.push_toast(
                                    "Reattach first",
                                    &format!(
                                        "Close the live session for #{n} first; \
                                         the stage offer will re-appear automatically.",
                                    ),
                                    ToastSeverity::Warning,
                                );
                                return Reaction::Redraw;
                            }
                        }
                        self.pending_stage_launch = None;
                        self.push_toast(
                            "Deferred",
                            "Start the next stage any time from the row's right-click menu.",
                            ToastSeverity::Info,
                        );
                        return Reaction::Redraw;
                    }
                    _ => {}
                }
            }
        }

        // ── Leg 3 (#517 / #587): rework (request-changes) confirm ───────────
        // #587: the rework dialog now owns a findings text input, so ALL key
        // events are consumed here (same discipline as `pending_test_fail`):
        //   Enter  → validate findings non-empty → confirm (saves findings +
        //             launches fix) or toast warning (keeps dialog open).
        //   Escape → cancel and defer.
        //   Backspace → edit the findings buffer.
        //   Char   → append to the findings buffer.
        // The `n`/`N` shortcut is intentionally removed: those characters
        // should type into the findings buffer, not dismiss the dialog.
        if self.pending_rework.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        self.confirm_rework();
                        return Reaction::Redraw;
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_rework = None;
                        self.push_toast(
                            "Fix deferred",
                            "Start it any time from the row's right-click menu.",
                            ToastSeverity::Info,
                        );
                        return Reaction::Redraw;
                    }
                    Key::Named(NamedKey::Backspace) => {
                        if let Some(ref mut p) = self.pending_rework {
                            p.findings.pop();
                        }
                        return Reaction::Redraw;
                    }
                    Key::Char(ch) => {
                        if let Some(ref mut p) = self.pending_rework {
                            p.findings.push(*ch);
                        }
                        return Reaction::Redraw;
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── Leg 3c / A3 (#517, #581): test failed → start fix confirm ────
        // Same intercept discipline: own Enter (→ launch interactive --fix-of
        // briefed with the failure) and Esc/n (dismiss).
        if self.pending_test_fix.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        self.confirm_test_fix();
                        return Reaction::Redraw;
                    }
                    Key::Named(NamedKey::Escape) | Key::Char('n') | Key::Char('N') => {
                        // #722: preserve the offer when the blocking dialog is showing.
                        if let Some(ref p) = self.pending_test_fix {
                            if self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo) {
                                let n = p.issue_num;
                                self.push_toast(
                                    "Reattach first",
                                    &format!(
                                        "Close the live session for #{n} first; \
                                         the fix offer will re-appear automatically.",
                                    ),
                                    ToastSeverity::Warning,
                                );
                                return Reaction::Redraw;
                            }
                        }
                        self.pending_test_fix = None;
                        self.push_toast(
                            "Fix deferred",
                            "Start it any time from the row's right-click menu.",
                            ToastSeverity::Info,
                        );
                        return Reaction::Redraw;
                    }
                    _ => {}
                }
            }
        }

        // ── Leg 3c (#517, #306): test passed → start merge agent confirm ─
        // Own Enter (→ launch interactive --merge-of) and Esc/n (dismiss).
        if self.pending_merge.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        self.confirm_merge();
                        return Reaction::Redraw;
                    }
                    Key::Named(NamedKey::Escape) | Key::Char('n') | Key::Char('N') => {
                        // #722: preserve the offer when the blocking dialog is showing.
                        if let Some(ref p) = self.pending_merge {
                            if self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo) {
                                let n = p.issue_num;
                                self.push_toast(
                                    "Reattach first",
                                    &format!(
                                        "Close the live session for #{n} first; \
                                         the merge offer will re-appear automatically.",
                                    ),
                                    ToastSeverity::Warning,
                                );
                                return Reaction::Redraw;
                            }
                        }
                        self.pending_merge = None;
                        self.push_toast(
                            "Merge deferred",
                            "Start it any time from the row's right-click menu.",
                            ToastSeverity::Info,
                        );
                        return Reaction::Redraw;
                    }
                    _ => {}
                }
            }
        }

        // ── #863: iteration cap reached → force-past-cap confirm ─────────
        // Own Enter (→ re-dispatch the same Fix with --force) and Esc/n (dismiss).
        if self.pending_fix_force_confirm.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        self.confirm_fix_force_past_cap();
                        return Reaction::Redraw;
                    }
                    Key::Named(NamedKey::Escape) | Key::Char('n') | Key::Char('N') => {
                        // #722: preserve the offer when the blocking dialog is showing.
                        if let Some(ref p) = self.pending_fix_force_confirm {
                            if self.issue_has_live_session_for_repo(p.issue_num, &p.coord_repo) {
                                let n = p.issue_num;
                                self.push_toast(
                                    "Reattach first",
                                    &format!(
                                        "Close the live session for #{n} first; \
                                         the force-fix offer will re-appear automatically.",
                                    ),
                                    ToastSeverity::Warning,
                                );
                                return Reaction::Redraw;
                            }
                        }
                        self.pending_fix_force_confirm = None;
                        self.push_toast(
                            "Not forcing",
                            "Resolve manually, or bump pipeline.max_review_iterations \
                             in coordinator.yml.",
                            ToastSeverity::Info,
                        );
                        return Reaction::Redraw;
                    }
                    _ => {}
                }
            }
        }

        // ── #440/#675: Pipeline/Board detail Terminal tab focus arbitration ──
        // PROTOCOL (mirrors the standalone Terminal pane):
        //   - When `active_view == Pipeline && pipeline_detail_tab == Terminal`
        //     OR `active_view == Board && board_detail_tab == Terminal` (#675):
        //     * F12 toggles `detail_terminal_focused`.
        //     * When focused, every other keypress is forwarded to the
        //       selected issue's PTY via `key_to_pty_bytes`.  TUI
        //       chrome (tab switching, view nav) is INACTIVE.
        //     * When unfocused, keys flow through to normal dispatch.
        //   - Outside this condition this block is a no-op.
        //
        // #467 addition (PTY released only; supersedes the ssh+tmux
        // launcher built for #446):
        //   * `s` launches a local human-attended `claude` session via
        //     `coord assign --interactive --repo <repo> <N>` and
        //     auto-focuses the PTY.  Only available on the Pipeline panel
        //     (not the Board Terminal — #675 scopes the Board Terminal to Chat).
        let in_pipeline_terminal = self.active_view == SidebarView::Pipeline
            && self.pipeline_detail_tab == PipelineDetailTab::Terminal;
        let in_board_terminal = self.active_view == SidebarView::Board
            && self.board_detail_tab == BoardDetailTab::Terminal;
        // #954 bug 1: as with the standalone Terminal pane above, a blocking
        // modal (e.g. a machine picker) must own ALL input — don't let a
        // focused detail PTY swallow keys meant for the dialog.
        if (in_pipeline_terminal || in_board_terminal) && !self.any_blocking_modal_active() {
            if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
                if matches!(key, Key::Named(NamedKey::F(12)))
                    && !modifiers.ctrl
                    && !modifiers.alt
                    && !modifiers.shift
                {
                    self.detail_terminal_focused = !self.detail_terminal_focused;
                    return Reaction::Redraw;
                }
                if self.detail_terminal_focused {
                    let _ = self.forward_key_to_detail_terminal(key, modifiers);
                    return Reaction::Redraw;
                }
                // ── #467: `s` = launch local `coord assign --interactive` ──
                // Only fires when the PTY is *released* (not focused), so the
                // letter 's' still reaches the live shell when in PTY mode.
                // Scoped to Pipeline Terminal only (#675).
                if in_pipeline_terminal
                    && matches!(key, Key::Char('s'))
                    && !modifiers.ctrl
                    && !modifiers.alt
                    && !modifiers.shift
                {
                    self.launch_interactive_session_for_selected_issue(InteractiveLaunchMode::Work);
                    return Reaction::Redraw;
                }
            }
            // #468: forward host-clipboard paste to the per-issue PTY.
            if self.detail_terminal_focused {
                if let UiEvent::ClipboardPaste(text) = &event {
                    let _ = self.forward_paste_to_detail_terminal(text);
                    return Reaction::Redraw;
                }
            }
        }

        // ── #541: global issue fuzzy-finder — Ctrl+P toggle ────────────────
        // Open / close the Telescope-style overlay with Ctrl+P from any view,
        // unless:
        //   (a) a PTY is capturing all keystrokes, or
        //   (b) any other modal dialog is currently holding focus.
        // The existing codebase invariant is "one modal owns ALL input while
        // open"; opening the finder on top of another modal breaks that
        // contract and leaves the user unable to reach the modal underneath.
        if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
            let pty_active = (self.active_view == SidebarView::Terminal
                && self.terminal_focused)
                || (self.active_view == SidebarView::Pipeline
                    && self.pipeline_detail_tab == PipelineDetailTab::Terminal
                    && self.detail_terminal_focused)
                || (self.active_view == SidebarView::Board // #675
                    && self.board_detail_tab == BoardDetailTab::Terminal
                    && self.detail_terminal_focused);
            if matches!(key, Key::Char('p') | Key::Char('P'))
                && modifiers.ctrl
                && !modifiers.alt
                && !pty_active
                && !self.any_blocking_modal_active()
            {
                if self.issue_finder.is_none() {
                    self.issue_finder = Some(IssueFinder::default());
                } else {
                    self.issue_finder = None;
                }
                return Reaction::Redraw;
            }
        }

        // ── #541: issue finder owns ALL input while open ─────────────────────
        // Esc=close, Enter=jump, j/k/↑/↓=navigate, Backspace=delete,
        // printable chars=type.  Every other key is swallowed so board/pipeline
        // shortcuts can't fire while the overlay is up.
        if self.issue_finder.is_some() {
            if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
                match key {
                    Key::Named(NamedKey::Escape) => {
                        self.issue_finder = None;
                    }
                    Key::Named(NamedKey::Enter) => {
                        self.confirm_issue_finder();
                    }
                    Key::Named(NamedKey::Down) | Key::Char('j')
                        if !modifiers.ctrl && !modifiers.alt =>
                    {
                        let count = {
                            let q = self
                                .issue_finder
                                .as_ref()
                                .map(|f| f.query.clone())
                                .unwrap_or_default();
                            self.finder_matches(&q).len()
                        };
                        if let Some(f) = &mut self.issue_finder {
                            f.move_down(count);
                        }
                    }
                    Key::Named(NamedKey::Up) | Key::Char('k')
                        if !modifiers.ctrl && !modifiers.alt =>
                    {
                        if let Some(f) = &mut self.issue_finder {
                            f.move_up();
                        }
                    }
                    Key::Named(NamedKey::Backspace) => {
                        if let Some(f) = &mut self.issue_finder {
                            f.backspace();
                        }
                    }
                    Key::Char(ch) if !modifiers.ctrl && !modifiers.alt => {
                        if let Some(f) = &mut self.issue_finder {
                            f.insert_char(*ch);
                        }
                    }
                    _ => {}
                }
            }
            return Reaction::Redraw;
        }

        // ── #1033: `L` opens/focuses the Sessions panel ───────────────────────
        // Retired the #628 fleet-wide live-sessions overlay in favor of the
        // always-visible Sessions panel (#1032) — `L` now just switches/
        // focuses that view instead of toggling a modal, so existing
        // muscle-memory still works. Guarded the same way the old overlay
        // toggle was (no PTY focus, no blocking modal, no issue finder) so
        // `L` doesn't steal a literal 'L' keystroke typed into a chat/terminal.
        if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
            let pty_active = (self.active_view == SidebarView::Terminal
                && self.terminal_focused)
                || (self.active_view == SidebarView::Pipeline
                    && self.pipeline_detail_tab == PipelineDetailTab::Terminal
                    && self.detail_terminal_focused);
            if matches!(key, Key::Char('L'))
                && !modifiers.ctrl
                && !modifiers.alt
                && !pty_active
                && self.issue_finder.is_none()
                && !self.any_blocking_modal_active()
            {
                self.switch_active_view(SidebarView::Sessions);
                return Reaction::Redraw;
            }
        }

        // ── #316 Phase B: file-issue modal owns ALL input while open ───
        // Esc cancels; Ctrl+Y submits via `gh issue create`.
        if self.file_issue_modal.is_some() {
            if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
                match key {
                    Key::Named(NamedKey::Escape) => {
                        if self
                            .file_issue_modal
                            .as_ref()
                            .map(|m| !m.submitting)
                            .unwrap_or(false)
                        {
                            self.file_issue_modal = None;
                        }
                    }
                    Key::Char('y') | Key::Char('Y') if modifiers.ctrl && !modifiers.alt => {
                        self.submit_file_issue();
                    }
                    _ => {}
                }
            }
            return Reaction::Redraw;
        }

        // ── #319 Phase A: refinement-notes review modal owns ALL input ──
        // When the modal is up the user is reviewing/editing the proposed
        // comment; chat input, view nav, and shortcuts are all locked out
        // so a stray keypress can't fire some background action or stuff
        // chars into the chat instead of the modal.
        if self.refinement_notes_modal.is_some() {
            if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
                self.handle_refinement_notes_modal_key(key, modifiers);
            }
            return Reaction::Redraw;
        }

        // ── #410: refinement-chat close dialog — Cancel/Save/Send.
        // Esc=Cancel (discard), S=Save (notes+ready), D=Send (notes+ready+dispatch).
        // The chat overlay stays rendered behind the dialog.
        if self.pending_refinement_close_prompt.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Escape) => {
                        // Cancel — discard, issue unchanged, just stop the worker.
                        self.pending_refinement_close_prompt = None;
                        self.cancel_refinement_chat();
                    }
                    Key::Char('s') | Key::Char('S') => {
                        // Save — draft notes + mark ready, stay on Board.
                        self.pending_refinement_close_prompt = None;
                        self.finalise_after_notes_post = true;
                        self.trigger_refinement_notes_synth();
                        if self.pending_refinement_notes_synth.is_none() {
                            self.finalise_after_notes_post = false;
                        }
                    }
                    Key::Char('d') | Key::Char('D') => {
                        // Send — draft notes + mark ready + dispatch to pipeline.
                        self.pending_refinement_close_prompt = None;
                        self.finalise_after_notes_post = true;
                        self.refine_then_dispatch = true;
                        self.trigger_refinement_notes_synth();
                        if self.pending_refinement_notes_synth.is_none() {
                            self.finalise_after_notes_post = false;
                            self.refine_then_dispatch = false;
                        }
                    }
                    _ => {}
                }
            }
            return Reaction::Redraw;
        }

        // ── Inject chat overlay — intercepts events when open ──────────────
        // Two routing modes:
        //   - **Worker-guidance** (chat opened with `b` over a watch overlay)
        //     keeps the original modal behaviour: chat captures ALL events
        //     until closed.  This matches its short-burst usage.
        //   - **Refinement** (#264, opened via Refine with chat) routes
        //     events to the chat only when the user is actively in a chat
        //     context (Board > Chat tab or worker-guidance modal).
        //     #818: the Pipeline Refinement tab is removed; refinement chats
        //     are no longer routed here.
        if self.inject_chat.is_some() {
            let chat_is_refinement = self.chat_is_refinement();
            let chat_is_board = self.chat_is_board_chat();
            // Three routing modes:
            //   - **Worker-guidance**: modal — captures ALL events.
            //   - **Refinement** (#264/#818): Refinement tab removed; the
            //     refinement chat is no longer shown inline in the Pipeline
            //     view so events are not routed to it.
            //   - **Board chat** (#316): routed only on Board > Chat tab.
            let route_to_chat = if chat_is_refinement {
                // #818: Refinement tab removed — no longer route events.
                false
            } else if chat_is_board {
                self.active_view == SidebarView::Board
                    && self.board_detail_tab == BoardDetailTab::Chat
                    && self.file_issue_modal.is_none() // modal intercepts first
            } else {
                true
            };
            if route_to_chat {
                let main_rect = if chat_is_refinement {
                    // Match the Refinement tab's content_rect so handle()'s
                    // layout maths agree with what's painted.
                    // `#464`: rounded helper so TUI render and hit-test agree.
                    let m = ctx.main_bounds();
                    let lh = backend.line_height();
                    let tab_h = detail_tab_bar_height(lh);
                    // Account for the panel toolbar carve-out done in
                    // render_content (#272).  When panel_toolbar() returns
                    // None we just shave the tab bar.
                    let after_panel = if self.panel_toolbar().is_some() {
                        Rect::new(
                            m.x,
                            m.y + self.toolbar_height(lh),
                            m.width,
                            (m.height - self.toolbar_height(lh)).max(0.0),
                        )
                    } else {
                        m
                    };
                    Rect::new(
                        after_panel.x,
                        after_panel.y + tab_h,
                        after_panel.width,
                        (after_panel.height - tab_h).max(0.0),
                    )
                } else if chat_is_board {
                    // Match the Board Chat tab's content_rect.
                    // `#464`: rounded helper so TUI render and hit-test agree.
                    let m = ctx.main_bounds();
                    let lh = backend.line_height();
                    let tab_h = detail_tab_bar_height(lh);
                    Rect::new(m.x, m.y + tab_h, m.width, (m.height - tab_h).max(0.0))
                } else {
                    ctx.main_bounds()
                };
                // #316 Phase B: `Ctrl+F` in a board new-issue-chat opens the
                // file-issue modal.  Intercept before ChatController so the
                // chat input doesn't see Ctrl+F as a literal character.
                // Previously matched bare `f`/`F` (#366: fixed to require Ctrl
                // so literal 'f' can be typed in the chat input).
                // #1017: gate on the actual new-issue-chat type, not just
                // `chat_is_board` — milestone chats also render in the Board
                // Chat tab now, and Ctrl+F must stay a literal char there
                // (there is no draft issue to file from a milestone chat).
                let chat_is_new_issue = self
                    .focused_watch_state()
                    .map(|w| w.assignment_type == "new-issue-chat")
                    .unwrap_or(false);
                if chat_is_board && chat_is_new_issue {
                    if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
                        if matches!(key, Key::Char('f') | Key::Char('F'))
                            && modifiers.ctrl
                            && !modifiers.alt
                            && !modifiers.cmd
                        {
                            self.open_file_issue_modal();
                            return Reaction::Redraw;
                        }
                    }
                }
                // #319 Phase A: Ctrl+N triggers the refinement-notes
                // finaliser.  Intercept BEFORE forwarding to ChatController
                // so the chat input doesn't see Ctrl+N as a literal char.
                if chat_is_refinement {
                    if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
                        if matches!(key, Key::Char('n') | Key::Char('N'))
                            && modifiers.ctrl
                            && !modifiers.alt
                            && !modifiers.cmd
                        {
                            self.trigger_refinement_notes_synth();
                            return Reaction::Redraw;
                        }
                    }
                }
                let result = self
                    .inject_chat
                    .as_mut()
                    .unwrap()
                    .handle(&event, backend, main_rect);
                match result {
                    ChatControllerEvent::Submit { text } => {
                        self.submit_inject(text);
                    }
                    ChatControllerEvent::Cancelled => {
                        // Worker-guidance chat: Esc just closes the modal
                        // (worker keeps running because the user is
                        // mid-task).
                        //
                        // #410: Refinement chat: Esc always shows the
                        // Cancel/Save/Send dialog so the user can choose
                        // to discard, save notes+mark-ready, or save+dispatch.
                        //
                        // #316 Board chat: Esc just closes the overlay;
                        // no status label to flip since there's no issue.
                        if chat_is_refinement {
                            let issue_n = self
                                .focused_watch_state()
                                .map(|w| w.issue_number)
                                .unwrap_or(0);
                            if issue_n != 0 {
                                self.pending_refinement_close_prompt =
                                    Some(PendingRefinementClosePrompt {
                                        issue_number: issue_n,
                                    });
                                // Leave inject_chat open — the dialog renders
                                // on top of it so the user can read the
                                // transcript while deciding.
                                return Reaction::Redraw;
                            }
                            // No issue context (shouldn't happen in practice) —
                            // fall back to immediate cancel.
                            self.cancel_refinement_chat();
                        }
                        // Board chats: stop the worker, clear chat.
                        if chat_is_board {
                            if let Some(id) = self.watch_focused.clone() {
                                self.command_runner.spawn_queued(&["stop", &id]);
                            }
                            self.watch_focused = None;
                        }
                        // #386: Log-tab steer overlay: the `i` handler temporarily
                        // set `watch_focused` so submit_inject could reach the
                        // assignment.  Restore the invariant (`watch_focused` is
                        // None on the Log tab) so j/k scroll falls through to the
                        // Log-tab arms (#308) and `i` is not blocked by its guard.
                        if self.inject_opened_from_log_tab {
                            self.watch_focused = None;
                            self.inject_opened_from_log_tab = false;
                        }
                        self.inject_chat = None;
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
            // Refinement chat is open but the user is on another tab —
            // fall through so normal navigation works (h/l/1-4, mouse on
            // sidebar etc.).
        }

        // ── Mouse / scroll dispatch (before consuming the event) ─────────────
        needs_redraw |= self.handle_mouse(&event, backend, ctx);
        // A mouse click on the force-quit dialog's confirm button sets this
        // (the click path can't return Reaction::Exit itself).
        if self.quit_requested {
            return Reaction::Exit;
        }

        // ── Force-quit confirmation (interactive session live) ─────────────────
        // Shown when Esc/q is pressed with a live Terminal-tab session.  q / Q /
        // y / Enter confirms (the session keeps running in tmux); Esc / n
        // cancels.  Intercepts all keys so nothing leaks to the chrome beneath.
        if self.pending_quit_confirm {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char('q') | Key::Char('Q') | Key::Char('y') | Key::Char('Y')
                    | Key::Named(NamedKey::Enter) => {
                        return Reaction::Exit;
                    }
                    Key::Named(NamedKey::Escape) | Key::Char('n') | Key::Char('N') => {
                        self.pending_quit_confirm = false;
                        *self.dialog_layout.borrow_mut() = None;
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── Pre-compute panel bounds for keyboard visible-row estimates ───────
        let list_b = ctx.sidebar_bounds().unwrap_or(ctx.main_bounds());
        let lh = backend.line_height();

        // ── #685: Test-mode choice dialog ─────────────────────────────────────
        // 1/Enter = default (smoke or existing mode), 2 = the other option, Esc = cancel.
        if self.pending_test_mode_choice.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                let chosen: Option<&str> = match key {
                    Key::Named(NamedKey::Enter) => {
                        // Enter confirms the default (pre-selected) option.
                        let is_smoke_default = self
                            .pending_test_mode_choice
                            .as_ref()
                            .map(|p| p.current_mode.as_deref().map(|m| m != "auto").unwrap_or(true))
                            .unwrap_or(true);
                        if is_smoke_default { Some("smoke") } else { Some("auto") }
                    }
                    Key::Char('1') => Some("smoke"),
                    Key::Char('2') => Some("auto"),
                    Key::Named(NamedKey::Escape) => {
                        self.pending_test_mode_choice = None;
                        *self.dialog_layout.borrow_mut() = None;
                        return Reaction::Redraw;
                    }
                    _ => None,
                };
                if let Some(mode) = chosen {
                    if let Some(choice) = self.pending_test_mode_choice.take() {
                        self.confirm_test_mode_choice(choice, mode);
                    }
                    *self.dialog_layout.borrow_mut() = None;
                }
                return Reaction::Redraw;
            }
        }

        // ── #486 Leg 4: Pending fleet-machine picker ───────────────────────────
        // Armed for a remote Review/Fix launch when >1 machine can run the repo.
        // Numeric keys (1, 2, …) pick the machine and launch; Esc cancels.
        if self.pending_machine_picker.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char(ch) if ch.is_ascii_digit() && *ch != '0' => {
                        let digit = (*ch as u32 - '1' as u32) as usize;
                        if let Some(picker) = self.pending_machine_picker.as_ref() {
                            if digit < picker.machines.len() {
                                let mode = picker.mode;
                                let machine = picker.machines[digit].name.clone();
                                self.pending_machine_picker = None;
                                self.launch_interactive_session_on_machine(mode, machine, None, false);
                                return Reaction::Redraw;
                            }
                        }
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_machine_picker = None;
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── #954: pending "new terminal" machine picker ──────────────────────
        // Armed by `open_new_terminal_picker` (`n` in the Terminal view) when
        // >1 fleet machine is configured. Numeric keys (1, 2, …) pick the
        // machine and open the optional-name prompt; Esc cancels.
        if self.pending_new_terminal_picker.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char(ch) if ch.is_ascii_digit() && *ch != '0' => {
                        let digit = (*ch as u32 - '1' as u32) as usize;
                        if let Some(machines) = self.pending_new_terminal_picker.as_ref() {
                            if digit < machines.len() {
                                let machine = machines[digit].name.clone();
                                self.pending_new_terminal_picker = None;
                                self.begin_new_terminal_name_prompt(machine);
                                return Reaction::Redraw;
                            }
                        }
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_new_terminal_picker = None;
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── #954: pending "new terminal" optional name input ─────────────────
        // Armed by `begin_new_terminal_name_prompt` once a machine is chosen
        // (picker selection, or the single-machine fast path). Enter creates
        // + attaches via `create_and_attach_terminal` (empty buffer ⇒
        // auto-generated slug). Esc cancels.
        if self.pending_new_terminal.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        if let Some(input) = self.pending_new_terminal.take() {
                            self.create_and_attach_terminal(input.machine, input.buf);
                        }
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_new_terminal = None;
                    }
                    Key::Named(NamedKey::Backspace) => {
                        if let Some(ref mut input) = self.pending_new_terminal {
                            input.buf.pop();
                        }
                    }
                    Key::Char(ch) => {
                        if let Some(ref mut input) = self.pending_new_terminal {
                            input.buf.push(*ch);
                        }
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── #353: Pending repo picker for [Add] button ─────────────────────────
        // When multiple repos exist, this shows a numeric picker (1, 2, …).
        // Numeric keys select a repo, Enter dispatches, Esc cancels.
        if self.pending_repo_picker.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char(ch) if ch.is_ascii_digit() && *ch != '0' => {
                        let digit = (*ch as u32 - '1' as u32) as usize;
                        if let Some(ref mut picker) = self.pending_repo_picker {
                            if digit < picker.repos.len() {
                                let repo = picker.repos[digit].clone();
                                self.pending_repo_picker = None;
                                self.dispatch_board_chat_new_issue(&repo);
                                return Reaction::Redraw;
                            }
                        }
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_repo_picker = None;
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── #200 Pending test-fail reason: intercept all keys until submit ────
        // Enter submits and records test_state=failed. Esc cancels. Backspace
        // edits. Any printable char appends.
        if self.pending_test_fail.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        let reason = self
                            .pending_test_fail
                            .as_ref()
                            .map(|(_, b)| b.trim().to_string())
                            .unwrap_or_default();
                        let reason_opt = if reason.is_empty() {
                            None
                        } else {
                            Some(reason.as_str())
                        };
                        self.record_test_verdict("failed", reason_opt);
                        self.pending_test_fail = None;
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_test_fail = None;
                    }
                    Key::Named(NamedKey::Backspace) => {
                        if let Some((_, ref mut buf)) = self.pending_test_fail {
                            buf.pop();
                        }
                    }
                    Key::Char(ch) => {
                        if let Some((_, ref mut buf)) = self.pending_test_fail {
                            buf.push(*ch);
                        }
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── #296 Pending "report & dispatch fix" input: intercept all keys ───
        // `r` in Pipeline/Test-gate-actionable opens this buffer.
        // Enter records test_state=failed AND dispatches `coord fix`.
        // Esc cancels without recording anything.
        if self.pending_report_fix.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        let description = self.pending_report_fix.take().unwrap_or_default();
                        let description = description.trim().to_string();
                        let reason_opt = if description.is_empty() {
                            None
                        } else {
                            Some(description.as_str())
                        };
                        // Record the failure verdict first.
                        if self.record_test_verdict("failed", reason_opt) {
                            // Then dispatch a fix worker via `coord fix`.
                            if let Some(work_id) = self.pipeline_selected_work_id() {
                                let args: Vec<String> = if description.is_empty() {
                                    vec!["fix".to_string(), work_id.clone()]
                                } else {
                                    vec![
                                        "fix".to_string(),
                                        work_id.clone(),
                                        "--guidance".to_string(),
                                        description.clone(),
                                    ]
                                };
                                let args_ref: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
                                let issue_num = self
                                    .pipeline_sel
                                    .and_then(|i| self.pipeline_issues.get(i))
                                    .map(|iss| iss.number)
                                    .unwrap_or(0);
                                use crate::commands::SpawnQueuedOutcome;
                                match self.command_runner.spawn_queued(&args_ref) {
                                    SpawnQueuedOutcome::Deduped => {}
                                    SpawnQueuedOutcome::Queued => {
                                        self.push_toast(
                                            "Fix worker queued",
                                            &format!("Fix worker queued for #{} — will dispatch after current command.", issue_num),
                                            ToastSeverity::Info,
                                        );
                                    }
                                    SpawnQueuedOutcome::Started => {
                                        self.push_toast(
                                            "Fix worker dispatched",
                                            &format!("Fix worker dispatched for #{}", issue_num),
                                            ToastSeverity::Info,
                                        );
                                    }
                                }
                            }
                        }
                        self.pending_report_fix = None;
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_report_fix = None;
                    }
                    Key::Named(NamedKey::Backspace) => {
                        if let Some(ref mut buf) = self.pending_report_fix {
                            buf.pop();
                        }
                    }
                    Key::Char(ch) => {
                        if let Some(ref mut buf) = self.pending_report_fix {
                            buf.push(*ch);
                        }
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── #977 Pending "fast plan capture" title input: intercept all keys ─
        // `c` in the Plans panel opens this buffer. Enter dispatches `coord
        // milestone capture <repo> --title <buf>` via `capture_plan_stub`.
        // Esc cancels without creating anything.
        if self.pending_plan_capture.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        let title = self.pending_plan_capture.take().unwrap_or_default();
                        self.capture_plan_stub(title);
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_plan_capture = None;
                    }
                    Key::Named(NamedKey::Backspace) => {
                        if let Some(ref mut buf) = self.pending_plan_capture {
                            buf.pop();
                        }
                    }
                    Key::Char(ch) => {
                        if let Some(ref mut buf) = self.pending_plan_capture {
                            buf.push(*ch);
                        }
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── #1017 Pending "New milestone via chat…" title input: intercept
        // all keys ───────────────────────────────────────────────────────
        // Bare `C` in the Plans panel opens this buffer (sibling to #977's
        // `c` capture, above). Enter dispatches `coord milestone chat
        // <repo> --new [--title <buf>]` via `capture_plan_chat` — an empty
        // buffer is a valid submission here (the operator can leave the
        // title for the chat to work out). Esc cancels without dispatching
        // anything.
        if self.pending_new_milestone_chat.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        let title = self.pending_new_milestone_chat.take().unwrap_or_default();
                        self.capture_plan_chat(title);
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_new_milestone_chat = None;
                    }
                    Key::Named(NamedKey::Backspace) => {
                        if let Some(ref mut buf) = self.pending_new_milestone_chat {
                            buf.pop();
                        }
                    }
                    Key::Char(ch) => {
                        if let Some(ref mut buf) = self.pending_new_milestone_chat {
                            buf.push(*ch);
                        }
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── #1003 Pending Plans-row single-field input: intercept all keys ───
        // Set by "Edit milestone…" / "Add issue to milestone…" / "Remove
        // issue from milestone…" (Plans-panel / MilestoneDag row context
        // menu). Enter submits via `submit_milestone_row_input`. Esc cancels.
        if self.pending_milestone_row_input.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Enter) => {
                        if let Some(input) = self.pending_milestone_row_input.take() {
                            self.submit_milestone_row_input(input);
                        }
                    }
                    Key::Named(NamedKey::Escape) => {
                        self.pending_milestone_row_input = None;
                    }
                    Key::Named(NamedKey::Backspace) => {
                        if let Some(ref mut input) = self.pending_milestone_row_input {
                            input.buf.pop();
                        }
                    }
                    Key::Char(ch) => {
                        if let Some(ref mut input) = self.pending_milestone_row_input {
                            input.buf.push(*ch);
                        }
                    }
                    _ => {}
                }
                return Reaction::Redraw;
            }
        }

        // ── #1003 Pending "Close / archive plan" confirmation: intercept ALL
        // key presses ─────────────────────────────────────────────────────
        // Set by the Plans-panel / MilestoneDag row context menu's "Close /
        // archive plan" item. 'y'/'Y' confirms (`coord issue close`); every
        // other key cancels. Mirrors `pending_restart`.
        if self.pending_close_plan.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char('y') | Key::Char('Y') => {
                        if let Some(plan) = self.pending_close_plan.take() {
                            self.confirm_close_plan(plan);
                        }
                    }
                    _ => {
                        self.pending_close_plan = None;
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── Pending restart confirmation: intercept ALL key presses ──────────
        // While a restart is pending, 'y'/'Y' fires the restart; every other
        // key cancels.  We return early so normal key dispatch never fires.
        if self.pending_restart.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char('y') | Key::Char('Y') => {
                        if let Some(name) = self.pending_restart.take() {
                            use crate::commands::SpawnQueuedOutcome;
                            if let SpawnQueuedOutcome::Queued = self.command_runner.spawn_queued(&[
                                "agent",
                                "restart",
                                "--machine",
                                &name,
                            ]) {
                                self.push_toast(
                                    "⏳ Queued",
                                    "agent restart runs after current command",
                                    ToastSeverity::Info,
                                );
                            }
                        }
                    }
                    _ => {
                        self.pending_restart = None;
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── #956: Pending kill-terminal confirmation: intercept ALL key
        // presses ─────────────────────────────────────────────────────────
        // While a kill is pending, 'y'/'Y' fires it; every other key
        // cancels.  Mirrors `pending_restart` immediately above.
        if self.pending_kill_terminal.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char('y') | Key::Char('Y') => {
                        if let Some(p) = self.pending_kill_terminal.take() {
                            self.confirm_kill_terminal(p);
                        }
                    }
                    _ => {
                        self.pending_kill_terminal = None;
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── #1033: Pending kill-session confirmation: intercept ALL key
        // presses ─────────────────────────────────────────────────────────
        // While a kill is pending, 'y'/'Y' fires it; every other key
        // cancels.  Mirrors `pending_kill_terminal` immediately above.
        if self.pending_kill_session.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char('y') | Key::Char('Y') => {
                        if let Some(p) = self.pending_kill_session.take() {
                            self.confirm_kill_session(p);
                        }
                    }
                    _ => {
                        self.pending_kill_session = None;
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── Pending purge confirmation: intercept ALL key presses ─────────────
        // While a purge is pending, 'y'/'Y' executes it; every other key
        // cancels.  We return early so the normal key dispatch never fires.
        if self.pending_purge.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char('y') | Key::Char('Y') => {
                        let secs = self.purge_days as f64 * 86_400.0;
                        match purge_done_assignments_db(secs) {
                            Ok((a, i)) => self.push_toast(
                                "Purge complete",
                                &format!(
                                    "Removed {} assignment{} + {} closed issue{}",
                                    a,
                                    if a == 1 { "" } else { "s" },
                                    i,
                                    if i == 1 { "" } else { "s" }
                                ),
                                ToastSeverity::Info,
                            ),
                            Err(e) => self.push_toast(
                                "Purge failed",
                                &format!("{}", e),
                                ToastSeverity::Error,
                            ),
                        }
                        self.pending_purge = None;
                        self.refresh();
                    }
                    _ => {
                        // Any other key cancels — Escape, 'n', 'N', or anything else.
                        self.pending_purge = None;
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── #259 / #607: open context menu intercepts keyboard nav ──────────
        // Up/Down/j/k move the keyboard selection (skipping separators);
        // Enter / Right opens a submenu (if selected item has one) or activates;
        // Left / Escape closes the deepest submenu; outer Escape dismisses all.
        if self.pending_context_menu.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Named(NamedKey::Down) | Key::Char('j') => {
                        self.context_menu_move_selection(1);
                    }
                    Key::Named(NamedKey::Up) | Key::Char('k') => {
                        self.context_menu_move_selection(-1);
                    }
                    Key::Named(NamedKey::Enter) => {
                        // Enter: open submenu if parent, else activate leaf.
                        self.context_menu_activate_selected();
                    }
                    Key::Named(NamedKey::Right) => {
                        // Right: open submenu parent only — no-op on leaf items.
                        // This prevents accidental dispatch of Stop/Watch/etc.
                        // when the user arrows past the submenu parents.
                        if self.context_menu_selected_has_submenu() {
                            self.context_menu_activate_selected();
                        }
                    }
                    Key::Named(NamedKey::Left) | Key::Named(NamedKey::Escape) => {
                        // Left / Esc: close deepest submenu or dismiss entirely.
                        self.context_menu_close_submenu_or_dismiss();
                    }
                    _ => {
                        // Any other key dismisses to keep the focus model
                        // simple — typing a global keybind while the menu
                        // is open shouldn't both dismiss and fire that
                        // bind, so we just dismiss.
                        self.dismiss_context_menu();
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── #245: Pending --force-merge confirmation: intercept ALL keys ──
        // The user has pressed `m` while the "Checks failed" hint was visible.
        // We refuse to bypass the CI gate without an explicit y/Y so a
        // fat-fingered `m` can't merge a red PR.
        if let Some(repo) = self.pending_force_merge.clone() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char('y') | Key::Char('Y') => {
                        let scoped = !repo.is_empty();
                        let mut args: Vec<&str> = vec!["merge", "--force-merge"];
                        if scoped {
                            args.push("--repo");
                            args.push(&repo);
                        }
                        use crate::commands::SpawnQueuedOutcome;
                        let scope_str = if scoped {
                            format!(" --repo {}", repo)
                        } else {
                            String::new()
                        };
                        match self.command_runner.spawn_queued(&args) {
                            SpawnQueuedOutcome::Started => {
                                self.push_toast(
                                    "Force-merge dispatched",
                                    &format!(
                                        "coord merge --force-merge{} — CI gate bypassed",
                                        scope_str
                                    ),
                                    ToastSeverity::Warning,
                                );
                            }
                            SpawnQueuedOutcome::Queued => {
                                self.push_toast(
                                    "⏳ Queued",
                                    "force-merge runs after current command",
                                    ToastSeverity::Info,
                                );
                            }
                            SpawnQueuedOutcome::Deduped => {}
                        }
                        self.pending_force_merge = None;
                    }
                    _ => {
                        // Any other key cancels — Escape, 'n', 'N', anything.
                        self.pending_force_merge = None;
                        self.push_toast(
                            "Force-merge cancelled",
                            "CI gate stays in place",
                            ToastSeverity::Info,
                        );
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── #780: Merge-all-ready confirm: intercept key presses ──────────
        if let Some(aids) = self.pending_merge_all_ready.clone() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                match key {
                    Key::Char('y') | Key::Char('Y') => {
                        // Drain the entire queue — `coord merge` already merges in
                        // READY order; no extra args needed.
                        let args: Vec<&str> = vec!["merge"];
                        use crate::commands::SpawnQueuedOutcome;
                        match self.command_runner.spawn_queued(&args) {
                            SpawnQueuedOutcome::Started => {
                                self.push_toast(
                                    "Merge all ready dispatched",
                                    &format!("coord merge — {} entr{} queued",
                                        aids.len(),
                                        if aids.len() == 1 { "y" } else { "ies" }),
                                    ToastSeverity::Info,
                                );
                            }
                            SpawnQueuedOutcome::Queued => {
                                self.push_toast(
                                    "⏳ Queued",
                                    "merge runs after current command",
                                    ToastSeverity::Info,
                                );
                            }
                            SpawnQueuedOutcome::Deduped => {}
                        }
                        self.pending_merge_all_ready = None;
                    }
                    _ => {
                        // Any other key cancels.
                        self.pending_merge_all_ready = None;
                        self.push_toast(
                            "Merge all cancelled",
                            "Queue unchanged",
                            ToastSeverity::Info,
                        );
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── #816: PTY-panic dialog key intercept ────────────────────────────
        // Esc and Enter dismiss; any other key is swallowed to keep the
        // dialog visible and let the operator read the fault message.
        //
        // This block intentionally runs BEFORE the artifact_pull_dialog
        // intercept below, matching the rendering priority established in
        // build_prompt_dialog (pty_panic_dialog is returned first / shown on
        // top).  When both dialogs are simultaneously active the operator sees
        // the PTY-panic dialog and their keystrokes must be routed to it first.
        if self.pty_panic_dialog.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                let dismiss = matches!(
                    key,
                    Key::Named(NamedKey::Escape) | Key::Named(NamedKey::Enter)
                );
                if dismiss {
                    self.pty_panic_dialog = None;
                    *self.dialog_layout.borrow_mut() = None;
                }
                return Reaction::Redraw;
            }
        }

        // ── #1059: Gate A dispatch-failure dialog key intercept ─────────────
        // Higher priority than the artifact-pull dialog below (mirrors the
        // pty_panic ordering): Esc / Enter dismiss, other keys are swallowed
        // so the full failure reason stays readable.
        if self.gate_a_error_dialog.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                let dismiss = matches!(
                    key,
                    Key::Named(NamedKey::Escape) | Key::Named(NamedKey::Enter)
                );
                if dismiss {
                    self.gate_a_error_dialog = None;
                    *self.dialog_layout.borrow_mut() = None;
                }
                return Reaction::Redraw;
            }
        }

        // ── #532: Artifact-pull dialog: intercept key presses ──────────────
        // While the info dialog is open:
        //   'c'/'C' — copy path to clipboard (when available), then dismiss.
        //   Esc / Enter — dismiss without copying.
        //   All other keys — swallow (redraw) without dismissing; this lets
        //   longer error messages be scrolled and prevents accidental dismiss
        //   on Tab / arrow keys while the dialog is focused.
        //
        // This block intentionally runs AFTER the destructive-confirmation
        // intercepts (pending_purge, pending_force_merge, pending_restart) AND
        // after the pty_panic_dialog intercept above, so that if both an
        // artifact dialog and a higher-priority dialog are alive at the same
        // time, the higher-priority one wins — matching the rendering priority
        // in build_prompt_dialog and avoiding silently swallowed keystrokes
        // against a hidden artifact dialog.
        if self.artifact_pull_dialog.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                let path = self
                    .artifact_pull_dialog
                    .as_ref()
                    .and_then(|d| d.path.clone());
                // Classification lives in a pure helper so tests cover the
                // exact key match that production uses.
                match classify_artifact_pull_dialog_key(key, path.is_some()) {
                    ArtifactDialogKeyOutcome::CopyAndDismiss => {
                        if let Some(p) = path {
                            backend.services().clipboard().write_text(&p);
                            self.push_toast(
                                "Copied",
                                "Path copied to clipboard",
                                ToastSeverity::Info,
                            );
                        }
                        self.artifact_pull_dialog = None;
                        *self.dialog_layout.borrow_mut() = None;
                    }
                    ArtifactDialogKeyOutcome::Dismiss => {
                        self.artifact_pull_dialog = None;
                        *self.dialog_layout.borrow_mut() = None;
                    }
                    ArtifactDialogKeyOutcome::Swallow => {
                        // All other keys are swallowed but do NOT close the
                        // dialog — keeps the dialog visible so the user can
                        // read it.  (No scroll offset is tracked here; arrow
                        // keys are simply absorbed.)
                    }
                }
                return Reaction::Redraw;
            }
        }

        // ── #816: PTY-panic dialog key intercept ────────────────────────────
        // Esc and Enter dismiss; any other key is swallowed to keep the
        // dialog visible and let the operator read the fault message.
        if self.pty_panic_dialog.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                let dismiss = matches!(
                    key,
                    Key::Named(NamedKey::Escape) | Key::Named(NamedKey::Enter)
                );
                if dismiss {
                    self.pty_panic_dialog = None;
                    *self.dialog_layout.borrow_mut() = None;
                }
                return Reaction::Redraw;
            }
        }

        // ── #1059: Gate A dispatch-failure dialog key intercept ─────────────
        // Esc / Enter dismiss; any other key is swallowed so the operator can
        // read the full (word-wrapped) failure reason without accidentally
        // dismissing on Tab / arrow keys.  Same shape as the pty_panic block
        // above.
        if self.gate_a_error_dialog.is_some() {
            if let UiEvent::KeyPressed { key, .. } = &event {
                let dismiss = matches!(
                    key,
                    Key::Named(NamedKey::Escape) | Key::Named(NamedKey::Enter)
                );
                if dismiss {
                    self.gate_a_error_dialog = None;
                    *self.dialog_layout.borrow_mut() = None;
                }
                return Reaction::Redraw;
            }
        }

        // ── User-mapped keybindings (checked before hardcoded bindings) ──────
        if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
            if let Some(action) = self.action_for_key(key, modifiers) {
                match action {
                    ACTION_PIPELINE_REFRESH => {
                        self.maybe_kick_pipeline_loader();
                        self.push_toast(
                            "Pipeline",
                            "Refreshing issues from GitHub…",
                            ToastSeverity::Info,
                        );
                        return Reaction::Redraw;
                    }
                    _ => {}
                }
            }
        }

        // ── #259 follow-up: keyboard equivalent of right-click ────────────────
        // Open the context menu for the row j/k has already selected, with no
        // mouse needed.  Bindings:
        //   • Menu / Application key (`NamedKey::Menu` — the dedicated key)
        //   • Shift+F10              (the universal cross-platform convention)
        //   • '.'                    (printable fallback for laptops that have
        //                            no Menu key; the "more actions" kebab)
        // Once open, the menu's own keyboard nav (j/k/Enter/Esc, intercepted
        // earlier when `pending_context_menu` is Some) drives it.  Guarded so
        // no binding steals a keystroke from an active search field or a
        // chat/watch/quit overlay — '.' must still type into a focused search.
        if self.pending_context_menu.is_none() {
            if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
                let is_trigger = matches!(key, Key::Named(NamedKey::Menu))
                    || (matches!(key, Key::Named(NamedKey::F(10))) && modifiers.shift)
                    || matches!(key, Key::Char('.'));
                let input_active = self.board_search.focused
                    || self.pipeline_search.focused
                    || self.inject_chat.is_some()
                    || self.watch_focused.is_some()
                    || self.pending_quit_confirm;
                if is_trigger && !input_active {
                    if let Some(target) = self.context_menu_target_for_selection() {
                        // Anchor near the sidebar's top-left; `menu.layout()`
                        // clamps/flips the popup to stay within the viewport.
                        let anchor = Point::new(list_b.x + 8.0, list_b.y + lh * 1.5);
                        if self.open_context_menu(anchor, target) {
                            return Reaction::Redraw;
                        }
                    }
                }
            }
        }

        // ── Keyboard and window events ────────────────────────────────────────
        match &event {
            UiEvent::KeyPressed { key, .. } => {
                match key {
                    // ── Board search input ───────────────────────────────
                    // #646/#566: ESC while the filter is focused blurs it
                    // (clear + unfocus) — never quits while filter has focus.
                    Key::Named(NamedKey::Escape)
                        if self.active_view == SidebarView::Board
                            && self.board_search.focused =>
                    {
                        self.board_search.clear(); // also sets focused = false
                        self.board_sidebar.focus_form(0, false);
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }
                    // Escape clears search or (if already empty) quits.
                    Key::Named(NamedKey::Escape)
                        if self.active_view == SidebarView::Board
                            && !self.board_search.is_empty() =>
                    {
                        self.board_search.clear();
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }
                    // Backspace while search is active removes char before cursor.
                    Key::Named(NamedKey::Backspace)
                        if self.active_view == SidebarView::Board && self.board_search.focused =>
                    {
                        self.board_search.backspace();
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }
                    // Any printable char while search is active inserts at cursor.
                    Key::Char(ch)
                        if self.active_view == SidebarView::Board && self.board_search.focused =>
                    {
                        self.board_search.insert_char(*ch);
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }
                    // '/' activates search when not already active.
                    Key::Char('/')
                        if self.active_view == SidebarView::Board && !self.board_search.focused =>
                    {
                        self.board_search.focused = true;
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }

                    // ── Pipeline search input ────────────────────────────
                    // Mirror the Board search arms for the Pipeline view so
                    // typing in the filter never falls through to the
                    // Pipeline keybinds (r/R/D/m/f/…) further down.
                    // #646/#566: ESC while the filter is focused blurs it.
                    Key::Named(NamedKey::Escape)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_search.focused =>
                    {
                        self.pipeline_search.clear(); // also sets focused = false
                        self.pipeline_sidebar.focus_form(0, false);
                        self.rebuild_pipeline_sidebar(None);
                        needs_redraw = true;
                    }
                    // Escape clears search (when non-empty); empty falls
                    // through to the global quit handler.
                    Key::Named(NamedKey::Escape)
                        if self.active_view == SidebarView::Pipeline
                            && !self.pipeline_search.is_empty() =>
                    {
                        self.pipeline_search.clear();
                        self.rebuild_pipeline_sidebar(None);
                        needs_redraw = true;
                    }
                    // Backspace while search is active removes char before cursor.
                    Key::Named(NamedKey::Backspace)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_search.focused =>
                    {
                        self.pipeline_search.backspace();
                        self.rebuild_pipeline_sidebar(None);
                        needs_redraw = true;
                    }
                    // Any printable char while search is active inserts at cursor.
                    Key::Char(ch)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_search.focused =>
                    {
                        self.pipeline_search.insert_char(*ch);
                        self.rebuild_pipeline_sidebar(None);
                        needs_redraw = true;
                    }
                    // '/' activates search when not already active.
                    Key::Char('/')
                        if self.active_view == SidebarView::Pipeline
                            && !self.pipeline_search.focused =>
                    {
                        self.pipeline_search.focused = true;
                        self.rebuild_pipeline_sidebar(None);
                        needs_redraw = true;
                    }

                    // ── Audit filter: free-text type input (#1040) ───────
                    // Mirrors the Board/Pipeline `SidebarFilter` arms above
                    // (`SidebarFilter` itself is `mod.rs:143`) but scoped to
                    // `active_view == Audit` and toggled with `f` (contract
                    // §10's `"f=filter"` hint), not `/`. Placed here, well
                    // before the unguarded `q`/Esc catch-all further down,
                    // so a typed 'q' (or 't'/Tab/j/k/r/Enter) inserts into
                    // the filter text instead of triggering the Audit
                    // keybind it would otherwise mean — same reasoning as
                    // the Board/Pipeline arms just above.
                    //
                    // Esc while focused clears the typed text and blurs.
                    // Unlike Board/Pipeline's client-side filter (a re-
                    // render is free), a non-empty value here maps to a
                    // live `/audit` `type=` query param, so clearing it
                    // must also re-arm the fetch (contract §11) — but only
                    // when it actually held a value, so blurring an
                    // already-empty field doesn't spuriously refetch.
                    Key::Named(NamedKey::Escape)
                        if self.active_view == SidebarView::Audit
                            && self.audit_type_filter.focused =>
                    {
                        let had_value = !self.audit_type_filter.is_empty();
                        self.audit_type_filter.clear(); // also sets focused = false
                        if had_value {
                            self.on_audit_filters_changed();
                        }
                        needs_redraw = true;
                    }
                    Key::Named(NamedKey::Backspace)
                        if self.active_view == SidebarView::Audit
                            && self.audit_type_filter.focused =>
                    {
                        self.audit_type_filter.backspace();
                        needs_redraw = true;
                    }
                    Key::Char(ch)
                        if self.active_view == SidebarView::Audit
                            && self.audit_type_filter.focused =>
                    {
                        self.audit_type_filter.insert_char(*ch);
                        needs_redraw = true;
                    }
                    // Enter commits the typed value and re-arms the fetch
                    // (contract §11).
                    Key::Named(NamedKey::Enter)
                        if self.active_view == SidebarView::Audit
                            && self.audit_type_filter.focused =>
                    {
                        self.audit_type_filter.focused = false;
                        self.on_audit_filters_changed();
                        needs_redraw = true;
                    }
                    // `f` opens the filter for typing when not already
                    // focused; list-mode only (guarded off the detail pane,
                    // same as the other Audit-view-only keys below).
                    Key::Char('f')
                        if self.active_view == SidebarView::Audit
                            && !self.audit_type_filter.focused
                            && !self.audit_detail_open =>
                    {
                        self.audit_type_filter.focused = true;
                        needs_redraw = true;
                    }

                    // ── Watch overlay: control keys ─────────────────────
                    // 'b' opens the ChatController guidance overlay. When the
                    // overlay is open, ALL events are intercepted earlier in
                    // handle() — these arms only fire when it is closed.
                    Key::Char('b')
                        if self.watch_focused.is_some() && self.inject_chat.is_none() =>
                    {
                        if let Some(w) = self.focused_watch_state() {
                            let atype = w.assignment_type.clone();
                            let issue_n = w.issue_number;
                            let machine = w.machine.clone();
                            let mut chat = ChatController::new("inject");
                            chat.set_status(StyledText::plain(format!(
                                "  Guidance → {} #{} on {}  (Ctrl+S or Alt+Enter = send · Esc = close)",
                                atype, issue_n, machine
                            )));
                            chat.set_transcript(self.focused_transcript().to_vec());
                            self.inject_chat = Some(chat);
                        }
                        needs_redraw = true;
                    }
                    Key::Char('K') if self.watch_focused.is_some() => {
                        self.kill_watched();
                        needs_redraw = true;
                    }
                    Key::Char('A') if self.watch_focused.is_some() => {
                        self.approve_watched_plan();
                        needs_redraw = true;
                    }
                    // R = force a fresh SSE connection from byte 0.
                    Key::Char('R') if self.watch_focused.is_some() => {
                        self.reset_sse_watch();
                        needs_redraw = true;
                    }
                    Key::Char('q') | Key::Named(NamedKey::Escape)
                        if self.watch_focused.is_some() =>
                    {
                        self.close_watch();
                        needs_redraw = true;
                    }
                    // j/k scroll the watch-overlay log (not the Log detail tab).
                    // The Log tab only seeds `watch_pool` without focusing, so
                    // `watch_focused.is_some()` is false there and j/k falls
                    // through to the dedicated Log-tab arms below (#308).
                    Key::Char('j') | Key::Named(NamedKey::Down) if self.watch_focused.is_some() => {
                        if let Some(w) = self.focused_watch_state_mut() {
                            let current = if w.scroll == usize::MAX { 0 } else { w.scroll };
                            w.scroll = current.saturating_add(1);
                        }
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up) if self.watch_focused.is_some() => {
                        if let Some(w) = self.focused_watch_state_mut() {
                            let current = if w.scroll == usize::MAX { 0 } else { w.scroll };
                            w.scroll = current.saturating_sub(1);
                        }
                        needs_redraw = true;
                    }

                    // Guard the global quit: an unfocused Esc/q must not
                    // silently exit while an interactive session is live in the
                    // Terminal tab (the running claude survives in tmux, but the
                    // operator loses the attached view — it reads as "gone").
                    // Instead of dead-ending with a toast (no way to override),
                    // show a force-quit confirmation dialog.
                    Key::Char('q') | Key::Named(NamedKey::Escape)
                        if self.terminal_tab_has_live_session()
                            && !self.pending_quit_confirm =>
                    {
                        self.pending_quit_confirm = true;
                        needs_redraw = true;
                    }
                    // #1039: Esc closes the Audit entry-detail pane back to
                    // the list-only view (contract §7) instead of quitting —
                    // must precede the unguarded catch-all below, same
                    // reasoning as the `watch_focused`/live-session guards
                    // just above.
                    Key::Named(NamedKey::Escape)
                        if self.active_view == SidebarView::Audit
                            && self.audit_detail_open =>
                    {
                        self.audit_detail_open = false;
                        needs_redraw = true;
                    }
                    // #1040 contract §10: Esc clears all active Audit
                    // filters (time-range/category/type text) back to their
                    // defaults and re-arms the fetch — must precede the
                    // unguarded catch-all just below, same reasoning as the
                    // detail-pane-close arm just above. Guarded off
                    // `audit_type_filter.focused` (that narrower "blur the
                    // type field" arm lives further up, alongside the rest
                    // of the type-filter typing arms) and off
                    // `audit_detail_open` (closing the detail pane takes
                    // priority — the arm just above). Only fires when a
                    // filter is actually non-default, so plain Esc on an
                    // unfiltered Audit panel still falls through to the
                    // global quit-confirm catch-all.
                    Key::Named(NamedKey::Escape)
                        if self.active_view == SidebarView::Audit
                            && !self.audit_detail_open
                            && !self.audit_type_filter.focused
                            && self.audit_filters_active() =>
                    {
                        self.audit_time_range = AuditTimeRange::All;
                        self.audit_category = AuditCategory::All;
                        self.audit_type_filter.clear();
                        self.on_audit_filters_changed();
                        needs_redraw = true;
                    }
                    Key::Char('q') | Key::Named(NamedKey::Escape) => return Reaction::Exit,

                    // §3 (#782): numeric keys 1-7 used to switch sidebar views
                    // directly (Board/Machines/Pipeline/Settings/Terminal/
                    // Kanban/MergeQueue). Removed — views are now discovered
                    // and switched exclusively via the activity-bar panel
                    // buttons (click, or Ctrl-W focus-cycle + activity-bar
                    // selection) so the digits are free for other bindings
                    // and don't silently fire mid-typing (e.g. commit
                    // messages, search filters). This also covers the #771
                    // "8 → Milestone DAG" switch key that landed on main
                    // after this branch diverged — dropped for the same
                    // reason; the DAG view is reached via its activity-bar
                    // icon like every other panel.

                    // ── Milestone DAG keyboard nav (#771) ────────────────
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::MilestoneDag =>
                    {
                        let n = self.milestone_dag_views().len();
                        if n > 0 {
                            self.milestone_dag_sel =
                                (self.milestone_dag_sel + 1).min(n.saturating_sub(1));
                        }
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::MilestoneDag =>
                    {
                        self.milestone_dag_sel = self.milestone_dag_sel.saturating_sub(1);
                        needs_redraw = true;
                    }

                    // ── Plans panel keyboard nav (#975) ──────────────────
                    // #1001: `plans_sel` indexes into `plans_visible_entries()`
                    // — the currently-rendered rows — not the full roster, so
                    // navigation never lands on a collapsed no-work-order
                    // milestone that isn't on screen.
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Plans =>
                    {
                        let n = self.plans_visible_entries().len();
                        if n > 0 {
                            self.plans_sel = (self.plans_sel + 1).min(n.saturating_sub(1));
                        }
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Plans =>
                    {
                        self.plans_sel = self.plans_sel.saturating_sub(1);
                        needs_redraw = true;
                    }
                    // #1001: `u` toggles whether the currently-selected row's
                    // repo shows its "without a work order" milestones
                    // (default: collapsed into a "+N" summary line).
                    Key::Char('u') if self.active_view == SidebarView::Plans => {
                        self.toggle_plans_repo_expansion();
                        needs_redraw = true;
                    }
                    // Enter — open the tracking epic of the selected plan in
                    // the browser via `gh issue view --web`.  A no-op with a
                    // toast when the plan has no epic yet (#977 / #978 cover
                    // the create-epic workflow).
                    Key::Named(NamedKey::Enter)
                        if self.active_view == SidebarView::Plans =>
                    {
                        self.open_selected_plan_tracking_epic();
                        needs_redraw = true;
                    }
                    // "Capture a plan" (#977) — one-key fast-jot: pops the
                    // plan-title prompt. `c` is free in this view (the only
                    // other bare 'c' binding is Ctrl+C copy-selection, gated
                    // on modifiers.ctrl; 'n' is taken globally by `notify`).
                    Key::Char('c')
                        if self.active_view == SidebarView::Plans
                            && self.pending_plan_capture.is_none() =>
                    {
                        self.pending_plan_capture = Some(String::new());
                        needs_redraw = true;
                    }
                    // "New milestone via chat…" (#1017) — chat-driven sibling
                    // of `c` above: pops an (optional) title prompt, then
                    // dispatches `coord milestone chat <repo> --new` instead
                    // of the direct `coord milestone capture`. Bare `C` is
                    // free in this view (Ctrl+C copy-selection above is
                    // gated on modifiers.ctrl).
                    Key::Char('C')
                        if self.active_view == SidebarView::Plans
                            && self.pending_new_milestone_chat.is_none() =>
                    {
                        self.pending_new_milestone_chat = Some(String::new());
                        needs_redraw = true;
                    }
                    // "Dispatch milestone" — promote the selected milestone's
                    // declared work order into the pipeline (#767 Phase 1).
                    Key::Char('d')
                        if self.active_view == SidebarView::MilestoneDag =>
                    {
                        let target = self.milestone_dag_selected().map(|v| {
                            ContextMenuTarget::MilestoneHeader {
                                repo_name: v.repo_name.clone(),
                                tracking_issue: v.tracking_issue,
                                milestone_title: v.milestone_title.clone(),
                                milestone_number: v.milestone_number,
                            }
                        });
                        if let Some(target) = target {
                            self.dispatch_milestone_action(&target);
                        } else {
                            self.push_toast(
                                "Dispatch milestone",
                                "No milestone selected — no work-order block found.",
                                ToastSeverity::Info,
                            );
                        }
                        needs_redraw = true;
                    }

                    // ── Audit panel keyboard nav (#1039) ─────────────────
                    // Nav/Enter/`r` are only meaningful in list-only mode;
                    // Esc closes the detail pane back to the list (contract
                    // §7). Global Esc handling elsewhere (dialogs, chat,
                    // etc.) takes priority — this arm only fires when
                    // nothing else already claimed Esc for this frame.
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Audit
                            && !self.audit_detail_open =>
                    {
                        let n = self.audit_entries().len();
                        if n > 0 {
                            self.audit_sel = (self.audit_sel + 1).min(n - 1);
                        }
                        // #1094 fix: keep the selection inside the viewport
                        // — the row list is in the MAIN panel here (unlike
                        // Machines/MergeQueue, whose navigable list IS the
                        // sidebar), so use `main_bounds`, not `list_b`.
                        self.fix_audit_scroll(content_visible_rows(ctx.main_bounds(), lh));
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Audit
                            && !self.audit_detail_open =>
                    {
                        self.audit_sel = self.audit_sel.saturating_sub(1);
                        self.fix_audit_scroll(content_visible_rows(ctx.main_bounds(), lh));
                        needs_redraw = true;
                    }
                    Key::Named(NamedKey::Enter)
                        if self.active_view == SidebarView::Audit
                            && !self.audit_detail_open
                            && !self.audit_entries().is_empty() =>
                    {
                        self.audit_detail_open = true;
                        needs_redraw = true;
                    }
                    // Esc (while the detail pane is open) is handled earlier,
                    // alongside the other pending-state Escape guards, since
                    // it must run *before* the unguarded global `q`/Esc = Exit
                    // catch-all below — see the block just above that
                    // catch-all (`terminal_tab_has_live_session` guard).
                    Key::Char('r')
                        if self.active_view == SidebarView::Audit
                            && !self.audit_detail_open =>
                    {
                        self.refresh_audit();
                        needs_redraw = true;
                    }
                    // #1040 contract §8: `t` cycles the time-range filter
                    // (Last hour → Today → 7d → All → …) and re-arms the
                    // fetch. `!audit_type_filter.focused` is belt-and-braces
                    // here — the type-filter typing arms further up already
                    // claim every `Char`/`Backspace` while focused, so this
                    // guard can never actually be false when reached, but
                    // it documents the intent explicitly.
                    Key::Char('t')
                        if self.active_view == SidebarView::Audit
                            && !self.audit_detail_open
                            && !self.audit_type_filter.focused =>
                    {
                        self.audit_time_range = self.audit_time_range.next();
                        self.on_audit_filters_changed();
                        needs_redraw = true;
                    }
                    // #1040 contract §9: Tab cycles the category filter
                    // (all → dispatch → test → review → merge → override →
                    // plan → error → …) and re-arms the fetch.
                    Key::Named(NamedKey::Tab)
                        if self.active_view == SidebarView::Audit
                            && !self.audit_detail_open
                            && !self.audit_type_filter.focused =>
                    {
                        self.audit_category = self.audit_category.next();
                        self.on_audit_filters_changed();
                        needs_redraw = true;
                    }

                    // ── Merge Queue keyboard nav (#737) ──────────────────
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::MergeQueue =>
                    {
                        let n = self.data.merge_queue.len();
                        if n > 0 {
                            self.merge_queue_sel =
                                (self.merge_queue_sel + 1).min(n.saturating_sub(1));
                        }
                        self.fix_merge_queue_scroll(content_visible_rows(list_b, lh));
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::MergeQueue =>
                    {
                        self.merge_queue_sel = self.merge_queue_sel.saturating_sub(1);
                        self.fix_merge_queue_scroll(content_visible_rows(list_b, lh));
                        needs_redraw = true;
                    }
                    // #780: a → "Merge all ready" — confirm prompt then drain queue.
                    Key::Char('a')
                        if self.active_view == SidebarView::MergeQueue =>
                    {
                        self.dispatch_merge_queue_merge_all();
                        needs_redraw = true;
                    }
                    // #780: m → "Merge only this" — coord merge --only <aid>
                    Key::Char('m')
                        if self.active_view == SidebarView::MergeQueue =>
                    {
                        self.dispatch_merge_queue_merge_only(false);
                        needs_redraw = true;
                    }
                    // #780: M → "Force merge only this" — coord merge --only <aid> --force-merge
                    Key::Char('M')
                        if self.active_view == SidebarView::MergeQueue =>
                    {
                        self.dispatch_merge_queue_merge_only(true);
                        needs_redraw = true;
                    }
                    // d → coord merge --drop <assignment_id>
                    Key::Char('d')
                        if self.active_view == SidebarView::MergeQueue =>
                    {
                        self.dispatch_merge_queue_drop();
                        needs_redraw = true;
                    }
                    // s → coord assign --interactive --merge-of <assignment_id>
                    //     (launches in the standalone Terminal pane, like Chat/Troubleshoot)
                    Key::Char('s')
                        if self.active_view == SidebarView::MergeQueue =>
                    {
                        self.launch_merge_queue_interactive();
                        needs_redraw = true;
                    }

                    // ── #728: Done section extend-range (→) ─────────────
                    // `→` while the Pipeline Done section is focused cycles
                    // the time window: H2 → H24 → D7 → All → H2.
                    Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Pipeline
                            && self.is_done_section_active()
                            && !self.pipeline_search.focused =>
                    {
                        self.done_window = self.done_window.next();
                        self.rebuild_pipeline_sidebar(None);
                        // After rebuild the SidebarSystem starts with
                        // active_section() == None, so the default_select block
                        // in rebuild_pipeline_sidebar selects the FIRST section
                        // (usually in-progress) and the restore loop only re-
                        // focuses Done if pipeline_sel pointed at a Done row.
                        // When the user is on the Done section HEADER (no issue
                        // row selected) pipeline_sel is None, so the restore is
                        // skipped and Done loses focus.  Re-assert Done focus
                        // unconditionally here so subsequent → presses still
                        // fire the extend-range handler.
                        if let Some(done_idx) = self
                            .pipeline_state_section_names
                            .iter()
                            .position(|&k| k == "done")
                            .map(|i| i + 1)
                        {
                            self.pipeline_sidebar.set_active_section(Some(done_idx));
                        }
                        needs_redraw = true;
                    }

                    // ── Kanban keyboard nav (#638) ───────────────────────
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Kanban =>
                    {
                        self.kanban_model.move_selection(MoveDir::Down);
                        self.kanban_clamp_col_scroll();
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Kanban =>
                    {
                        self.kanban_model.move_selection(MoveDir::Up);
                        self.kanban_clamp_col_scroll();
                        needs_redraw = true;
                    }
                    Key::Char('h') | Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Kanban =>
                    {
                        self.kanban_model.move_selection(MoveDir::Left);
                        self.kanban_clamp_col_scroll();
                        needs_redraw = true;
                    }
                    Key::Char('l') | Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Kanban =>
                    {
                        self.kanban_model.move_selection(MoveDir::Right);
                        self.kanban_clamp_col_scroll();
                        needs_redraw = true;
                    }
                    Key::Char('g') if self.active_view == SidebarView::Kanban => {
                        self.kanban_model.jump_to_top();
                        self.kanban_clamp_col_scroll();
                        needs_redraw = true;
                    }
                    Key::Char('G') if self.active_view == SidebarView::Kanban => {
                        self.kanban_model.jump_to_bottom();
                        self.kanban_clamp_col_scroll();
                        needs_redraw = true;
                    }
                    Key::Named(NamedKey::Enter) if self.active_view == SidebarView::Kanban => {
                        if let Some(card_id) = self.kanban_model.selected_card_id.clone() {
                            self.kanban_open_card(&card_id);
                            needs_redraw = true;
                        }
                    }

                    // ── Settings panel keyboard nav ──────────────────────
                    // #237: j/k now step through the unified form's
                    // interactive fields directly — there are no
                    // categories to navigate any more.
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Settings =>
                    {
                        let count = self.settings_interactive_field_ids().len();
                        if count > 0 {
                            self.settings_field_sel = (self.settings_field_sel + 1).min(count - 1);
                        }
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Settings =>
                    {
                        self.settings_field_sel = self.settings_field_sel.saturating_sub(1);
                        needs_redraw = true;
                    }
                    // Tab — next interactive field within the form
                    Key::Named(NamedKey::Tab) if self.active_view == SidebarView::Settings => {
                        let count = self.settings_interactive_field_ids().len();
                        if count > 1 {
                            self.settings_field_sel = (self.settings_field_sel + 1) % count;
                        }
                        needs_redraw = true;
                    }
                    // l/Right — next option (SegmentedControl) or toggle (Toggle)
                    Key::Char('l') | Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Settings =>
                    {
                        if self.settings_change_focused(1) {
                            needs_redraw = true;
                        }
                    }
                    // h/Left — previous option
                    Key::Char('h') | Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Settings =>
                    {
                        if self.settings_change_focused(-1) {
                            needs_redraw = true;
                        }
                    }
                    // Space/Enter — toggle or select current field
                    Key::Char(' ') | Key::Named(NamedKey::Enter)
                        if self.active_view == SidebarView::Settings =>
                    {
                        if self.settings_change_focused(1) {
                            needs_redraw = true;
                        }
                    }

                    // ── Tab — cycle sections within Board SidebarSystem ──
                    Key::Named(NamedKey::Tab) if self.active_view == SidebarView::Board => {
                        let prev_sel = self.board_selected_issue();
                        let result = self.board_sidebar.handle(&event, backend, list_b);
                        if result != SidebarEvent::Ignored {
                            let new_sel = self.board_selected_issue();
                            if new_sel != prev_sel {
                                self.detail_scroll = 0;
                            }
                            needs_redraw = true;
                        }
                    }

                    // ── j/k — scroll Issue tab body ───────────────────────
                    // §4 (#782): only when focus is off the sidebar (Main/Detail).
                    // In Sidebar focus these fall through to the generic arm so
                    // j/k drives the issue list, keeping the Ctrl-W focus model
                    // consistent across every tab.
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Issue
                            && self.focused_region != FocusedRegion::Sidebar =>
                    {
                        self.pipeline_detail_scroll = self.pipeline_detail_scroll.saturating_add(1);
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Issue
                            && self.focused_region != FocusedRegion::Sidebar =>
                    {
                        self.pipeline_detail_scroll = self.pipeline_detail_scroll.saturating_sub(1);
                        needs_redraw = true;
                    }

                    // ── j/k — scroll Overview tab stage body ──────────────
                    // `[`/`]` switch the focused stage; j/k scroll the
                    // rendered content (plans + log tails overflow easily).
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Overview
                            && self.focused_region != FocusedRegion::Sidebar =>
                    {
                        self.pipeline_stage_content_scroll =
                            self.pipeline_stage_content_scroll.saturating_add(1);
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Overview
                            && self.focused_region != FocusedRegion::Sidebar =>
                    {
                        self.pipeline_stage_content_scroll =
                            self.pipeline_stage_content_scroll.saturating_sub(1);
                        needs_redraw = true;
                    }

                    // ── j/k — scroll Log tab: sticky-to-bottom ────────────
                    // Up breaks sticky; Down re-sticks when reaching the bottom.
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Log
                            && self.focused_region != FocusedRegion::Sidebar =>
                    {
                        let items = self.pipeline_log_list().items.len();
                        let visible = self.last_main_visible_rows.get().max(1);
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        if self.pipeline_detail_scroll != usize::MAX {
                            let new = self.pipeline_detail_scroll.saturating_add(1);
                            self.pipeline_detail_scroll = if new >= max { usize::MAX } else { new };
                        }
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Log
                            && self.focused_region != FocusedRegion::Sidebar =>
                    {
                        let items = self.pipeline_log_list().items.len();
                        let visible = self.last_main_visible_rows.get().max(1);
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        let current = if self.pipeline_detail_scroll == usize::MAX {
                            max
                        } else {
                            self.pipeline_detail_scroll
                        };
                        self.pipeline_detail_scroll = current.saturating_sub(1);
                        needs_redraw = true;
                    }

                    // ── < / > — horizontal scroll the Log tab (#302). Lines are
                    // no longer clipped at 60 chars; scroll sideways to read long
                    // turn text / commands. The rasteriser clamps the offset to
                    // content width and paints an h-scrollbar.
                    //
                    // #605: bare h/l/Left/Right used to be bound here, which
                    // hijacked the global tab-cycle convention and stranded the
                    // user on the Log tab ("can't get past Log"). H-scroll now
                    // lives on `<`/`>` so bare h/l/Left/Right fall through to the
                    // detail-tab cycler below.
                    Key::Char('>')
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Log =>
                    {
                        self.pipeline_log_hscroll = self.pipeline_log_hscroll.saturating_add(8);
                        needs_redraw = true;
                    }
                    Key::Char('<')
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Log =>
                    {
                        self.pipeline_log_hscroll = self.pipeline_log_hscroll.saturating_sub(8);
                        needs_redraw = true;
                    }

                    // ── PageDown — scroll Log tab one page down (#307) ────
                    Key::Named(NamedKey::PageDown)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Log =>
                    {
                        let items = self.pipeline_log_list().items.len();
                        let visible = self.last_main_visible_rows.get().max(1);
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        if self.pipeline_detail_scroll != usize::MAX {
                            let new = self.pipeline_detail_scroll.saturating_add(visible);
                            self.pipeline_detail_scroll = if new >= max { usize::MAX } else { new };
                        }
                        needs_redraw = true;
                    }

                    // ── PageUp — scroll Log tab one page up (#307) ────────
                    Key::Named(NamedKey::PageUp)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Log =>
                    {
                        let items = self.pipeline_log_list().items.len();
                        let visible = self.last_main_visible_rows.get().max(1);
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        let current = if self.pipeline_detail_scroll == usize::MAX {
                            max
                        } else {
                            self.pipeline_detail_scroll
                        };
                        self.pipeline_detail_scroll = current.saturating_sub(visible);
                        needs_redraw = true;
                    }

                    // ── j/k — scroll Summary tab ──────────────────────────
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Summary
                            && self.focused_region != FocusedRegion::Sidebar =>
                    {
                        let items = self.pipeline_summary_list().items.len();
                        let visible = self.last_main_visible_rows.get().max(1);
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        self.pipeline_detail_scroll =
                            (self.pipeline_detail_scroll + 1).min(max);
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Summary
                            && self.focused_region != FocusedRegion::Sidebar =>
                    {
                        self.pipeline_detail_scroll =
                            self.pipeline_detail_scroll.saturating_sub(1);
                        needs_redraw = true;
                    }

                    // ── i — inject/steer a running worker from the Log tab (#386)
                    // Opens the guidance-chat overlay bound to the running
                    // assignment for the selected pipeline row so the user can
                    // send a mid-run message without leaving the Log view.
                    // Uses the same submit_inject / spawn_inject_post path
                    // as the 'b' keybind on the watch overlay — no new HTTP
                    // machinery, just wiring.
                    Key::Char('i')
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Log
                            && self.watch_focused.is_none()
                            && self.inject_chat.is_none() =>
                    {
                        // Find the running assignment for the selected row.
                        let running = self
                            .pipeline_sel
                            .and_then(|i| self.pipeline_issues.get(i).cloned())
                            .and_then(|issue| {
                                let local_repo = issue.coord_repo.as_deref().map(|s| s.to_string());
                                self.data
                                    .assignments
                                    .iter()
                                    .filter(|a| a.issue_number == issue.number)
                                    .filter(|a| match local_repo.as_deref() {
                                        Some(r) => a.repo == r,
                                        None => true,
                                    })
                                    .find(|a| a.status == "running")
                                    .map(|a| {
                                        (
                                            a.id.clone(),
                                            a.issue_number,
                                            a.machine.clone(),
                                            a.assignment_type
                                                .clone()
                                                .unwrap_or_else(|| "work".to_string()),
                                        )
                                    })
                            });
                        if let Some((aid, issue_n, machine, atype)) = running {
                            // Ensure the SSE pool has an entry (Log tab may
                            // already have seeded one via ensure_log_tab_sse).
                            if !self.watch_pool.contains_key(&aid) {
                                self.open_sse_in_pool_for_selected_issue();
                            }
                            // Focus the assignment so submit_inject can reach it.
                            // Set the flag so the Cancelled arm knows to restore
                            // watch_focused=None on Esc (Log-tab invariant, #308).
                            self.watch_focused = Some(aid);
                            self.inject_opened_from_log_tab = true;
                            // Open the guidance chat overlay — same shape as
                            // the 'b' handler in the watch overlay above.
                            let mut chat = ChatController::new("inject");
                            chat.set_status(StyledText::plain(format!(
                                "  Steer → {} #{} on {}  (Ctrl+S or Alt+Enter = send · Esc = close)",
                                atype, issue_n, machine
                            )));
                            chat.set_transcript(self.focused_transcript().to_vec());
                            self.inject_chat = Some(chat);
                        } else {
                            self.push_toast(
                                "Steer",
                                "No running assignment to steer for this issue.",
                                ToastSeverity::Warning,
                            );
                        }
                        needs_redraw = true;
                    }

                    // ── Down / j ─────────────────────────────────────────
                    // §4 (#782): when focus is on Main/Detail (moved off the
                    // sidebar with Ctrl-W), j/Down scrolls the content pane
                    // instead of moving the sidebar selection — this is what
                    // makes the focus indicator functional rather than cosmetic.
                    // Sidebar focus keeps the original list-navigation below.
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.focused_region != FocusedRegion::Sidebar
                            && self.scroll_focused_content(true) =>
                    {
                        needs_redraw = true;
                    }
                    Key::Char('j') | Key::Named(NamedKey::Down) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let prev_sel = self.board_selected_issue();
                                let result = self.board_sidebar.handle(&event, backend, list_b);
                                if result != SidebarEvent::Ignored {
                                    let new_sel = self.board_selected_issue();
                                    if new_sel != prev_sel {
                                        self.detail_scroll = 0;
                                    }
                                }
                            }
                            SidebarView::Machines => {
                                let m = self.data.machines.len();
                                if m > 0 && self.machine_sel + 1 < m {
                                    self.machine_sel += 1;
                                    self.machine_detail_scroll = 0;
                                }
                                self.fix_machine_scroll(content_visible_rows(list_b, lh));
                            }
                            SidebarView::Pipeline => {
                                let prev = self.pipeline_sel;
                                self.pipeline_sidebar.handle(&event, backend, list_b);
                                self.pipeline_sel = self.selected_pipeline_index();
                                if self.pipeline_sel != prev {
                                    self.pipeline_detail_scroll = 0;
                                    self.pipeline_focused_stage =
                                        self.default_focused_stage_for_selected_issue();
                                    self.pipeline_stage_content_scroll = 0;
                                }
                            }
                            // Settings: handled by the earlier guarded arm.
                            SidebarView::Settings => {}
                            // #953: move the tree cursor down one flattened
                            // row (machine or terminal). #424: when the PTY
                            // has focus, keys never reach here (intercepted
                            // earlier as passthrough), so this is safe.
                            SidebarView::Terminal => {
                                self.terminal_tree_move_selection(1);
                                self.fix_terminal_tree_scroll(content_visible_rows(list_b, lh));
                            }
                            // #638: Kanban j/k handled by the earlier guarded arm.
                            SidebarView::Kanban => {}
                            // #737: MergeQueue j/k handled by the earlier guarded arm.
                            SidebarView::MergeQueue => {}
                            // #771: MilestoneDag j/k handled by the earlier guarded arm.
                            SidebarView::MilestoneDag => {}
                            // #975: Plans j/k handled by the earlier guarded arm.
                            SidebarView::Plans => {}
                            // #1032: move the Sessions tree cursor down
                            // one flattened row (machine/repo/session).
                            SidebarView::Sessions => {
                                self.sessions_tree_move_selection(1);
                                self.fix_sessions_tree_scroll(content_visible_rows(list_b, lh));
                            }
                            // #1039: list-mode j/k handled by the earlier
                            // guarded arm; a no-op here (which only runs
                            // while the detail pane is open, since the
                            // guarded arm requires `!audit_detail_open`).
                            SidebarView::Audit => {}
                        }
                        needs_redraw = true;
                    }

                    // ── Up / k ───────────────────────────────────────────
                    // §4 (#782): mirror of the Down/j arm — Main/Detail focus
                    // scrolls the content pane up; Sidebar focus navigates the
                    // list below.
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.focused_region != FocusedRegion::Sidebar
                            && self.scroll_focused_content(false) =>
                    {
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let prev_sel = self.board_selected_issue();
                                let result = self.board_sidebar.handle(&event, backend, list_b);
                                if result != SidebarEvent::Ignored {
                                    let new_sel = self.board_selected_issue();
                                    if new_sel != prev_sel {
                                        self.detail_scroll = 0;
                                    }
                                }
                            }
                            SidebarView::Machines => {
                                if self.machine_sel > 0 {
                                    self.machine_sel -= 1;
                                    self.machine_detail_scroll = 0;
                                }
                                self.fix_machine_scroll(content_visible_rows(list_b, lh));
                            }
                            SidebarView::Pipeline => {
                                let prev = self.pipeline_sel;
                                self.pipeline_sidebar.handle(&event, backend, list_b);
                                self.pipeline_sel = self.selected_pipeline_index();
                                if self.pipeline_sel != prev {
                                    self.pipeline_detail_scroll = 0;
                                    self.pipeline_focused_stage =
                                        self.default_focused_stage_for_selected_issue();
                                    self.pipeline_stage_content_scroll = 0;
                                }
                            }
                            // Settings: handled by the earlier guarded arm.
                            SidebarView::Settings => {}
                            // #953: see Down/j arm above.
                            SidebarView::Terminal => {
                                self.terminal_tree_move_selection(-1);
                                self.fix_terminal_tree_scroll(content_visible_rows(list_b, lh));
                            }
                            // #638: Kanban k handled by the earlier guarded arm.
                            SidebarView::Kanban => {}
                            // #737: MergeQueue k handled by the earlier guarded arm.
                            SidebarView::MergeQueue => {}
                            // #771: MilestoneDag k handled by the earlier guarded arm.
                            SidebarView::MilestoneDag => {}
                            // #975: Plans k handled by the earlier guarded arm.
                            SidebarView::Plans => {}
                            // #1032: see Down/j arm above.
                            SidebarView::Sessions => {
                                self.sessions_tree_move_selection(-1);
                                self.fix_sessions_tree_scroll(content_visible_rows(list_b, lh));
                            }
                            // #1039: see Down/j arm above.
                            SidebarView::Audit => {}
                        }
                        needs_redraw = true;
                    }

                    // ── [ / ] — cycle focused stage (Overview tab) ────────
                    // Sets `pipeline_focused_stage`, which the rasteriser
                    // draws with an accent border on the stage boxes and
                    // selects which stage's content the scrollable panel shows.
                    // #818: available on the Overview tab only (Stages tab removed).
                    Key::Char('[')
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Overview =>
                    {
                        self.focus_prev_pipeline_stage();
                        needs_redraw = true;
                    }
                    Key::Char(']')
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Overview =>
                    {
                        self.focus_next_pipeline_stage();
                        needs_redraw = true;
                    }

                    // ── h/l — cycle Pipeline detail tabs ─────────────────
                    // #818 order: Overview → Issue → Log → Summary → Terminal → Overview …
                    Key::Char('h') | Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        let next = match self.pipeline_detail_tab {
                            PipelineDetailTab::Overview => PipelineDetailTab::Terminal,
                            PipelineDetailTab::Issue => PipelineDetailTab::Overview,
                            PipelineDetailTab::Log => PipelineDetailTab::Issue,
                            PipelineDetailTab::Summary => PipelineDetailTab::Log,
                            PipelineDetailTab::Terminal => PipelineDetailTab::Summary,
                        };
                        // Release PTY focus whenever the user navigates away
                        // from the Terminal tab.
                        if self.pipeline_detail_tab == PipelineDetailTab::Terminal {
                            self.detail_terminal_focused = false;
                        }
                        self.pipeline_detail_tab = next;
                        // #605: landing ON the Terminal tab via keyboard nav
                        // focuses the PTY immediately — no separate F12 needed
                        // ("once the tab is active it should have focus").
                        // The mouse tab-click path is intentionally left
                        // unfocused (no mouse-focus support yet).
                        if next == PipelineDetailTab::Terminal {
                            self.detail_terminal_focused = true;
                        }
                        self.pipeline_detail_scroll =
                            if self.pipeline_detail_tab == PipelineDetailTab::Log {
                                usize::MAX
                            } else {
                                0
                            };
                        if self.pipeline_detail_tab == PipelineDetailTab::Log {
                            self.ensure_log_tab_sse();
                        }
                        needs_redraw = true;
                    }
                    Key::Char('l') | Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        let next = match self.pipeline_detail_tab {
                            PipelineDetailTab::Overview => PipelineDetailTab::Issue,
                            PipelineDetailTab::Issue => PipelineDetailTab::Log,
                            PipelineDetailTab::Log => PipelineDetailTab::Summary,
                            PipelineDetailTab::Summary => PipelineDetailTab::Terminal,
                            PipelineDetailTab::Terminal => PipelineDetailTab::Overview,
                        };
                        // Release PTY focus whenever the user navigates away
                        // from the Terminal tab.
                        if self.pipeline_detail_tab == PipelineDetailTab::Terminal {
                            self.detail_terminal_focused = false;
                        }
                        self.pipeline_detail_tab = next;
                        // #605: landing ON the Terminal tab via keyboard nav
                        // focuses the PTY immediately — no separate F12 needed
                        // ("once the tab is active it should have focus").
                        // The mouse tab-click path is intentionally left
                        // unfocused (no mouse-focus support yet).
                        if next == PipelineDetailTab::Terminal {
                            self.detail_terminal_focused = true;
                        }
                        self.pipeline_detail_scroll =
                            if self.pipeline_detail_tab == PipelineDetailTab::Log {
                                usize::MAX
                            } else {
                                0
                            };
                        if self.pipeline_detail_tab == PipelineDetailTab::Log {
                            self.ensure_log_tab_sse();
                        }
                        needs_redraw = true;
                    }

                    // ── h/l — cycle Board detail tabs ────────────────────
                    // Board → Issue → Board Chat → Terminal → Board
                    // (l/Right = forward, h/Left = backward).
                    // #675: Terminal tab added as the 4th tab.
                    Key::Char('l') | Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Board
                            && !self.board_search.focused
                            && self.inject_chat.is_none()
                            && !self.detail_terminal_focused =>
                    {
                        self.board_detail_tab = match self.board_detail_tab {
                            BoardDetailTab::Board => BoardDetailTab::Issue,
                            BoardDetailTab::Issue => BoardDetailTab::Chat,
                            BoardDetailTab::Chat => BoardDetailTab::Terminal,
                            BoardDetailTab::Terminal => BoardDetailTab::Board,
                        };
                        self.detail_scroll = 0;
                        needs_redraw = true;
                    }
                    Key::Char('h') | Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Board
                            && !self.board_search.focused
                            && self.inject_chat.is_none()
                            && !self.detail_terminal_focused =>
                    {
                        self.board_detail_tab = match self.board_detail_tab {
                            BoardDetailTab::Board => BoardDetailTab::Terminal,
                            BoardDetailTab::Issue => BoardDetailTab::Board,
                            BoardDetailTab::Chat => BoardDetailTab::Issue,
                            BoardDetailTab::Terminal => BoardDetailTab::Chat,
                        };
                        self.detail_scroll = 0;
                        needs_redraw = true;
                    }

                    // ── #316: Board Chat tab CTA shortcuts (r = Refine, n = New Issue) ──
                    Key::Char('r') | Key::Char('R')
                        if self.active_view == SidebarView::Board
                            && self.board_detail_tab == BoardDetailTab::Chat
                            && self.inject_chat.is_none()
                            && self.pending_board_chat.is_none() =>
                    {
                        if let Some(repo) = self.board_active_repo().map(str::to_string) {
                            self.dispatch_board_chat_refine(&repo);
                        } else {
                            self.push_toast(
                                "No repo selected",
                                "Select a repo in the sidebar before starting a chat.",
                                ToastSeverity::Info,
                            );
                        }
                        needs_redraw = true;
                    }
                    Key::Char('n') | Key::Char('N')
                        if self.active_view == SidebarView::Board
                            && self.board_detail_tab == BoardDetailTab::Chat
                            && self.inject_chat.is_none()
                            && self.pending_board_chat.is_none() =>
                    {
                        if let Some(repo) = self.board_active_repo().map(str::to_string) {
                            self.dispatch_board_chat_new_issue(&repo);
                        } else {
                            self.push_toast(
                                "No repo selected",
                                "Select a repo in the sidebar before starting a chat.",
                                ToastSeverity::Info,
                            );
                        }
                        needs_redraw = true;
                    }

                    // ── #353: 'A' keybind mirrors the [A]dd toolbar button. ──
                    Key::Char('A')
                        if self.active_view == SidebarView::Board
                            && self.inject_chat.is_none()
                            && self.pending_board_chat.is_none()
                            && self.pending_repo_picker.is_none() =>
                    {
                        self.dispatch_toolbar_action("toolbar:add");
                        needs_redraw = true;
                    }

                    // ── Home ─────────────────────────────────────────────
                    Key::Named(NamedKey::Home) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let prev_sel = self.board_selected_issue();
                                self.board_sidebar.handle(&event, backend, list_b);
                                let new_sel = self.board_selected_issue();
                                if new_sel != prev_sel {
                                    self.detail_scroll = 0;
                                }
                            }
                            SidebarView::Machines => {
                                self.machine_sel = 0;
                                self.machine_detail_scroll = 0;
                                self.fix_machine_scroll(content_visible_rows(list_b, lh));
                            }
                            SidebarView::Pipeline => {
                                let prev = self.pipeline_sel;
                                self.pipeline_sidebar.handle(&event, backend, list_b);
                                self.pipeline_sel = self.selected_pipeline_index();
                                if self.pipeline_sel != prev {
                                    self.pipeline_focused_stage =
                                        self.default_focused_stage_for_selected_issue();
                                    self.pipeline_stage_content_scroll = 0;
                                }
                            }
                            SidebarView::Settings => {
                                // #237: jump to the first interactive field
                                // in the unified form.
                                self.settings_field_sel = 0;
                                self.settings_form.borrow_mut().set_scroll_offset(0);
                            }
                            // #424: Terminal — no nav target for Home.
                            SidebarView::Terminal => {}
                            // #638: Kanban — Home jumps to top of focused column.
                            SidebarView::Kanban => {
                                self.kanban_model.jump_to_top();
                                self.kanban_clamp_col_scroll();
                            }
                            // #737: MergeQueue — Home jumps to first entry.
                            SidebarView::MergeQueue => {
                                self.merge_queue_sel = 0;
                                self.fix_merge_queue_scroll(content_visible_rows(list_b, lh));
                            }
                            // #771: MilestoneDag — Home jumps to first milestone.
                            SidebarView::MilestoneDag => {
                                self.milestone_dag_sel = 0;
                            }
                            // #975: Plans — Home jumps to first plan.
                            SidebarView::Plans => {
                                self.plans_sel = 0;
                            }
                            // #1032: Sessions — no nav target for Home, same
                            // as Terminal (j/k tree-walk covers navigation).
                            SidebarView::Sessions => {}
                            // #1039: Audit — Home jumps to the first entry.
                            SidebarView::Audit => {
                                self.audit_sel = 0;
                                // #1094 fix: scroll back to the top too —
                                // otherwise row 0 is selected but still
                                // scrolled off-screen above the viewport.
                                self.audit_scroll = 0;
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── End ──────────────────────────────────────────────
                    Key::Named(NamedKey::End) => {
                        match self.active_view {
                            SidebarView::Board => {
                                let prev_sel = self.board_selected_issue();
                                self.board_sidebar.handle(&event, backend, list_b);
                                let new_sel = self.board_selected_issue();
                                if new_sel != prev_sel {
                                    self.detail_scroll = 0;
                                }
                            }
                            SidebarView::Machines => {
                                let m = self.data.machines.len();
                                if m > 0 {
                                    self.machine_sel = m - 1;
                                    self.machine_detail_scroll = 0;
                                }
                                self.fix_machine_scroll(content_visible_rows(list_b, lh));
                            }
                            SidebarView::Pipeline => {
                                let prev = self.pipeline_sel;
                                self.pipeline_sidebar.handle(&event, backend, list_b);
                                self.pipeline_sel = self.selected_pipeline_index();
                                if self.pipeline_sel != prev {
                                    self.pipeline_focused_stage =
                                        self.default_focused_stage_for_selected_issue();
                                    self.pipeline_stage_content_scroll = 0;
                                }
                            }
                            SidebarView::Settings => {
                                // #237: jump to the last interactive field
                                // in the unified form.
                                let count = self.settings_interactive_field_ids().len();
                                self.settings_field_sel = count.saturating_sub(1);
                            }
                            // #424: Terminal — no nav target for End.
                            SidebarView::Terminal => {}
                            // #638: Kanban — End jumps to bottom of focused column.
                            SidebarView::Kanban => {
                                self.kanban_model.jump_to_bottom();
                                self.kanban_clamp_col_scroll();
                            }
                            // #737: MergeQueue — End jumps to last entry.
                            SidebarView::MergeQueue => {
                                let n = self.data.merge_queue.len();
                                if n > 0 {
                                    self.merge_queue_sel = n - 1;
                                }
                                self.fix_merge_queue_scroll(content_visible_rows(list_b, lh));
                            }
                            // #771: MilestoneDag — End jumps to last milestone.
                            SidebarView::MilestoneDag => {
                                let n = self.milestone_dag_views().len();
                                if n > 0 {
                                    self.milestone_dag_sel = n - 1;
                                }
                            }
                            // #975: Plans — End jumps to last plan.
                            // #1001: bounded by the visible (rendered) roster.
                            SidebarView::Plans => {
                                let n = self.plans_visible_entries().len();
                                if n > 0 {
                                    self.plans_sel = n - 1;
                                }
                            }
                            // #1032: Sessions — no nav target for End, same
                            // as Terminal (j/k tree-walk covers navigation).
                            SidebarView::Sessions => {}
                            // #1039: Audit — End jumps to the last entry.
                            SidebarView::Audit => {
                                let n = self.audit_entries().len();
                                if n > 0 {
                                    self.audit_sel = n - 1;
                                }
                                // #1094 fix: scroll the last row into view.
                                self.fix_audit_scroll(content_visible_rows(
                                    ctx.main_bounds(),
                                    lh,
                                ));
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── Enter — Pipeline: Go fires the active stage.
                    Key::Named(NamedKey::Enter) if self.active_view == SidebarView::Pipeline => {
                        self.dispatch_pipeline_active_go();
                        needs_redraw = true;
                    }

                    // ── r — #296: report failure + dispatch fix worker ───────
                    // When the Test gate is actionable, `r` opens an inline
                    // text input.  On Enter, the failure is recorded AND
                    // `coord fix <work_id> --guidance <description>` is
                    // dispatched.  This shadows the "mark ready" binding
                    // below when the test gate is active — that's intentional
                    // (an issue in the Test stage can't usefully be "marked
                    // ready" simultaneously).
                    Key::Char('r')
                        if self.active_view == SidebarView::Pipeline
                            && self.test_gate_actionable()
                            && self.pending_test_fail.is_none()
                            && self.pending_report_fix.is_none() =>
                    {
                        if self.pipeline_selected_work_id().is_some() {
                            self.pending_report_fix = Some(String::new());
                            needs_redraw = true;
                        } else {
                            self.push_toast(
                                "No work assignment",
                                "Dispatch Work first before reporting a failure.",
                                ToastSeverity::Info,
                            );
                            needs_redraw = true;
                        }
                    }

                    // ── r — mark refined issue ready for dispatch ────────
                    // For an issue with status:refining (or status:backlog,
                    // or no status:* label), `r` spawns `coord ready` which
                    // sets status:ready via gh. After the GH side returns,
                    // the next data refresh moves the row into the Pending
                    // lifecycle section and the Pipeline tab shows [Go].
                    Key::Char('r') if self.active_view == SidebarView::Pipeline => {
                        // #249 Principle 2: every no-op gives feedback.
                        // Without these toasts, pressing `r` on an issue
                        // with no coord_repo mapping (or no selection)
                        // looks identical to pressing `r` on a working
                        // issue — both render nothing, and the user is
                        // left guessing.
                        let selected = self.pipeline_sel.and_then(|i| self.pipeline_issues.get(i));
                        match selected {
                            None => {
                                self.push_toast(
                                    "Nothing to mark ready",
                                    "Select an issue in the pipeline first.",
                                    ToastSeverity::Info,
                                );
                            }
                            Some(issue) if issue.coord_repo.is_none() => {
                                self.push_toast(
                                    "No coord_repo mapping",
                                    &format!(
                                        "{} isn't mapped in coordinator.yml — \
                                         add a `repos` entry so coord can act on it.",
                                        issue.repo_slug,
                                    ),
                                    ToastSeverity::Warning,
                                );
                            }
                            Some(issue) => {
                                let repo = issue.coord_repo.clone().unwrap();
                                let num = issue.number;
                                let num_str = num.to_string();
                                use crate::commands::SpawnQueuedOutcome;
                                match self
                                    .command_runner
                                    .spawn_queued(&["ready", &repo, &num_str])
                                {
                                    SpawnQueuedOutcome::Deduped => {}
                                    SpawnQueuedOutcome::Queued => {
                                        self.pipeline_status = Some((
                                            format!("#{}: ready queued", num),
                                            Instant::now(),
                                        ));
                                    }
                                    SpawnQueuedOutcome::Started => {
                                        self.pipeline_status = Some((
                                            format!("#{}: marking ready", num),
                                            Instant::now(),
                                        ));
                                    }
                                }
                            }
                        }
                        needs_redraw = true;
                    }

                    // ── PageDown (Board only) ─────────────────────────────
                    Key::Named(NamedKey::PageDown) if self.active_view == SidebarView::Board => {
                        let prev_sel = self.board_selected_issue();
                        self.board_sidebar.handle(&event, backend, list_b);
                        let new_sel = self.board_selected_issue();
                        if new_sel != prev_sel {
                            self.detail_scroll = 0;
                        }
                        needs_redraw = true;
                    }

                    // ── PageUp (Board only) ───────────────────────────────
                    Key::Named(NamedKey::PageUp) if self.active_view == SidebarView::Board => {
                        let prev_sel = self.board_selected_issue();
                        self.board_sidebar.handle(&event, backend, list_b);
                        let new_sel = self.board_selected_issue();
                        if new_sel != prev_sel {
                            self.detail_scroll = 0;
                        }
                        needs_redraw = true;
                    }

                    // ── PageDown (Audit only) ─────────────────────────────
                    // #1094 fix: the fix-iteration-1 report called out that
                    // PgDn didn't work as a keyboard fallback for reaching
                    // rows beyond the first screenful — it wasn't wired up
                    // at all. Pages the selection by a full viewport instead
                    // of one row at a time; `fix_audit_scroll` follows it
                    // into view same as `j`/`k`.
                    Key::Named(NamedKey::PageDown)
                        if self.active_view == SidebarView::Audit
                            && !self.audit_detail_open =>
                    {
                        let n = self.audit_entries().len();
                        if n > 0 {
                            let page = content_visible_rows(ctx.main_bounds(), lh).max(1);
                            self.audit_sel = (self.audit_sel + page).min(n - 1);
                        }
                        self.fix_audit_scroll(content_visible_rows(ctx.main_bounds(), lh));
                        needs_redraw = true;
                    }

                    // ── PageUp (Audit only) ───────────────────────────────
                    Key::Named(NamedKey::PageUp)
                        if self.active_view == SidebarView::Audit
                            && !self.audit_detail_open =>
                    {
                        let page = content_visible_rows(ctx.main_bounds(), lh).max(1);
                        self.audit_sel = self.audit_sel.saturating_sub(page);
                        self.fix_audit_scroll(content_visible_rows(ctx.main_bounds(), lh));
                        needs_redraw = true;
                    }

                    // ── Arrow cursor movement inside the search box ───────
                    Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Board && self.board_search.focused =>
                    {
                        self.board_search.cursor_left();
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }
                    Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Board && self.board_search.focused =>
                    {
                        self.board_search.cursor_right();
                        self.rebuild_board_sidebar();
                        needs_redraw = true;
                    }
                    Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_search.focused =>
                    {
                        self.pipeline_search.cursor_left();
                        self.rebuild_pipeline_sidebar(None);
                        needs_redraw = true;
                    }
                    Key::Named(NamedKey::Right)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_search.focused =>
                    {
                        self.pipeline_search.cursor_right();
                        self.rebuild_pipeline_sidebar(None);
                        needs_redraw = true;
                    }

                    // NOTE: This is the *default* `r` binding. View-specific
                    // overrides (Machines, Pipeline, Sessions) sit further
                    // down with their own `if self.active_view == …` guards
                    // and would be dead code without this exclusion — Rust
                    // evaluates match arms top-to-bottom, so an unguarded `r`
                    // here would silently shadow them.
                    Key::Char('r')
                        if self.active_view != SidebarView::Machines
                            && self.active_view != SidebarView::Sessions =>
                    {
                        self.refresh();
                        self.kick_issue_sync();
                        needs_redraw = true;
                    }

                    // ── #954: n — new terminal (Terminal-view machine picker) ─
                    // Must sit ABOVE the global `n` → notify binding below
                    // (which gains a `SidebarView::Terminal` exclusion), same
                    // top-to-bottom-shadowing discipline as the `r` arm above
                    // ('n' is otherwise taken globally by `notify`, per the
                    // #977 Plans-panel `c` binding's comment). Guarded on
                    // `!terminal_focused` too: while the embedded PTY has
                    // focus every key is passthrough (#424) and never reaches
                    // this match at all, but the guard documents the intent
                    // explicitly rather than relying on that upstream cutoff.
                    Key::Char('n')
                        if self.active_view == SidebarView::Terminal
                            && !self.terminal_focused =>
                    {
                        self.open_new_terminal_picker();
                        needs_redraw = true;
                    }

                    // ── S — force issue sync (Board panel) ───────────────
                    Key::Char('S')
                        if self.active_view == SidebarView::Board && !self.board_search.focused =>
                    {
                        self.force_issue_sync();
                        needs_redraw = true;
                    }

                    // ── Coordinator commands ─────────────────────────────
                    // #192: `p` / `a` / `A` are retired alongside the
                    // PROPOSALS section.  Right-click → Send to
                    // Pipeline (#261) is the canonical dispatch path
                    // now; `coord plan` still exists as a CLI escape
                    // hatch but doesn't earn a keybind.
                    // #954: excluded from the Terminal view — the guarded
                    // arm above claims 'n' there for "new terminal" instead.
                    Key::Char('n') if self.active_view != SidebarView::Terminal => {
                        use crate::commands::SpawnQueuedOutcome;
                        match self.command_runner.spawn_queued(&["notify"]) {
                            SpawnQueuedOutcome::Started => {
                                self.last_notify = Instant::now();
                            }
                            SpawnQueuedOutcome::Queued => {
                                self.push_toast(
                                    "⏳ Queued",
                                    "notify runs after current command",
                                    ToastSeverity::Info,
                                );
                            }
                            SpawnQueuedOutcome::Deduped => {}
                        }
                        needs_redraw = true;
                    }
                    Key::Char('m') => {
                        // #272-followup: route everything through the
                        // classifier so silent no-ops become actionable
                        // toasts (review blocked → toast; CI failed →
                        // open the force-merge prompt; ready → spawn
                        // `coord merge --repo <slug>`).
                        if self.dispatch_pipeline_merge_for_selected_issue() {
                            needs_redraw = true;
                        }
                    }
                    // #253: Capital M overrides the review-approval gate —
                    // active only when the selected merge entry is blocked
                    // on review so the keybind doesn't silently skip review
                    // on unrelated merges.
                    Key::Char('M') if self.merge_blocked_on_review_for_selected_issue() => {
                        use crate::commands::SpawnQueuedOutcome;
                        if let SpawnQueuedOutcome::Queued = self
                            .command_runner
                            .spawn_queued(&["merge", "--skip-review"])
                        {
                            self.push_toast(
                                "⏳ Queued",
                                "merge --skip-review runs after current command",
                                ToastSeverity::Info,
                            );
                        }
                        needs_redraw = true;
                    }
                    // ── 1–9 — Test stage: run smoke-test plan step ────────
                    // #349: When the Pipeline view is active, the test gate is
                    // actionable, and the test stage is focused, pressing a
                    // digit key runs the corresponding non-pull step from the
                    // generated smoke test plan via a background shell thread.
                    // Pull steps use [a] (handled below); "verify" steps are
                    // marked checked immediately without spawning a subprocess.
                    // key_num (1-indexed) maps to the key_num-th non-pull step.
                    Key::Char(ch)
                        if self.active_view == SidebarView::Pipeline
                            && self.test_gate_actionable()
                            && self.is_test_stage_focused()
                            && matches!(ch, '1'..='9') =>
                    {
                        let key_num = (*ch as u32 - '0' as u32) as usize; // 1..=9
                        if let Some(step_idx) = self.test_plan_runnable_step_idx(key_num) {
                            self.run_test_plan_step(step_idx);
                        }
                        needs_redraw = true;
                    }

                    // ── a (test stage) — pull step via [a] keybind ────────
                    // #349: When the test stage is focused and the plan contains
                    // a pull step, [a] triggers that step via `run_test_plan_step`
                    // (which now captures stdout/stderr).  This arm fires BEFORE
                    // the #336 artifact-badge arm so only one action is taken —
                    // preventing two concurrent pulls.
                    Key::Char('a')
                        if self.active_view == SidebarView::Pipeline
                            && self.test_gate_actionable()
                            && self.is_test_stage_focused()
                            && self.test_plan_pull_step_idx().is_some() =>
                    {
                        if let Some(pull_idx) = self.test_plan_pull_step_idx() {
                            self.run_test_plan_step(pull_idx);
                        }
                        needs_redraw = true;
                    }

                    // ── r — Machines: restart selected agent ─────────────
                    Key::Char('r') if self.active_view == SidebarView::Machines => {
                        if let Some(m) = self.data.machines.get(self.machine_sel) {
                            if m.active_count > 0 {
                                // Has active workers — require confirmation.
                                self.pending_restart = Some(m.name.clone());
                            } else {
                                let name = m.name.clone();
                                use crate::commands::SpawnQueuedOutcome;
                                if let SpawnQueuedOutcome::Queued = self
                                    .command_runner
                                    .spawn_queued(&["agent", "restart", "--machine", &name])
                                {
                                    self.push_toast(
                                        "⏳ Queued",
                                        "agent restart runs after current command",
                                        ToastSeverity::Info,
                                    );
                                }
                            }
                            needs_redraw = true;
                        }
                    }

                    // ── K — Terminal: kill selected terminal (#956) ───────
                    // Uppercase `K` is the fleet-wide "kill" convention (also
                    // used by the Sessions panel below, and formerly by the
                    // retired #628 live-sessions overlay). Always routes
                    // through the confirm dialog (terminals are persistent
                    // and may hold live work) — no-op when the selection is
                    // a machine row or nothing is selected.
                    Key::Char('K') if self.active_view == SidebarView::Terminal => {
                        if self.open_kill_terminal_confirm() {
                            needs_redraw = true;
                        }
                    }

                    // ── #1033: Sessions panel — attach / kill / stop ──────
                    // Mirrors the retired #628 live-sessions overlay's
                    // r=reattach / K=kill / f=stop keys, now scoped to the
                    // Sessions-tree's selected leaf. Each is a no-op (with a
                    // toast) when the selection isn't a session row.
                    Key::Char('r') | Key::Char('R')
                        if self.active_view == SidebarView::Sessions =>
                    {
                        if self.reattach_selected_fleet_session() {
                            needs_redraw = true;
                        } else {
                            self.push_toast(
                                "No session selected",
                                "Select a session leaf in the tree first.",
                                ToastSeverity::Info,
                            );
                        }
                    }
                    Key::Char('K') if self.active_view == SidebarView::Sessions => {
                        if self.open_kill_session_confirm() {
                            needs_redraw = true;
                        } else {
                            self.push_toast(
                                "No session selected",
                                "Select a session leaf in the tree first.",
                                ToastSeverity::Info,
                            );
                        }
                    }
                    Key::Char('f') | Key::Char('F')
                        if self.active_view == SidebarView::Sessions =>
                    {
                        if self.stop_selected_fleet_session() {
                            needs_redraw = true;
                        } else {
                            self.push_toast(
                                "No session selected",
                                "Select a session leaf in the tree first.",
                                ToastSeverity::Info,
                            );
                        }
                    }

                    // ── u — Machines: update selected agent ───────────────
                    Key::Char('u') if self.active_view == SidebarView::Machines => {
                        if let Some(m) = self.data.machines.get(self.machine_sel) {
                            let name = m.name.clone();
                            use crate::commands::SpawnQueuedOutcome;
                            if let SpawnQueuedOutcome::Queued = self.command_runner.spawn_queued(&[
                                "agent",
                                "update",
                                "--machine",
                                &name,
                            ]) {
                                self.push_toast(
                                    "⏳ Queued",
                                    "agent update runs after current command",
                                    ToastSeverity::Info,
                                );
                            }
                            needs_redraw = true;
                        }
                    }

                    // ── c — Machines: clean stale worktrees ───────────────
                    Key::Char('c') if self.active_view == SidebarView::Machines => {
                        if let Some(m) = self.data.machines.get(self.machine_sel) {
                            let name = m.name.clone();
                            use crate::commands::SpawnQueuedOutcome;
                            if let SpawnQueuedOutcome::Queued = self.command_runner.spawn_queued(&[
                                "agent",
                                "clean-worktrees",
                                "--machine",
                                &name,
                            ]) {
                                self.push_toast(
                                    "⏳ Queued",
                                    "agent clean-worktrees runs after current command",
                                    ToastSeverity::Info,
                                );
                            }
                            needs_redraw = true;
                        }
                    }

                    // ── p — Board: jump to the same issue in Pipeline view ───
                    // #815: When an issue row is selected in the Board, pressing
                    // `p` switches to the Pipeline panel and highlights that issue.
                    // No-op when the search filter has focus (to avoid stealing
                    // typed 'p' characters from the search box).
                    Key::Char('p')
                        if self.active_view == SidebarView::Board
                            && !self.board_search.focused =>
                    {
                        self.jump_board_to_pipeline();
                        needs_redraw = true;
                    }

                    // ── p — Machines: pause/unpause routing toggle ────────
                    Key::Char('p') if self.active_view == SidebarView::Machines => {
                        if let Some(m) = self.data.machines.get(self.machine_sel) {
                            let name = m.name.clone();
                            let is_paused = self.paused_machines.contains(&name);
                            let cmd = if is_paused { "unpause" } else { "pause" };
                            use crate::commands::SpawnQueuedOutcome;
                            let outcome =
                                self.command_runner.spawn_queued(&[cmd, &name]);
                            match outcome {
                                SpawnQueuedOutcome::Deduped => {}
                                SpawnQueuedOutcome::Queued => {
                                    let verb = if is_paused { "resume" } else { "pause" };
                                    self.push_toast(
                                        "Machine routing",
                                        &format!(
                                            "{}: {} queued — will run after current command.",
                                            name, verb
                                        ),
                                        ToastSeverity::Info,
                                    );
                                }
                                SpawnQueuedOutcome::Started => {
                                    // Optimistic local update so the badge
                                    // reflects the new state immediately.
                                    if is_paused {
                                        self.paused_machines.remove(&name);
                                    } else {
                                        self.paused_machines.insert(name.clone());
                                    }
                                    let verb = if is_paused { "resumed" } else { "paused" };
                                    self.push_toast(
                                        "Machine routing",
                                        &format!("{}: {}", name, verb),
                                        ToastSeverity::Info,
                                    );
                                }
                            }
                            needs_redraw = true;
                        }
                    }

                    Key::Char('R') => {
                        if self.active_view == SidebarView::Pipeline {
                            // #236: Test failed → R bounces back to a fresh
                            // Work dispatch (the Pipeline widget can't attach
                            // a [Retry] to a Failed Test, so without this the
                            // R keybind has no actionable target).
                            if self.can_bounce_work_after_test_fail() {
                                self.dispatch_pipeline_work();
                            } else if self.dispatch_pipeline_active_go() {
                                // In the Pipeline panel, R fires the active
                                // stage button — Retry on a Failed stage, or
                                // Go on a Pending one (same as Enter). When
                                // it dispatched something, we're done.
                            } else {
                                // #194: when no stage is actionable, fall back
                                // to an immediate refresh of pipeline issues
                                // from GitHub. Reset the last-load timestamp so
                                // maybe_kick_pipeline_loader() bypasses the 60 s
                                // guard. This matches the `R=refresh` hint
                                // shown in the Pipeline status bar.
                                self.maybe_kick_pipeline_loader();
                            }
                            needs_redraw = true;
                        } else if let Some(a) = self.board_selected_failed_assignment() {
                            let id = a.id.clone();
                            use crate::commands::SpawnQueuedOutcome;
                            if let SpawnQueuedOutcome::Queued =
                                self.command_runner.spawn_queued(&["retry", &id])
                            {
                                self.push_toast(
                                    "⏳ Queued",
                                    "retry runs after current command",
                                    ToastSeverity::Info,
                                );
                            }
                            needs_redraw = true;
                        } else {
                            // #249 Principle 2: explain the precondition
                            // for Board-view R so users don't think the
                            // keybind is broken.
                            self.push_toast(
                                "No failed assignment selected",
                                "Focus a row with status FAIL in the Board sidebar, then press R.",
                                ToastSeverity::Info,
                            );
                            needs_redraw = true;
                        }
                    }

                    // ── P — Purge done/failed assignments older than purge_days ──
                    // Only fires in the Board view when the cursor is in the
                    // Completed (done/merged) status group.  Opens a confirm
                    // prompt; the early-intercept block above handles 'y'/cancel.
                    Key::Char('P')
                        if self.active_view == SidebarView::Board
                            && !self.board_search.focused
                            && self.board_selection_in_completed_group() =>
                    {
                        let secs = self.purge_days as f64 * 86_400.0;
                        let counts = count_purgeable_db(secs).unwrap_or((0, 0));
                        self.pending_purge = Some(counts);
                        needs_redraw = true;
                    }

                    // ── D — Dismiss a Done-section pipeline issue (session-only) ──
                    // Hides the selected issue from the Done section for the lifetime
                    // of the current TUI session.  The issue reappears after a restart
                    // or if the user manually re-runs `gh` (i.e. this is in-memory only).
                    // Only fires when the selected issue is in the Done lifecycle section,
                    // preventing accidental dismissal of active work.
                    Key::Char('D') if self.active_view == SidebarView::Pipeline => {
                        if let Some(idx) = self.pipeline_sel {
                            if let Some(issue) = self.pipeline_issues.get(idx).cloned() {
                                if self.pipeline_lifecycle_section(&issue) == "done" {
                                    self.pipeline_dismissed
                                        .insert((issue.repo_slug.clone(), issue.number));
                                    // The dismissed issue is filtered out by
                                    // pipeline_groups_for_repo; pass None so the
                                    // rebuild lands on a sensible neighbor.
                                    self.rebuild_pipeline_sidebar(None);
                                    needs_redraw = true;
                                } else {
                                    self.pipeline_status = Some((
                                        format!(
                                            "D only dismisses Done issues (#{} is {})",
                                            issue.number,
                                            self.pipeline_lifecycle_section(&issue)
                                        ),
                                        Instant::now(),
                                    ));
                                    needs_redraw = true;
                                }
                            }
                        }
                    }

                    // ── #200 Test gate: P = Pass, F = Fail (reason), S = Skip ──
                    // Active in the Pipeline view when the selected issue's Test
                    // stage is Pending (Work is Done, no verdict yet).
                    Key::Char('P')
                        if self.active_view == SidebarView::Pipeline
                            && self.pending_test_fail.is_none()
                            && self.test_gate_actionable() =>
                    {
                        self.record_test_verdict("passed", None);
                        needs_redraw = true;
                    }
                    Key::Char('S')
                        if self.active_view == SidebarView::Pipeline
                            && self.pending_test_fail.is_none()
                            && self.test_gate_actionable() =>
                    {
                        self.record_test_verdict("skipped", None);
                        needs_redraw = true;
                    }
                    Key::Char('F')
                        if self.active_view == SidebarView::Pipeline
                            && self.pending_test_fail.is_none()
                            && self.test_gate_actionable() =>
                    {
                        // Open inline reason input. We need a stable handle for
                        // the work assignment in case the list reshuffles.
                        if let Some(_work_id) = self.pipeline_selected_work_id() {
                            // We carry an unused 0 as the first tuple slot —
                            // pipeline_selected_work_id() is re-resolved at
                            // submit time, so we don't need to cache the index.
                            self.pending_test_fail = Some((0, String::new()));
                            needs_redraw = true;
                        }
                    }

                    // #bounce: lowercase f = bounce the pipeline back
                    // to a fix worker when the selected Pipeline row has
                    // a request-changes review.  Uppercase F is the
                    // Test-fail key (handled above) and only fires when
                    // the test gate is actionable, so the two don't
                    // overlap in practice.
                    Key::Char('f')
                        if self.active_view == SidebarView::Pipeline
                            && self.selected_pipeline_review_id_for_bounce().is_some() =>
                    {
                        self.dispatch_bounce_for_selected_pipeline_row();
                        needs_redraw = true;
                    }

                    // ── #235 Phase 1: B = build (fetch + checkout +
                    //              build_command on the local machine) ──
                    // Spawns `coord test <work_id>` in a background thread
                    // and toasts the outcome. Manual trigger by design —
                    // auto-on-completion would clobber the user's working
                    // copy mid-edit.
                    Key::Char('B')
                        if self.pending_test_fail.is_none() && self.can_trigger_test_build() =>
                    {
                        if let Some(work_id) = self.pipeline_selected_work_id() {
                            let (branch, issue_number) = self
                                .data
                                .assignments
                                .iter()
                                .find(|a| a.id == work_id)
                                .and_then(|a| a.branch.clone().map(|b| (b, a.issue_number)))
                                .unwrap_or_else(|| (String::from("?"), 0));
                            self.spawn_test_build(work_id, branch, issue_number);
                            needs_redraw = true;
                        }
                    }

                    // ── T — Pipeline / Test stage: open test-chat ─────────
                    // #314 Phase B: spawn `coord test-chat <work_id>` and arm
                    // the bind poll so the chat overlay opens once the
                    // assignment row appears in the DB.
                    Key::Char('T')
                        if self.active_view == SidebarView::Pipeline
                            && self.test_gate_actionable()
                            && self.pipeline_selected_work_id().is_some() =>
                    {
                        if self.spawn_test_chat() {
                            needs_redraw = true;
                        }
                    }

                    // ── #336 / #532: a — pull artifacts or re-open dialog ──
                    // Routing lives in `compute_a_key_artifact_action` so
                    // tests drive the same decision the handler does.
                    // No-op when the fetch is still in-flight or no work
                    // assignment is visible.
                    Key::Char('a')
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        match self.compute_a_key_artifact_action() {
                            Some(AKeyArtifactAction::ReopenDialog(dlg))
                            | Some(AKeyArtifactAction::ShowAbsence(dlg)) => {
                                self.artifact_pull_dialog = Some(dlg);
                            }
                            Some(AKeyArtifactAction::StartPull {
                                work_id,
                                repo,
                                sanitized,
                            }) => {
                                use crate::commands::SpawnQueuedOutcome;
                                let outcome = self
                                    .command_runner
                                    .spawn_queued(&["pull-artifact", &work_id]);
                                if outcome != SpawnQueuedOutcome::Deduped {
                                    self.pending_artifact_pull =
                                        Some((work_id, repo, sanitized));
                                    let status_msg = if outcome == SpawnQueuedOutcome::Queued {
                                        "Pull artifacts queued…"
                                    } else {
                                        "Pulling artifacts…"
                                    };
                                    self.pipeline_status =
                                        Some((status_msg.into(), Instant::now()));
                                }
                            }
                            None => {}
                        }
                        needs_redraw = true;
                    }

                    _ => {}
                }
            }

            UiEvent::WindowResized { .. } => {
                needs_redraw = true;
            }

            _ => {}
        }

        if needs_redraw {
            Reaction::Redraw
        } else {
            Reaction::Continue
        }
    }

    // ── Mouse dispatch ────────────────────────────────────────────────────

    /// Dispatch one mouse event. Called from `handle()` before the keyboard
    /// match so we can still pass `&UiEvent` to `board_tree.handle()`.
    /// Returns `true` if a redraw is needed.
    ///
    /// Uses [`ShellContext::in_sidebar`] / [`ShellContext::in_main`] to
    /// route between the sidebar list and the main detail panel.
    pub(crate) fn handle_mouse(
        &mut self,
        event: &UiEvent,
        backend: &mut dyn Backend,
        ctx: &ShellContext,
    ) -> bool {
        match event {
            UiEvent::MouseDown {
                position,
                button: MouseButton::Left,
                modifiers,
                ..
            } => {
                let pos = *position;
                let lh = backend.line_height();
                // #369/#329: an open prompt dialog intercepts all clicks
                // first (highest z-order) — outside → dismiss; on a
                // button → fire action; inside body → swallow.
                if let Some(handled) = self.handle_dialog_click(pos, backend) {
                    return handled;
                }
                // #259: an open context menu intercepts all clicks next
                // — outside the menu → dismiss; on an item → activate;
                // anywhere else inside the menu → swallow (keep open).
                if let Some(handled) = self.handle_context_menu_click(pos) {
                    return handled;
                }
                if ctx.in_sidebar(pos.x, pos.y) {
                    if let Some(sidebar_b) = ctx.sidebar_bounds() {
                        return self.mouse_sidebar_click(event, pos, sidebar_b, backend);
                    }
                    false
                } else if ctx.in_main(pos.x, pos.y) {
                    let main_b = ctx.main_bounds();
                    let char_w = backend.char_width();
                    // #646 focus-follows-click: clicking the terminal content area focuses it.
                    if self.active_view == SidebarView::Terminal && !self.terminal_focused {
                        self.terminal_focused = true;
                    }
                    if self.active_view == SidebarView::Pipeline
                        && self.pipeline_detail_tab == PipelineDetailTab::Terminal
                    {
                        // Only focus when clicking below the tab bar (the terminal content).
                        let tab_h = detail_tab_bar_height(lh);
                        if pos.y - main_b.y >= tab_h && !self.detail_terminal_focused {
                            self.detail_terminal_focused = true;
                        }
                    }
                    // #675: Board Terminal tab — same focus-follows-click as Pipeline Terminal.
                    if self.active_view == SidebarView::Board
                        && self.board_detail_tab == BoardDetailTab::Terminal
                    {
                        let tab_h = detail_tab_bar_height(lh);
                        if pos.y - main_b.y >= tab_h && !self.detail_terminal_focused {
                            self.detail_terminal_focused = true;
                        }
                    }
                    // #464: host-side selection — must check BEFORE the PTY
                    // forwarding path so Shift can override even when the app
                    // has mouse reporting on (e.g. vim visual mode).
                    //
                    // Two cases both route to host selection:
                    //   1. Shift held → always host-select (standard terminal
                    //      override convention; overrides vim/tmux/less).
                    //   2. Mouse reporting OFF → forward_mouse returns false
                    //      anyway; start selection here so we own the drag.
                    //
                    // For case 2 we need to peek at the session's reporting
                    // state without consuming the event — read the flag, then
                    // branch.
                    // Compute cell coordinates once; reuse for both the
                    // reporting-state peek and the host-select branch (avoids
                    // a redundant coordinate translation on every mouse-down).
                    let cr = self.active_terminal_pixel_to_cell(pos, main_b, lh, char_w);
                    let reporting_on = cr.is_some() && {
                        // Only peek when there's actually a session.
                        match self.active_view {
                            SidebarView::Terminal => self
                                .standalone_pty_session()
                                .map(|s| s.mouse_reporting_enabled())
                                .unwrap_or(false),
                            SidebarView::Pipeline
                                if self.pipeline_detail_tab
                                    == PipelineDetailTab::Terminal =>
                            {
                                self.selected_issue_key()
                                    .and_then(|k| {
                                        self.detail_terminal_sessions
                                            .get(&k)
                                            .map(|s| s.mouse_reporting_enabled())
                                    })
                                    .unwrap_or(false)
                            }
                            _ => false,
                        }
                    };
                    // #790: copy mode (F9) also forces host selection so a
                    // plain drag selects text instead of reaching the PTY —
                    // the tmux-proof path when Shift is eaten by an outer tmux.
                    let force_host_sel =
                        self.terminal_should_host_select(modifiers.shift, reporting_on);
                    if force_host_sel {
                        if let Some((col, row)) = cr {
                            self.terminal_host_sel_begin(col, row);
                            return true;
                        }
                    }
                    // #454: Forward click to the embedded PTY when mouse
                    // reporting is enabled. Returns true only if the PTY
                    // consumed it (i.e. mouse reporting is on); fall through
                    // to normal TUI click handling otherwise.
                    if self.terminal_mouse_event(
                        TerminalMouseKind::Press,
                        MouseButton::Left,
                        pos,
                        *modifiers,
                        main_b,
                        lh,
                        char_w,
                    ) {
                        // Remember the press so the matching `Release`
                        // fires even if the user drags out of the panel
                        // before releasing (#454 review fix).
                        self.pty_pressed_buttons |= pty_button_bit(MouseButton::Left);
                        return true;
                    }
                    self.mouse_main_click(pos, main_b, lh)
                } else {
                    false
                }
            }
            UiEvent::MouseDown {
                position,
                button: MouseButton::Right,
                modifiers,
                ..
            } => {
                // #259: right-click opens a context menu for the row
                // under the cursor (Board / Pipeline sidebar only for
                // MVP).  We synthesise a left-click first so the row
                // gets focused / selected, then open the menu using the
                // newly-selected row as the target.
                let pos = *position;
                let modifiers = *modifiers;
                if ctx.in_sidebar(pos.x, pos.y) {
                    if let Some(sidebar_b) = ctx.sidebar_bounds() {
                        // Pre-select the row under the cursor by routing
                        // a left-click; existing handlers already update
                        // selection state and re-rebuild the sidebar.
                        let synthetic_left = UiEvent::MouseDown {
                            widget: None,
                            button: MouseButton::Left,
                            position: pos,
                            modifiers: quadraui::Modifiers::default(),
                        };
                        self.mouse_sidebar_click(&synthetic_left, pos, sidebar_b, backend);
                    }
                    // Synthetic-left above already moved the selection to the
                    // row under the cursor, so the shared selection-based
                    // target builder reflects the clicked row.
                    let target = self.context_menu_target_for_selection();
                    if let Some(target) = target {
                        if self.open_context_menu(pos, target) {
                            return true;
                        }
                    }
                } else if ctx.in_main(pos.x, pos.y) {
                    // #1003 fix-up: the Plans-panel roster lives in the MAIN
                    // panel, not the sidebar (unlike Board/Pipeline/Machines,
                    // handled above) — the sidebar-only branch above left
                    // every Plans right-click a silent no-op, so the CRUD
                    // context menu this issue adds was unreachable by mouse.
                    // Pre-select the row under the cursor (mirrors the
                    // synthetic-left-click pattern above) before resolving
                    // the target from the now-current selection.
                    if self.active_view == SidebarView::Plans {
                        let main_b = ctx.main_bounds();
                        let lh = backend.line_height();
                        if let Some(idx) = self.plans_row_at(pos, main_b, lh) {
                            self.plans_sel = idx;
                        }
                        if let Some(target) = self.context_menu_target_for_selection() {
                            if self.open_context_menu(pos, target) {
                                return true;
                            }
                        }
                    }
                    // #454: Forward right-click Press to the embedded PTY when
                    // mouse reporting is enabled.  Without this the PTY would
                    // receive an orphaned Release (from the MouseUp arm) with
                    // no corresponding Press, breaking right-click in apps
                    // such as vim or tmux.
                    let main_b = ctx.main_bounds();
                    let char_w = backend.char_width();
                    let lh = backend.line_height();
                    if self.terminal_mouse_event(
                        TerminalMouseKind::Press,
                        MouseButton::Right,
                        pos,
                        modifiers,
                        main_b,
                        lh,
                        char_w,
                    ) {
                        // Mirror the Left-button path: remember the press
                        // so the matching `Release` fires even if the
                        // user releases outside the panel (#454 fix-2).
                        self.pty_pressed_buttons |= pty_button_bit(MouseButton::Right);
                        return true;
                    }
                }
                false
            }

            UiEvent::Scroll {
                position, delta, ..
            } => {
                let pos = *position;
                let d = *delta;
                let lh = backend.line_height();
                let char_w = backend.char_width();
                if ctx.in_sidebar(pos.x, pos.y) {
                    if let Some(sidebar_b) = ctx.sidebar_bounds() {
                        return self.mouse_sidebar_scroll(event, d, sidebar_b, backend, lh);
                    }
                    false
                } else if ctx.in_main(pos.x, pos.y) {
                    self.mouse_main_scroll(d, pos, ctx.main_bounds(), lh, char_w)
                } else {
                    false
                }
            }

            // #272: drive ToolbarHoverTracker from MouseMoved so the
            // hovered toolbar button gets a background tint without the
            // host having to track button bounds across frames.  A
            // change in the hovered id triggers a redraw.
            UiEvent::MouseMoved { .. } => {
                let (pos, buttons) = if let UiEvent::MouseMoved { position, buttons } = event {
                    (*position, *buttons)
                } else {
                    return false;
                };
                let lh = backend.line_height();
                let mut redraw = false;
                // Forward to the active sidebar so scrollbar drag tracks the cursor.
                // SidebarSystem.handle(MouseMoved) calls drag_to() internally and
                // returns Consumed when scroll state changed — use that to trigger redraw.
                if let Some(sidebar_b) = ctx.sidebar_bounds() {
                    let result = match self.active_view {
                        SidebarView::Board => self.board_sidebar.handle(event, backend, sidebar_b),
                        SidebarView::Pipeline => {
                            self.pipeline_sidebar.handle(event, backend, sidebar_b)
                        }
                        _ => SidebarEvent::Ignored,
                    };
                    if result != SidebarEvent::Ignored {
                        redraw = true;
                    }
                }
                if ctx.in_sidebar(pos.x, pos.y) {
                    if let Some(sidebar_b) = ctx.sidebar_bounds() {
                        let panel = self.build_sidebar_action_panel(lh);
                        let layout = panel.layout(
                            sidebar_b,
                            quadraui::SidebarPanelMeasure::new(lh, 8.0),
                            toolbar_tui_measure,
                        );
                        if let Some(t) = layout.toolbar_layout.as_ref() {
                            redraw |= self.sidebar_action_bar_hover.update(t, pos.x, pos.y);
                        } else {
                            redraw |= self.sidebar_action_bar_hover.clear();
                        }
                        redraw |= self.panel_toolbar_hover.clear();
                    }
                } else if ctx.in_main(pos.x, pos.y) {
                    // #464: if a host-side selection drag is in progress,
                    // extend the selection to the current cell and redraw.
                    // This takes priority over the PTY motion path so the
                    // drag doesn't accidentally escape to the PTY.
                    let char_w = backend.char_width();
                    let main_b = ctx.main_bounds();
                    // #1094: continue an in-progress Audit column-resize
                    // drag before any other main-panel motion handling.
                    // `active_view == Audit` already implies the terminal
                    // paths below are no-ops, but checking explicitly keeps
                    // the precedence self-documenting.
                    if self.active_view == SidebarView::Audit && buttons.left {
                        redraw |= self.audit_update_resize_drag(pos, main_b);
                        // #1094 fix: continue an in-progress scrollbar
                        // track drag (started by a `MouseDown` inside
                        // `audit_scrollbar_hit`'s region — see
                        // `mouse_main_click` below), same precedence as the
                        // column-resize drag just above.
                        if let Some(axis) = self.audit_scrollbar_drag {
                            redraw |= match axis {
                                AuditScrollAxis::Vertical => {
                                    self.audit_apply_vscroll(pos, main_b)
                                }
                                AuditScrollAxis::Horizontal => {
                                    self.audit_apply_hscroll(pos, main_b)
                                }
                            };
                        }
                    }
                    if self.terminal_host_sel_dragging && buttons.left {
                        if let Some((col, row)) =
                            self.active_terminal_pixel_to_cell(pos, main_b, lh, char_w)
                        {
                            self.terminal_host_sel_update(col, row);
                            redraw = true;
                        }
                    } else if buttons.left || buttons.right || buttons.middle {
                        // #454: forward cursor motion to the embedded PTY when
                        // mouse-reporting mode is active.  `forward_mouse`
                        // returns false when reporting is off, so there is no
                        // performance cost on the common idle path.
                        //
                        // Plain hover (no button held) is intentionally NOT
                        // forwarded: under the xterm "button-event tracking"
                        // protocol (mode 1002), a motion event always carries
                        // a button bit, so reporting Left for hover looks
                        // identical to a Left-drag — and would trigger vim
                        // visual selection, tmux copy-mode drag, ranger
                        // selection, etc. on plain cursor movement.  We
                        // forward Move only when at least one button is
                        // actually held (#454 review fix).
                        let btn = if buttons.right {
                            MouseButton::Right
                        } else if buttons.middle {
                            MouseButton::Middle
                        } else {
                            // buttons.left is true by the outer guard.
                            MouseButton::Left
                        };
                        redraw |= self.terminal_mouse_event(
                            TerminalMouseKind::Move,
                            btn,
                            pos,
                            Modifiers::default(),
                            main_b,
                            lh,
                            char_w,
                        );
                    }
                    // Note: intentional fall-through — toolbar hover tracking
                    // below still runs even when the PTY consumed the move.
                    if let Some(toolbar) = self.panel_toolbar() {
                        let panel = SidebarPanel {
                            id: WidgetId::new("panel-toolbar"),
                            toolbar: Some(toolbar),
                            toolbar_height: Some(self.toolbar_height(lh)),
                        };
                        let layout = panel.layout(
                            ctx.main_bounds(),
                            quadraui::SidebarPanelMeasure::new(lh, 8.0),
                            toolbar_tui_measure,
                        );
                        if let Some(t) = layout.toolbar_layout.as_ref() {
                            redraw |= self.panel_toolbar_hover.update(t, pos.x, pos.y);
                        } else {
                            redraw |= self.panel_toolbar_hover.clear();
                        }
                    } else {
                        redraw |= self.panel_toolbar_hover.clear();
                    }
                    // #438: hover tracking for the pipeline action bar
                    // ([ Go ⏎ ] / [ Retry ⏎ ] strip below the tab row).
                    // Pipeline has no panel toolbar so content starts at
                    // main_b.y + tab_h — compute bar_rect the same way
                    // the render path does and feed it to `toolbar_layout`.
                    if self.active_view == SidebarView::Pipeline
                        && self.pipeline_detail_tab == PipelineDetailTab::Overview
                    {
                        if let Some(action_toolbar) = self.pipeline_action_bar_toolbar() {
                            let main_b = ctx.main_bounds();
                            let tab_h = detail_tab_bar_height(lh);
                            let bar_h = pipeline_action_bar_height(true, lh);
                            let bar_rect =
                                Rect::new(main_b.x, main_b.y + tab_h, main_b.width, bar_h);
                            let layout = backend.toolbar_layout(bar_rect, &action_toolbar);
                            redraw |= self.pipeline_action_bar_hover.update(&layout, pos.x, pos.y);
                        } else {
                            redraw |= self.pipeline_action_bar_hover.clear();
                        }
                    } else {
                        redraw |= self.pipeline_action_bar_hover.clear();
                    }
                    redraw |= self.sidebar_action_bar_hover.clear();
                } else {
                    redraw |= self.sidebar_action_bar_hover.clear();
                    redraw |= self.panel_toolbar_hover.clear();
                    redraw |= self.pipeline_action_bar_hover.clear();
                }
                redraw
            }

            // Forward MouseUp to the active sidebar to release any scrollbar drag,
            // and forward the release to the embedded PTY when mouse reporting is on.
            UiEvent::MouseUp { button, position, .. } => {
                let pos = *position;
                let btn = *button;
                // #1094: release an in-progress Audit column-resize drag
                // (started by a `MouseDown` on a `DataTableHit::
                // HeaderDivider`) before any other `MouseUp` handling —
                // mirrors the #464 host-selection-drag finalize priority
                // just below.
                // #1094 fix: release an in-progress scrollbar-track drag
                // the same way — started by a `MouseDown` inside
                // `audit_scrollbar_hit`'s region (`mouse_main_click` below).
                if btn == MouseButton::Left
                    && (self.audit_resize_col.take().is_some()
                        || self.audit_scrollbar_drag.take().is_some())
                {
                    return true;
                }
                if let Some(sidebar_b) = ctx.sidebar_bounds() {
                    match self.active_view {
                        SidebarView::Board => {
                            self.board_sidebar.handle(event, backend, sidebar_b);
                        }
                        SidebarView::Pipeline => {
                            self.pipeline_sidebar.handle(event, backend, sidebar_b);
                        }
                        _ => {}
                    }
                }
                // #464: finalise a host-side selection drag on release.
                // Must run before the PTY forwarding path so we don't also
                // send a spurious Release to the PTY.
                if self.terminal_host_sel_dragging && btn == MouseButton::Left {
                    self.terminal_host_sel_end();
                    return true;
                }
                // #454: forward button release to the embedded PTY when mouse
                // reporting is enabled (gated inside forward_mouse itself).
                //
                // Two paths into the release:
                //   - Cursor still in_main → normal, position-driven
                //     forward via `terminal_mouse_event`.
                //   - Cursor outside in_main but we have an outstanding
                //     PTY press for this button → the user dragged out of
                //     the panel; force-forward with the position clamped
                //     to the PTY rect so the terminal app gets its
                //     matching Release (vim visual mode, tmux drag, less
                //     would otherwise stay "button held" forever).
                let bit = pty_button_bit(btn);
                let press_outstanding = bit != 0 && (self.pty_pressed_buttons & bit) != 0;
                let lh = backend.line_height();
                let char_w = backend.char_width();
                let main_b = ctx.main_bounds();
                let in_main = ctx.in_main(pos.x, pos.y);
                let mut consumed = false;
                if in_main {
                    consumed = self.terminal_mouse_event(
                        TerminalMouseKind::Release,
                        btn,
                        pos,
                        Modifiers::default(),
                        main_b,
                        lh,
                        char_w,
                    );
                }
                if press_outstanding && !consumed {
                    // Out-of-bounds release: clamp position to the PTY
                    // content rect and force-forward so the PTY sees the
                    // matching Release.  `terminal_force_release` handles
                    // both the standalone and Pipeline/Terminal sessions.
                    consumed = self.terminal_force_release(btn, pos, main_b, lh, char_w);
                }
                if press_outstanding {
                    self.pty_pressed_buttons &= !bit;
                }
                if consumed {
                    return true;
                }
                false
            }

            _ => false,
        }
    }

    /// Click in the sidebar (board sidebar system / machines list,
    /// depending on the active view).
    pub(crate) fn mouse_sidebar_click(
        &mut self,
        event: &UiEvent,
        pos: Point,
        sidebar_b: Rect,
        backend: &mut dyn Backend,
    ) -> bool {
        // #270: action bar (row of contextual verb buttons) sits above
        // the tree.  Hit-test it first; if the click landed on a button
        // we've already dispatched the action.  Pass the shrunken rect
        // to the tree's hit-tester so its math doesn't see the bar row.
        let lh = backend.line_height();
        let (sidebar_b, consumed) = self.hit_test_sidebar_action_bar(pos, sidebar_b, lh);
        let _ = backend; // backend reserved for hover updates wired below
        if consumed {
            return true;
        }
        // #646 focus-follows-click: any sidebar click blurs the terminal.
        if self.terminal_focused || self.detail_terminal_focused {
            self.terminal_focused = false;
            self.detail_terminal_focused = false;
        }
        match self.active_view {
            SidebarView::Board => {
                let result = self.board_sidebar.handle(event, backend, sidebar_b);
                match result {
                    SidebarEvent::RowSelected { section, ref path } => {
                        // #646 focus-follows-click: row click blurs the search filter.
                        if self.board_search.focused {
                            self.board_search.focused = false;
                            self.board_sidebar.focus_form(0, false);
                        }
                        if path.len() == 1 {
                            // #410: click on a milestone header — toggle milestone expansion.
                            let offset = self.board_repo_offset();
                            if section >= offset {
                                let repo_idx = section - offset;
                                if let Some(repo) = self.board_repo_names.get(repo_idx).cloned() {
                                    let milestone_idx = path[0] as usize;
                                    let cache = self.board_issues_cache.clone();
                                    let milestones = self.board_milestones_for_repo(&cache, &repo);
                                    if let Some((m_key, _, group_issues)) =
                                        milestones.get(milestone_idx)
                                    {
                                        let m_key = m_key.clone();
                                        // #857: first click on an untouched key must
                                        // invert the default it was actually painted
                                        // with (in-flight ⇒ expanded), not a hardcoded
                                        // `true` — else a non-in-flight (collapsed)
                                        // milestone's first click would insert `true`
                                        // and immediately negate it back to `false`,
                                        // silently no-op'ing the click.
                                        let default_expanded =
                                            Self::board_milestone_has_inflight(group_issues);
                                        let entry = self
                                            .board_milestone_expanded
                                            .entry((repo, m_key))
                                            .or_insert(default_expanded);
                                        *entry = !*entry;
                                        self.rebuild_board_sidebar();
                                    }
                                }
                            }
                        } else {
                            // path.len() == 2: issue row — reset detail scroll.
                            self.detail_scroll = 0;
                        }
                        true
                    }
                    SidebarEvent::RowActivated { section, ref path } => {
                        // #646 focus-follows-click: row activate blurs the search filter.
                        if self.board_search.focused {
                            self.board_search.focused = false;
                            self.board_sidebar.focus_form(0, false);
                        }
                        if path.len() == 1 {
                            // Activate on a milestone header — toggle expansion.
                            let offset = self.board_repo_offset();
                            if section >= offset {
                                let repo_idx = section - offset;
                                if let Some(repo) = self.board_repo_names.get(repo_idx).cloned() {
                                    let milestone_idx = path[0] as usize;
                                    let cache = self.board_issues_cache.clone();
                                    let milestones = self.board_milestones_for_repo(&cache, &repo);
                                    if let Some((m_key, _, group_issues)) =
                                        milestones.get(milestone_idx)
                                    {
                                        let m_key = m_key.clone();
                                        // #857: first click on an untouched key must
                                        // invert the default it was actually painted
                                        // with (in-flight ⇒ expanded), not a hardcoded
                                        // `true` — else a non-in-flight (collapsed)
                                        // milestone's first click would insert `true`
                                        // and immediately negate it back to `false`,
                                        // silently no-op'ing the click.
                                        let default_expanded =
                                            Self::board_milestone_has_inflight(group_issues);
                                        let entry = self
                                            .board_milestone_expanded
                                            .entry((repo, m_key))
                                            .or_insert(default_expanded);
                                        *entry = !*entry;
                                        self.rebuild_board_sidebar();
                                    }
                                }
                            }
                        } else {
                            // path.len() == 2: issue row activate — reset detail scroll.
                            self.detail_scroll = 0;
                        }
                        true
                    }
                    SidebarEvent::HeaderActivated { section: _ } => true,
                    SidebarEvent::FormEvent {
                        section: 0,
                        event: FormEvent::TextInputChanged { ref value, .. },
                    } => {
                        self.board_search.set_value(value);
                        self.rebuild_board_sidebar();
                        true
                    }
                    // Click on the filter TextInput focuses it (emits FocusChanged).
                    SidebarEvent::FormEvent {
                        section: 0,
                        event: FormEvent::FocusChanged { .. },
                    } => {
                        self.board_search.focused = true;
                        self.board_sidebar.focus_form(0, true);
                        true
                    }
                    // #410: Chevron click on a milestone header row (only depth-1 rows are headers).
                    SidebarEvent::RowToggleExpand { section, ref path } if path.len() == 1 => {
                        let offset = self.board_repo_offset();
                        if section >= offset {
                            let repo_idx = section - offset;
                            if let Some(repo) = self.board_repo_names.get(repo_idx).cloned() {
                                let cache = self.board_issues_cache.clone();
                                let milestones = self.board_milestones_for_repo(&cache, &repo);
                                let milestone_idx = path[0] as usize;
                                if let Some((m_key, _, group_issues)) =
                                    milestones.get(milestone_idx)
                                {
                                    let m_key = m_key.clone();
                                    // #857: match the painted default (see the
                                    // RowSelected/RowActivated arms above) so a
                                    // chevron click never silently no-ops.
                                    let default_expanded =
                                        Self::board_milestone_has_inflight(group_issues);
                                    let entry = self
                                        .board_milestone_expanded
                                        .entry((repo, m_key))
                                        .or_insert(default_expanded);
                                    *entry = !*entry;
                                    self.rebuild_board_sidebar();
                                }
                            }
                        }
                        true
                    }
                    SidebarEvent::StateChanged
                    | SidebarEvent::Consumed
                    | SidebarEvent::ScrollChanged { .. }
                    | SidebarEvent::FormEvent { .. }
                    | SidebarEvent::RowToggleExpand { .. } => true,
                    _ => false,
                }
            }
            SidebarView::Machines => {
                // Row 0 = title strip; item i starts at row 1+i-scroll.
                let row = (pos.y - sidebar_b.y).max(0.0) as usize;
                if row >= 1 {
                    let item_idx = (row - 1) + self.machine_scroll;
                    let m = self.data.machines.len();
                    if item_idx < m && item_idx != self.machine_sel {
                        self.machine_sel = item_idx;
                        self.machine_detail_scroll = 0;
                        return true;
                    }
                }
                false
            }
            // #953: the Terminal-view tree is a raw `TreeView` (not
            // `SidebarSystem`), so — like Machines above — click dispatch
            // uses flat pixel-row math rather than a `SidebarEvent`. No
            // title row (unlike Machines' `ListView.title`), so row 0 maps
            // directly to the first tree row.
            SidebarView::Terminal => {
                if pos.y < sidebar_b.y {
                    return false;
                }
                let row = ((pos.y - sidebar_b.y) / lh).floor() as usize + self.terminal_tree_scroll;
                self.terminal_tree_click_row(row)
            }
            SidebarView::Pipeline => {
                let prev = self.pipeline_sel;
                let result = self.pipeline_sidebar.handle(event, backend, sidebar_b);
                self.pipeline_sel = self.selected_pipeline_index();
                if self.pipeline_sel != prev {
                    self.pipeline_focused_stage = self.default_focused_stage_for_selected_issue();
                    self.pipeline_stage_content_scroll = 0;
                }
                match result {
                    SidebarEvent::FormEvent {
                        section: 0,
                        event: FormEvent::TextInputChanged { ref value, .. },
                    } => {
                        self.pipeline_search.set_value(value);
                        self.rebuild_pipeline_sidebar(None);
                        true
                    }
                    // Click on the filter TextInput focuses it (emits FocusChanged).
                    SidebarEvent::FormEvent {
                        section: 0,
                        event: FormEvent::FocusChanged { .. },
                    } => {
                        self.pipeline_search.focused = true;
                        self.pipeline_sidebar.focus_form(0, true);
                        true
                    }
                    SidebarEvent::RowToggleExpand { section, ref path } if path.len() == 1 => {
                        // A one-level path = a repo/liveness sub-header was toggled.
                        // New/Done group by repo; Active groups by liveness (Live/Idle).
                        // Both persist expand state in pipeline_lifecycle_expanded
                        // keyed by (lc_key, group_key).
                        let search_offset = 1usize;
                        if section >= search_offset {
                            let state_idx = section - search_offset;
                            if let Some(&lc_key) = self.pipeline_state_section_names.get(state_idx)
                            {
                                let groups = if lc_key == "in-progress" {
                                    self.pipeline_active_by_liveness()
                                } else {
                                    self.pipeline_repos_for_state(lc_key)
                                };
                                let gi = path[0] as usize;
                                if let Some((group_key, _)) = groups.get(gi) {
                                    let group_key = group_key.clone();
                                    let entry = self
                                        .pipeline_lifecycle_expanded
                                        .entry((lc_key.to_string(), group_key))
                                        .or_insert(true);
                                    *entry = !*entry;
                                    self.rebuild_pipeline_sidebar(None);
                                }
                            }
                        }
                        true
                    }
                    // #1197: an epic row is a branch too — collapsing it hides
                    // just its own children, leaving the epic row and its
                    // milestone siblings visible (previously the only way to
                    // hide an epic's children was collapsing the whole
                    // milestone, which hid the epic itself along with them).
                    // Matched by identity via `pipeline_epic_row_keys` rather
                    // than by path length: epic rows land at len 2 (Done,
                    // Refining/Pending) *or* len 3 (New, In-progress) depending
                    // on whether that section is milestone-grouped.  This arm
                    // must precede the milestone arm below — a Refining epic
                    // row and a New milestone header are both len 2.
                    SidebarEvent::RowToggleExpand { section, ref path }
                        if self
                            .pipeline_epic_row_keys
                            .contains_key(&(section, path.clone())) =>
                    {
                        if let Some(key) =
                            self.pipeline_epic_row_keys.get(&(section, path.clone())).cloned()
                        {
                            // Default expanded — mirrors the render-path
                            // default in `epic_expand_state`.
                            let entry =
                                self.pipeline_epic_expanded.entry(key).or_insert(true);
                            *entry = !*entry;
                            self.rebuild_pipeline_sidebar(None);
                        }
                        true
                    }
                    SidebarEvent::RowToggleExpand { section, ref path } if path.len() == 2 => {
                        // #668/#1069: A two-level path = a milestone sub-header
                        // was toggled, within either a New section (grouped
                        // repo → milestone) or an In-progress section (grouped
                        // liveness → milestone).  Persist the state in
                        // pipeline_milestone_expanded keyed by (lc_key,
                        // repo_key_or_liveness_key, milestone_key).
                        // Refining/Pending have no milestone tier, so a
                        // path.len()==2 there is an issue row (not a header) —
                        // those sections handle selection via RowSelected, not
                        // here.  #728: Done no longer has milestone
                        // sub-headers (flat list), so path.len()==2 in Done is
                        // an issue row — skip it here.
                        let search_offset = 1usize;
                        if section >= search_offset {
                            let state_idx = section - search_offset;
                            if let Some(&lc_key) = self.pipeline_state_section_names.get(state_idx)
                            {
                                if lc_key == "new" {
                                    let repo_groups = self.pipeline_repos_for_state(lc_key);
                                    let ri = path[0] as usize;
                                    let mi = path[1] as usize;
                                    if let Some((repo_key, repo_issue_idxs)) =
                                        repo_groups.get(ri)
                                    {
                                        let repo_key = repo_key.clone();
                                        let milestones = self
                                            .pipeline_milestones_for_issues(repo_issue_idxs);
                                        if let Some((mil_key, _, _)) = milestones.get(mi) {
                                            let mil_key = mil_key.clone();
                                            // #857: default is collapsed (New has no
                                            // in-flight concept — those issues are
                                            // pre-dispatch by definition), matching
                                            // the render-path default above.
                                            let entry = self
                                                .pipeline_milestone_expanded
                                                .entry((
                                                    lc_key.to_string(),
                                                    repo_key,
                                                    mil_key,
                                                ))
                                                .or_insert(false);
                                            *entry = !*entry;
                                            self.rebuild_pipeline_sidebar(None);
                                        }
                                    }
                                } else if lc_key == "in-progress" {
                                    let groups = self.pipeline_active_by_liveness();
                                    let gi = path[0] as usize;
                                    let mi = path[1] as usize;
                                    if let Some((group_key, issue_idxs)) = groups.get(gi) {
                                        let group_key = group_key.clone();
                                        let milestones =
                                            self.pipeline_milestones_for_issues(issue_idxs);
                                        if let Some((mil_key, _, _)) = milestones.get(mi) {
                                            let mil_key = mil_key.clone();
                                            // #1069: default is expanded — unlike
                                            // New, In-progress work is already
                                            // in flight and should be visible.
                                            let entry = self
                                                .pipeline_milestone_expanded
                                                .entry((
                                                    lc_key.to_string(),
                                                    group_key,
                                                    mil_key,
                                                ))
                                                .or_insert(true);
                                            *entry = !*entry;
                                            self.rebuild_pipeline_sidebar(None);
                                        }
                                    }
                                }
                            }
                        }
                        true
                    }
                    // #646 focus-follows-click: row click/activate blurs the search filter.
                    SidebarEvent::RowSelected { .. } | SidebarEvent::RowActivated { .. } => {
                        if self.pipeline_search.focused {
                            self.pipeline_search.focused = false;
                            self.pipeline_sidebar.focus_form(0, false);
                        }
                        true
                    }
                    SidebarEvent::HeaderActivated { .. }
                    | SidebarEvent::StateChanged
                    | SidebarEvent::Consumed
                    | SidebarEvent::ScrollChanged { .. }
                    | SidebarEvent::FormEvent { .. }
                    | SidebarEvent::RowToggleExpand { .. } => true,
                    _ => false,
                }
            }
            SidebarView::Settings => {
                // #237: the Settings sidebar is now an empty placeholder —
                // all settings live in the main-panel form.  Clicks land in
                // the sidebar slot do nothing.
                let _ = (pos, sidebar_b, backend);
                false
            }
            // #638: Kanban sidebar is a placeholder; clicks are inert.
            SidebarView::Kanban => false,
            // #737: Merge Queue sidebar is a placeholder; clicks are inert.
            SidebarView::MergeQueue => false,
            // #771: Milestone DAG sidebar is a placeholder; the milestone
            // list + DAG both live in the main panel (`mouse_main_click`).
            SidebarView::MilestoneDag => false,
            // #1121: the Plans-view tree is a raw `TreeView` (not
            // `SidebarSystem`), so — like Terminal/Sessions above — click
            // dispatch uses flat pixel-row math rather than a
            // `SidebarEvent`. No title row, so row 0 maps directly to the
            // first tree row.
            SidebarView::Plans => {
                if pos.y < sidebar_b.y {
                    return false;
                }
                let row = ((pos.y - sidebar_b.y) / lh).floor() as usize + self.plans_tree_scroll;
                self.plans_tree_click_row(row)
            }
            // #1032: the Sessions-view tree is a raw `TreeView` (not
            // `SidebarSystem`), so — like Terminal above — click dispatch
            // uses flat pixel-row math rather than a `SidebarEvent`. No
            // title row, so row 0 maps directly to the first tree row.
            SidebarView::Sessions => {
                if pos.y < sidebar_b.y {
                    return false;
                }
                let row = ((pos.y - sidebar_b.y) / lh).floor() as usize + self.sessions_tree_scroll;
                self.sessions_tree_click_row(row)
            }
            // #1039: Audit sidebar is a placeholder (count + badge only);
            // the entry list lives in the main panel (`mouse_main_click`).
            SidebarView::Audit => false,
        }
    }

    /// Handle a left-click in the main panel.
    ///
    /// Routes the click to the right handler depending on the active view:
    /// - **Settings** — delegates to the `FormController` (field clicks, toggle, segmented control).
    /// - **Board** — handles the Board/Issue tab bar.
    /// - **Pipeline** — handles the tab bar and the `PipelineView` primitive
    ///   hit-test (dispatches the Go action when a stage button is clicked).
    /// - **Machines** — no-op (no interactive elements in the main panel).
    pub(crate) fn mouse_main_click(&mut self, pos: Point, main_b: Rect, lh: f32) -> bool {
        // #249 Principle 1: toolbar row at the top of main_content_bounds
        // is hit-tested first.  A click inside it dispatches the action
        // bound to the corresponding `toolbar:<verb>` segment.  We
        // shrink `main_b` for the rest of the handler so existing
        // tab-bar math (which expects (0..tab_h) from the panel's top)
        // continues to work unchanged.
        let (content_main_b, toolbar_consumed) = self.hit_test_panel_toolbar(pos, main_b, lh);
        if toolbar_consumed {
            return true;
        }
        let main_b = content_main_b;
        // #646 focus-follows-click: click in main area blurs any active filter.
        if self.board_search.focused {
            self.board_search.focused = false;
            self.board_sidebar.focus_form(0, false);
        }
        if self.pipeline_search.focused {
            self.pipeline_search.focused = false;
            self.pipeline_sidebar.focus_form(0, false);
        }

        if self.active_view == SidebarView::Settings {
            // Route click to FormController. FormController::handle_cached
            // uses metrics cached by render_and_cache().
            let click_event = UiEvent::MouseDown {
                widget: None,
                button: MouseButton::Left,
                position: pos,
                modifiers: Modifiers::default(),
            };
            let result = self
                .settings_form
                .borrow_mut()
                .handle_cached(&click_event, main_b);
            match result {
                FormControllerEvent::FormAction(ref form_event) => {
                    // Sync keyboard focus indicator to the clicked field so
                    // keyboard navigation picks up from where the mouse clicked.
                    let clicked_id = match form_event {
                        FormEvent::SegmentedControlChanged { id, .. }
                        | FormEvent::ToggleChanged { id, .. } => Some(id.clone()),
                        _ => None,
                    };
                    let changed = self.apply_settings_event(form_event);
                    if changed {
                        if let Some(id) = clicked_id {
                            let field_ids = self.settings_interactive_field_ids();
                            if let Some(pos) = field_ids.iter().position(|fid| fid == &id) {
                                self.settings_field_sel = pos;
                            }
                        }
                    }
                    return changed;
                }
                FormControllerEvent::ScrollChanged | FormControllerEvent::Consumed => return true,
                FormControllerEvent::Ignored => return false,
            }
        }
        if self.active_view == SidebarView::Board {
            // #269: hit-test from the actual TabBar labels (char widths)
            // instead of hard-coded offsets.  This stays correct when
            // tabs are renamed or have a badge appended.
            let tab_h = detail_tab_bar_height(lh);
            if pos.y - main_b.y < tab_h {
                let bar = self.board_detail_tab_bar();
                let labels: Vec<&str> = bar.tabs.iter().map(|t| t.label.as_str()).collect();
                // Board has 4 tabs (#675 added Terminal) — unlikely to overflow at
                // typical widths, so scroll_offset is 0.
                if let Some(idx) = hit_tab_index_from_labels(&labels, main_b.x, pos.x, 0) {
                    let new_tab = match idx {
                        0 => BoardDetailTab::Board,
                        1 => BoardDetailTab::Issue,
                        2 => BoardDetailTab::Chat,
                        _ => BoardDetailTab::Terminal,
                    };
                    if new_tab != self.board_detail_tab {
                        // Mirror Pipeline tab handler: release PTY focus when
                        // switching away from the Terminal tab via mouse click.
                        if new_tab != BoardDetailTab::Terminal {
                            self.detail_terminal_focused = false;
                        }
                        self.board_detail_tab = new_tab;
                        self.detail_scroll = 0;
                        return true;
                    }
                }
                return false;
            }
            // #316: click in Chat tab content area — handle CTA button clicks.
            if self.board_detail_tab == BoardDetailTab::Chat && self.inject_chat.is_none() {
                let content_rect = Rect::new(
                    main_b.x,
                    main_b.y + tab_h,
                    main_b.width,
                    (main_b.height - tab_h).max(0.0),
                );
                let bar_h = lh * 2.0;
                let bar_rect = Rect::new(content_rect.x, content_rect.y, content_rect.width, bar_h);
                if pos.y >= bar_rect.y
                    && pos.y < bar_rect.y + bar_rect.height
                    && pos.x >= bar_rect.x
                    && pos.x < bar_rect.x + bar_rect.width
                {
                    // Two equal-width buttons: left half → Refine, right half → New Issue.
                    let mid = bar_rect.x + bar_rect.width / 2.0;
                    let action_id = if pos.x < mid {
                        "board-chat:refine"
                    } else {
                        "board-chat:new-issue"
                    };
                    return self.dispatch_toolbar_action(action_id);
                }
            }
            return false;
        }
        if self.active_view == SidebarView::Pipeline {
            // Tab bar occupies the first `detail_tab_bar_height(lh)` row of
            // the main panel — `(lh * 1.4).round()`, so the TUI and pixel
            // backends agree on the boundary (#464).
            let tab_h = detail_tab_bar_height(lh);
            if pos.y - main_b.y < tab_h {
                let bar = self.pipeline_detail_tab_bar();
                let labels: Vec<&str> = bar.tabs.iter().map(|t| t.label.as_str()).collect();
                // #605: match the painter's scroll-to-active offset so clicks
                // land on the right tab when the bar is scrolled on a narrow
                // width. The TUI tab_bar_layout computes this identically (same
                // width, same per-tab char measure, scroll arrows disabled).
                let active_idx = bar.tabs.iter().position(|t| t.is_active).unwrap_or(0);
                let tab_scroll = TabBar::fit_active_scroll_offset(
                    active_idx,
                    bar.tabs.len(),
                    main_b.width as usize,
                    |i| labels[i].chars().count(),
                );
                if let Some(idx) =
                    hit_tab_index_from_labels(&labels, main_b.x, pos.x, tab_scroll)
                {
                    // #818: Overview / Issue / Log / Summary / Terminal (5 tabs).
                    let new_tab = match idx {
                        0 => PipelineDetailTab::Overview,
                        1 => PipelineDetailTab::Issue,
                        2 => PipelineDetailTab::Log,
                        3 => PipelineDetailTab::Summary,
                        _ => PipelineDetailTab::Terminal,
                    };
                    if new_tab != self.pipeline_detail_tab {
                        if new_tab != PipelineDetailTab::Terminal {
                            self.detail_terminal_focused = false;
                        }
                        self.pipeline_detail_tab = new_tab;
                        self.pipeline_detail_scroll = if new_tab == PipelineDetailTab::Log {
                            usize::MAX
                        } else {
                            0
                        };
                        if new_tab == PipelineDetailTab::Log {
                            self.ensure_log_tab_sse();
                        }
                        return true;
                    }
                    return false;
                }
                return false;
            }
            // Below the tab row → the active tab's content. The PipelineView
            // is rendered into the content area (main_b minus tab row), so
            // we must hit-test against that rect — not main_b directly, or
            // the y-coordinates are off by tab_h.
            if self.pipeline_detail_tab == PipelineDetailTab::Overview {
                if let Some(view) = self.build_pipeline_widget() {
                    let content_rect = Rect::new(
                        main_b.x,
                        main_b.y + tab_h,
                        main_b.width,
                        (main_b.height - tab_h).max(0.0),
                    );
                    // #303: click on the button bar dispatches the active
                    // action. Bar lives at the top of content_rect when any
                    // stage has a dispatchable action.
                    let action_btn = self.pipeline_action_button();
                    let bar_h = pipeline_action_bar_height(action_btn.is_some(), lh);
                    if bar_h > 0.0
                        && pos.y >= content_rect.y
                        && pos.y < content_rect.y + bar_h
                        && pos.x >= content_rect.x
                        && pos.x < content_rect.x + content_rect.width
                    {
                        if let Some((_, stage_idx)) = action_btn {
                            self.dispatch_pipeline_stage(stage_idx);
                            return true;
                        }
                    }
                    // Stage row sits below the bar.
                    let pv_origin = Rect::new(
                        content_rect.x,
                        content_rect.y + bar_h,
                        content_rect.width,
                        (content_rect.height - bar_h).max(0.0),
                    );
                    let pv_rect = pipeline_detail_pv_rect(pv_origin, lh);
                    // Match the render path: stripped view → action_height=0,
                    // so action_bounds is always None and only Body hits fire.
                    let render_view = pipeline_view_for_render(&view);
                    let layout = tui_pipeline_layout(&render_view, pv_rect);
                    match layout.hit_test(pos.x, pos.y) {
                        PipelineHit::Action(stage_idx) => {
                            // Defensive: with action-stripped view this branch
                            // shouldn't fire, but keep the dispatch path wired
                            // in case a future stage carries an action again.
                            self.dispatch_pipeline_stage(stage_idx);
                            return true;
                        }
                        PipelineHit::Body(stage_idx) => {
                            // Click on a stage box — set focus so the content
                            // panel below switches to this stage's output.
                            self.pipeline_focused_stage = Some(stage_idx);
                            self.pipeline_stage_content_scroll = 0;
                            return true;
                        }
                        PipelineHit::Empty => return false,
                    }
                }
            }
            return false;
        }
        // #638: Kanban view — hit-test click against last known board layout.
        if self.active_view == SidebarView::Kanban {
            let hit = self.kanban_layout.borrow().as_ref().map(|l| l.hit_test(pos.x, pos.y));
            match hit {
                Some(BoardHit::Card(ref card_id)) => {
                    // Second click on already-selected card → open in Board view.
                    if self.kanban_model.selected_card_id.as_ref() == Some(card_id) {
                        let id = card_id.clone();
                        self.kanban_open_card(&id);
                    } else {
                        self.kanban_model.selected_card_id = Some(card_id.clone());
                    }
                    return true;
                }
                Some(BoardHit::ColumnHeader(_)) | Some(BoardHit::Empty) | None => {}
            }
            return false;
        }
        // #1003 fix-up: the Plans-panel roster is a raw `ListView` painted
        // straight into the main panel (unlike Board/Pipeline/Machines,
        // whose selectable rows live in the sidebar) — without this, a
        // left-click here never moved `plans_sel`, and — more importantly —
        // the right-click handler's synthetic-left-then-select flow (which
        // this same hit-test also backs, see `handle_mouse`) had nothing to
        // pre-select, leaving the #1003 CRUD context menu unreachable by
        // mouse entirely.
        if self.active_view == SidebarView::Plans {
            if let Some(idx) = self.plans_row_at(pos, main_b, lh) {
                self.plans_sel = idx;
                return true;
            }
            return false;
        }
        // #1039/#1094: Audit panel main list is a `DataTable` (was a plain
        // `ListView`) painted straight into the main panel. While the
        // detail pane is closed: a row hit selects it (unchanged from
        // #1039), a header-divider hit begins a column-resize drag (#1094
        // deliverable 3/4 — continued in the `MouseMoved` arm above and
        // released on `MouseUp`), and a plain header hit is a no-op (sort
        // is explicitly deferred, see `audit.rs` module docs). The detail
        // pane itself isn't a selectable-row target (it has no rows of its
        // own to pick), so the table isn't hit-tested at all while
        // `audit_detail_open`.
        //
        // #1094 fix (fix-iteration-1): a scrollbar-track hit is checked
        // FIRST, before `audit_table_hit` — `DataTableLayout::hit_test`
        // (quadraui) has no concept of the scrollbar strips it reserves
        // space for, so without this a click on either scrollbar fell
        // through and was mis-hit-tested as a row click (the reported
        // "scrollbar click passes through and selects/opens the row
        // underneath" bug).
        if self.active_view == SidebarView::Audit && !self.audit_detail_open {
            if let Some(axis) = self.audit_scrollbar_hit(pos, main_b) {
                self.audit_scrollbar_drag = Some(axis);
                match axis {
                    AuditScrollAxis::Vertical => {
                        self.audit_apply_vscroll(pos, main_b);
                    }
                    AuditScrollAxis::Horizontal => {
                        self.audit_apply_hscroll(pos, main_b);
                    }
                }
                return true;
            }
            return match self.audit_table_hit(pos, main_b) {
                Some(DataTableHit::Row { idx }) => {
                    self.audit_sel = idx;
                    true
                }
                Some(DataTableHit::HeaderDivider { col }) => {
                    self.audit_resize_col = Some(col);
                    true
                }
                // The audit table has no footer, so `Footer` can't occur —
                // treat it (and the other non-actionable hits) as a no-op.
                Some(DataTableHit::Header { .. })
                | Some(DataTableHit::Footer)
                | Some(DataTableHit::Empty)
                | None => false,
            };
        }
        false
    }

    /// Forward a mouse event to the active embedded terminal PTY (standalone
    /// `SidebarView::Terminal` or `PipelineDetailTab::Terminal`).
    ///
    /// Returns `true` when the PTY consumed the event — caller should trigger
    /// a redraw and skip any local fallback handling.  Returns `false` when
    /// - no terminal view is active,
    /// - the cursor is outside the terminal content area, or
    /// - `forward_mouse` reports that the PTY has mouse reporting off (for
    ///   Press/Release/Move) or neither mouse reporting nor alt-screen
    ///   active (for wheel events).
    ///
    /// The coordinate translation mirrors the render path: for the standalone
    /// terminal, `main_b` is the full main content rect (no toolbar).  For the
    /// detail terminal, [`Self::pipeline_terminal_content_y`] (tab bar height
    /// plus the `#818` pinned stage strip, when shown — `#995`) is the
    /// content-area top edge.
    pub(crate) fn terminal_mouse_event(
        &mut self,
        kind: TerminalMouseKind,
        button: MouseButton,
        pos: Point,
        modifiers: Modifiers,
        main_b: Rect,
        lh: f32,
        char_w: f32,
    ) -> bool {
        match self.active_view {
            SidebarView::Terminal => {
                // Terminal surface occupies the full main content rect (the
                // Terminal view has no panel toolbar, so main_b == the PTY area).
                if let Some((col, row)) =
                    terminal_pixel_to_cell(pos, main_b, main_b.y, char_w, lh)
                {
                    if let Some(sess) = self.standalone_pty_session_mut() {
                        return sess.forward_mouse(kind, button, col, row, modifiers);
                    }
                }
                false
            }
            SidebarView::Pipeline
                if self.pipeline_detail_tab == PipelineDetailTab::Terminal =>
            {
                // Content area starts below the tab bar and the #818 pinned
                // stage strip. `#995`: route through the shared
                // `pipeline_terminal_content_y` helper so the hit-test
                // origin lines up with the render origin (tab bar +
                // stage-strip height) in the TUI backend (where
                // `q_rect_to_ratatui` rounds the fractional `lh * 1.4` to a
                // whole cell).
                let content_y = self.pipeline_terminal_content_y(main_b, lh);
                if let Some((col, row)) =
                    terminal_pixel_to_cell(pos, main_b, content_y, char_w, lh)
                {
                    if let Some(issue_key) = self.selected_issue_key() {
                        if let Some(sess) = self.detail_terminal_sessions.get_mut(&issue_key) {
                            return sess.forward_mouse(kind, button, col, row, modifiers);
                        }
                    }
                }
                false
            }
            _ => false,
        }
    }

    /// #454: force-forward a `Release` to the active terminal session even
    /// when `pos` lies outside the PTY content rect.  Mirrors
    /// [`terminal_mouse_event`]'s routing (standalone Terminal vs
    /// Pipeline/Terminal tab) but uses [`terminal_pixel_to_cell_clamped`]
    /// so the cell coordinates land inside the visible grid.
    ///
    /// Called from the `MouseUp` arm when `pty_pressed_buttons` records an
    /// outstanding press for `button` — without this fallback, terminal
    /// apps that opted into mouse reporting would stay stuck in
    /// "button held" state after the user drags out of the panel
    /// (vim visual mode, tmux mouse drag, less, ranger, …).
    pub(crate) fn terminal_force_release(
        &mut self,
        button: MouseButton,
        pos: Point,
        main_b: Rect,
        lh: f32,
        char_w: f32,
    ) -> bool {
        match self.active_view {
            SidebarView::Terminal => {
                let (col, row) =
                    terminal_pixel_to_cell_clamped(pos, main_b, main_b.y, char_w, lh);
                if let Some(sess) = self.standalone_pty_session_mut() {
                    return sess.forward_mouse(
                        TerminalMouseKind::Release,
                        button,
                        col,
                        row,
                        Modifiers::default(),
                    );
                }
                false
            }
            SidebarView::Pipeline
                if self.pipeline_detail_tab == PipelineDetailTab::Terminal =>
            {
                // `#995`: shared helper includes both the tab bar and the
                // #818 pinned stage strip so this stays in parity with the
                // render path.
                let content_y = self.pipeline_terminal_content_y(main_b, lh);
                let (col, row) =
                    terminal_pixel_to_cell_clamped(pos, main_b, content_y, char_w, lh);
                if let Some(issue_key) = self.selected_issue_key() {
                    if let Some(sess) = self.detail_terminal_sessions.get_mut(&issue_key) {
                        return sess.forward_mouse(
                            TerminalMouseKind::Release,
                            button,
                            col,
                            row,
                            Modifiers::default(),
                        );
                    }
                }
                false
            }
            _ => false,
        }
    }

    // ── #464: host-side terminal selection helpers ────────────────────────────

    /// Translate a pixel position to a terminal cell `(col, row)` for
    /// whichever terminal view is currently active (standalone Terminal or
    /// Pipeline / Terminal tab).  Returns `None` when `pos` is outside the
    /// PTY content area.
    pub(crate) fn active_terminal_pixel_to_cell(
        &self,
        pos: Point,
        main_b: Rect,
        lh: f32,
        char_w: f32,
    ) -> Option<(u16, u16)> {
        match self.active_view {
            SidebarView::Terminal => {
                terminal_pixel_to_cell(pos, main_b, main_b.y, char_w, lh)
            }
            SidebarView::Pipeline
                if self.pipeline_detail_tab == PipelineDetailTab::Terminal =>
            {
                // `#995`: shared helper includes both the tab bar and the
                // #818 pinned stage strip so this stays in parity with the
                // render path.
                let content_y = self.pipeline_terminal_content_y(main_b, lh);
                terminal_pixel_to_cell(pos, main_b, content_y, char_w, lh)
            }
            _ => None,
        }
    }

    /// Return a mutable reference to the currently active embedded terminal
    /// session, or `None` when no terminal view is active or no session exists.
    pub(crate) fn active_terminal_session_mut(
        &mut self,
    ) -> Option<&mut quadraui::terminal_engine::TerminalSession> {
        match self.active_view {
            SidebarView::Terminal => self.standalone_pty_session_mut(),
            SidebarView::Pipeline
                if self.pipeline_detail_tab == PipelineDetailTab::Terminal =>
            {
                let key = self.selected_issue_key()?;
                self.detail_terminal_sessions.get_mut(&key)
            }
            _ => None,
        }
    }

    /// Return the selected text from the active terminal session, if any.
    pub(crate) fn active_terminal_selected_text(&self) -> Option<String> {
        match self.active_view {
            SidebarView::Terminal => self.standalone_pty_session()?.selected_text(),
            SidebarView::Pipeline
                if self.pipeline_detail_tab == PipelineDetailTab::Terminal =>
            {
                let key = self.selected_issue_key()?;
                self.detail_terminal_sessions.get(&key)?.selected_text()
            }
            _ => None,
        }
    }

    /// Clear the selection in the active terminal session.
    pub(crate) fn clear_active_terminal_selection(&mut self) {
        if let Some(sess) = self.active_terminal_session_mut() {
            sess.selection = None;
        }
    }

    /// Begin a host-side selection drag in the active terminal at `(col, row)`.
    /// Clears any previous selection and sets the anchor to `(col, row)`.
    /// No-op (dragging flag not set) when there is no active terminal session.
    pub(crate) fn terminal_host_sel_begin(&mut self, col: u16, row: u16) {
        use quadraui::terminal_engine::TerminalSelection;
        if let Some(sess) = self.active_terminal_session_mut() {
            sess.selection = Some(TerminalSelection {
                start_row: row,
                start_col: col,
                end_row: row,
                end_col: col,
            });
        } else {
            return;
        }
        self.terminal_host_sel_dragging = true;
    }

    /// Extend the host-side selection drag to `(col, row)` (move event).
    /// No-op when no drag is in progress.
    pub(crate) fn terminal_host_sel_update(&mut self, col: u16, row: u16) {
        if !self.terminal_host_sel_dragging {
            return;
        }
        if let Some(sess) = self.active_terminal_session_mut() {
            if let Some(ref mut sel) = sess.selection {
                sel.end_row = row;
                sel.end_col = col;
            }
        }
    }

    /// Finalise the host-side selection drag (release event).
    /// The selection remains visible; only the dragging flag is cleared.
    /// A collapsed selection (anchor == end) is cleared entirely since it
    /// represents a plain click with no text chosen.
    pub(crate) fn terminal_host_sel_end(&mut self) {
        self.terminal_host_sel_dragging = false;
        // Clear collapsed (point) selections — they're just phantom clicks.
        if let Some(sess) = self.active_terminal_session_mut() {
            if matches!(&sess.selection, Some(sel)
                if sel.start_row == sel.end_row && sel.start_col == sel.end_col)
            {
                sess.selection = None;
            }
        }
    }

    /// #790: decide whether a left mouse-press in the terminal pane should
    /// begin a host-side text selection instead of being forwarded to the
    /// embedded PTY.  True when:
    ///   - `shift` is held (the classic terminal override — but an OUTER tmux
    ///     consumes Shift before coord-tui sees it, which is exactly why copy
    ///     mode exists), OR
    ///   - mouse reporting is OFF (a plain shell won't consume the click), OR
    ///   - copy mode is active (the F9 toggle — keyboard-driven and therefore
    ///     immune to the outer-tmux Shift interception).
    pub(crate) fn terminal_should_host_select(&self, shift: bool, reporting_on: bool) -> bool {
        shift || !reporting_on || self.terminal_copy_mode
    }

    /// #790: whether the active view is one where host-side terminal selection
    /// (and therefore F9 copy mode) is wired.  Mirrors the arms of
    /// [`active_terminal_session_mut`]: the standalone Terminal view and the
    /// Pipeline detail Terminal tab.  The Board Terminal tab is Chat-scoped
    /// (#675) and has no host-selection path, so copy mode is unavailable there.
    pub(crate) fn terminal_copy_mode_available(&self) -> bool {
        matches!(self.active_view, SidebarView::Terminal)
            || (self.active_view == SidebarView::Pipeline
                && self.pipeline_detail_tab == PipelineDetailTab::Terminal)
    }

    /// #790: leave terminal copy mode — clear the flag, drop any in-progress
    /// drag, and discard the (uncopied) host selection.  Idempotent.
    pub(crate) fn exit_terminal_copy_mode(&mut self) {
        self.terminal_copy_mode = false;
        self.terminal_host_sel_dragging = false;
        self.clear_active_terminal_selection();
    }

    /// Scroll wheel in the sidebar.
    pub(crate) fn mouse_sidebar_scroll(
        &mut self,
        event: &UiEvent,
        delta: ScrollDelta,
        sidebar_b: Rect,
        backend: &mut dyn Backend,
        lh: f32,
    ) -> bool {
        match self.active_view {
            SidebarView::Board => {
                // Delegate to the SidebarSystem's built-in scroll handler.
                self.board_sidebar.handle(event, backend, sidebar_b);
                true
            }
            SidebarView::Machines => {
                let visible = content_visible_rows(sidebar_b, lh);
                let m = self.data.machines.len();
                if delta.y > 0.0 {
                    // Scroll up → show earlier items.
                    self.machine_scroll = self.machine_scroll.saturating_sub(1);
                } else if delta.y < 0.0 {
                    // Scroll down → show later items.
                    let max = m.saturating_sub(visible);
                    self.machine_scroll = (self.machine_scroll + 1).min(max);
                }
                true
            }
            SidebarView::Pipeline => {
                self.pipeline_sidebar.handle(event, backend, sidebar_b);
                true
            }
            SidebarView::Settings => {
                // #237: sidebar is an empty placeholder.  Forward the wheel
                // event to the main-panel form so the user can scroll the
                // settings list even when their cursor lingers on the left.
                let _ = (delta, sidebar_b, backend);
                false
            }
            // #424: Terminal view's sidebar is just a hint placeholder —
            // no scrollable content, swallow the wheel.
            SidebarView::Terminal => false,
            // #638: Kanban sidebar is a placeholder — no sidebar scroll.
            SidebarView::Kanban => false,
            // #737: Merge Queue sidebar is a placeholder — no sidebar scroll.
            SidebarView::MergeQueue => false,
            // #771: Milestone DAG sidebar is a placeholder — no sidebar scroll.
            SidebarView::MilestoneDag => false,
            // #975: Plans sidebar is a placeholder — no sidebar scroll.
            SidebarView::Plans => false,
            // #1032: Sessions tree has no mouse-wheel scroll wired in this
            // read-only slice — j/k tree-walk covers navigation, same as
            // Terminal above.
            SidebarView::Sessions => false,
            // #1039: Audit sidebar is a placeholder (count + badge only) —
            // no sidebar scroll.
            SidebarView::Audit => false,
        }
    }

    /// Scroll wheel in the main panel (detail / machine detail).
    pub(crate) fn mouse_main_scroll(&mut self, delta: ScrollDelta, pos: Point, main_b: Rect, lh: f32, char_w: f32) -> bool {
        let visible = content_visible_rows(main_b, lh);
        // Stash the live viewport size — `watch_log_list` uses this to compute
        // a stick-to-bottom offset that keeps the last line on screen.
        self.last_main_visible_rows.set(visible.max(1));
        // Watch overlay takes over the main panel; route scrollwheel to it
        // regardless of which view is active underneath.  The Log tab is
        // unaffected: it only seeds `watch_pool` (no `watch_focused`), so this
        // gate is false there and the wheel falls through to the Log scroller.
        if self.watch_focused.is_some() {
            // SSE log lines drive the count when present; fall back to the
            // remote-log cache when SSE isn't yet connected.
            let items = self
                .watch_focused
                .as_ref()
                .and_then(|id| self.watch_pool.get(id))
                .map(|ctx| ctx.sse.lines.len())
                .unwrap_or_else(|| self.watch_log_list().items.len());
            let max = items.saturating_sub(visible.saturating_sub(1));
            if let Some(id) = self.watch_focused.clone() {
                if let Some(ctx) = self.watch_pool.get_mut(&id) {
                    let w = &mut ctx.state;
                    // Anchor the current scroll position: a usize::MAX sentinel
                    // means stick-to-bottom; convert that to the explicit max
                    // before applying the wheel delta so wheel-up actually moves.
                    if w.scroll == usize::MAX {
                        w.scroll = max;
                    }
                    if delta.y > 0.0 {
                        // Wheel up → older lines.
                        w.scroll = w.scroll.saturating_sub(3);
                    } else if delta.y < 0.0 {
                        // Wheel down → newer; once at the bottom, re-enable
                        // stick-to-bottom so future appends keep auto-scrolling.
                        let new_scroll = (w.scroll + 3).min(max);
                        w.scroll = if new_scroll >= max {
                            usize::MAX
                        } else {
                            new_scroll
                        };
                    }
                }
            }
            return true;
        }
        match self.active_view {
            SidebarView::Board => {
                // Use the active tab's actual list so the scroll max matches
                // what's rendered. Board tab → detail_list; Issue tab → body.
                // Chat tab scroll is handled by ChatController itself.
                let items = match self.board_detail_tab {
                    BoardDetailTab::Board => self.detail_list().items.len(),
                    BoardDetailTab::Issue => self.board_issue_body_list().items.len(),
                    BoardDetailTab::Chat => 0,
                    BoardDetailTab::Terminal => 0, // #675: scroll handled by the PTY session
                };
                let max = items.saturating_sub(visible.saturating_sub(1));
                if delta.y > 0.0 {
                    self.detail_scroll = self.detail_scroll.saturating_sub(1);
                } else if delta.y < 0.0 {
                    self.detail_scroll = (self.detail_scroll + 1).min(max);
                }
                true
            }
            SidebarView::Machines => {
                let items = self.machine_detail_list().items.len();
                let max = items.saturating_sub(visible.saturating_sub(1));
                if delta.y > 0.0 {
                    self.machine_detail_scroll = self.machine_detail_scroll.saturating_sub(1);
                } else if delta.y < 0.0 {
                    self.machine_detail_scroll = (self.machine_detail_scroll + 1).min(max);
                }
                true
            }
            SidebarView::Pipeline => {
                // Issue tab body and Overview tab body can both overflow the
                // panel.
                match self.pipeline_detail_tab {
                    PipelineDetailTab::Issue => {
                        let items = self.pipeline_issue_body_list().items.len();
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        if delta.y > 0.0 {
                            self.pipeline_detail_scroll =
                                self.pipeline_detail_scroll.saturating_sub(1);
                        } else if delta.y < 0.0 {
                            self.pipeline_detail_scroll =
                                (self.pipeline_detail_scroll + 1).min(max);
                        }
                    }
                    PipelineDetailTab::Overview => {
                        // #818: The body list (meta + stage content) scrolls on
                        // the Overview tab.
                        let items = self.pipeline_tab_body_list().items.len();
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        if delta.y > 0.0 {
                            self.pipeline_stage_content_scroll =
                                self.pipeline_stage_content_scroll.saturating_sub(1);
                        } else if delta.y < 0.0 {
                            self.pipeline_stage_content_scroll =
                                (self.pipeline_stage_content_scroll + 1).min(max);
                        }
                    }
                    PipelineDetailTab::Log => {
                        let items = self.pipeline_log_list().items.len();
                        let visible_rows = visible.max(1);
                        let max = items.saturating_sub(visible_rows.saturating_sub(1));
                        let current = if self.pipeline_detail_scroll == usize::MAX {
                            max
                        } else {
                            self.pipeline_detail_scroll
                        };
                        if delta.y > 0.0 {
                            // Scroll up breaks sticky.
                            self.pipeline_detail_scroll = current.saturating_sub(1);
                        } else if delta.y < 0.0 {
                            let new = (current + 1).min(max);
                            // Re-stick when reaching the bottom.
                            self.pipeline_detail_scroll = if new >= max { usize::MAX } else { new };
                        }
                    }
                    PipelineDetailTab::Summary => {
                        // #558: plain scroll — same pattern as the Issue tab.
                        let items = self.pipeline_summary_list().items.len();
                        let max = items.saturating_sub(visible.saturating_sub(1));
                        if delta.y > 0.0 {
                            self.pipeline_detail_scroll =
                                self.pipeline_detail_scroll.saturating_sub(1);
                        } else if delta.y < 0.0 {
                            self.pipeline_detail_scroll =
                                (self.pipeline_detail_scroll + 1).min(max);
                        }
                    }
                    PipelineDetailTab::Terminal => {
                        // #454: Forward scroll to the PTY when the child has
                        // mouse reporting or is on the alt screen; otherwise
                        // scroll local scrollback (3 rows per notch).
                        //
                        // `forward_mouse` for `WheelUp`/`WheelDown` already
                        // gates internally on
                        // `should_forward_wheel()` (mouse reporting OR
                        // alt-screen), so calling it directly and falling
                        // back on `false` is equivalent to the explicit
                        // gate from the issue spec.
                        //
                        // `#464`: rounded helper for parity with the render
                        // path so the click-to-cell mapping is exact in TUI.
                        //
                        // `#995`: route through `pipeline_terminal_content_y`
                        // like the other three Pipeline/Terminal call sites
                        // so the stage-strip height isn't dropped here too.
                        let content_y = self.pipeline_terminal_content_y(main_b, lh);
                        if delta.y != 0.0 {
                            if let Some((col, row)) =
                                terminal_pixel_to_cell(pos, main_b, content_y, char_w, lh)
                            {
                                let kind = if delta.y > 0.0 {
                                    TerminalMouseKind::WheelUp
                                } else {
                                    TerminalMouseKind::WheelDown
                                };
                                if let Some(issue_key) = self.selected_issue_key() {
                                    if let Some(sess) =
                                        self.detail_terminal_sessions.get_mut(&issue_key)
                                    {
                                        if !sess.forward_mouse(
                                            kind,
                                            MouseButton::Left,
                                            col,
                                            row,
                                            Modifiers::default(),
                                        ) {
                                            // No PTY mouse reporting — scroll local scrollback.
                                            if delta.y > 0.0 {
                                                sess.scroll_up(3);
                                            } else {
                                                sess.scroll_down(3);
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                let _ = visible;
                true
            }
            SidebarView::Settings => {
                // Forward scroll to FormController (scrolls through form fields).
                let scroll_event = UiEvent::Scroll {
                    widget: None,
                    position: Point::new(0.0, 0.0),
                    delta,
                };
                self.settings_form
                    .borrow_mut()
                    .handle_cached(&scroll_event, main_b);
                true
            }
            // #454: Terminal pane — forward scroll wheel to the PTY when
            // the child has mouse reporting enabled or is on the alt screen
            // (e.g. tmux, vim, less).  Fall back to local scrollback when
            // forward_mouse returns false.
            //
            // `forward_mouse` for wheel kinds gates internally on
            // `should_forward_wheel()` (mouse reporting OR alt-screen),
            // matching the explicit gate from the issue spec.
            SidebarView::Terminal => {
                if delta.y != 0.0 {
                    if let Some((col, row)) =
                        terminal_pixel_to_cell(pos, main_b, main_b.y, char_w, lh)
                    {
                        if let Some(sess) = self.standalone_pty_session_mut() {
                            let kind = if delta.y > 0.0 {
                                TerminalMouseKind::WheelUp
                            } else {
                                TerminalMouseKind::WheelDown
                            };
                            if !sess.forward_mouse(
                                kind,
                                MouseButton::Left,
                                col,
                                row,
                                Modifiers::default(),
                            ) {
                                // No PTY mouse reporting — scroll local scrollback.
                                if delta.y > 0.0 {
                                    sess.scroll_up(3);
                                } else {
                                    sess.scroll_down(3);
                                }
                            }
                        }
                    }
                }
                true
            }
            // #638: Kanban — mouse wheel scroll not yet implemented in v1;
            // keyboard navigation (j/k/h/l) works.
            SidebarView::Kanban => true,
            // #737: Merge Queue panel — j/k handles navigation; wheel is no-op for now.
            SidebarView::MergeQueue => true,
            // #771: Milestone DAG panel — j/k handles navigation; wheel is no-op for now.
            SidebarView::MilestoneDag => true,
            // #975: Plans panel — j/k handles navigation; wheel is no-op for now.
            SidebarView::Plans => true,
            // #1032: Sessions main-panel detail is a static, unscrollable
            // view of the selected leaf; wheel is a no-op, same as Kanban/
            // MergeQueue/MilestoneDag/Plans above.
            SidebarView::Sessions => true,
            // #1039: Audit panel — j/k handles navigation; wheel is a no-op
            // for now, same as Plans/MergeQueue/MilestoneDag above.
            SidebarView::Audit => true,
        }
    }
}
