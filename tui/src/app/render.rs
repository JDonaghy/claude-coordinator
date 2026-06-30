#[allow(unused_imports)]
use super::*;

// ─── ShellApp implementation ──────────────────────────────────────────────────

impl ShellApp for CoordApp {
    /// Draw content into the shell's content zones.
    ///
    /// The shell has already rendered the activity bar, sidebar header, and
    /// divider. We draw:
    /// - The status bar into `layout.status_bar_bounds`.
    /// - The list (tree/machines/pipeline) into `layout.sidebar_content_bounds`.
    /// - The detail panel into `layout.main_content_bounds`.
    /// Push `active_theme` to the backend on first startup so the shell
    /// chrome (activity bar, sidebar) uses the user's saved theme from
    /// frame 0 rather than quadraui's built-in dark defaults.
    fn setup(&mut self, backend: &mut dyn Backend) {
        backend.set_theme(self.active_theme.clone());
    }

    fn render_content(&self, backend: &mut dyn Backend, layout: &AppShellLayout) {
        // Push the active theme to the backend at the start of every frame.
        // This ensures (a) content is always painted with the user's chosen
        // palette, and (b) the shell chrome (drawn by ShellAdapter *before*
        // render_content) uses the correct theme on the *next* frame — so
        // theme switches are visible within one redraw cycle.
        backend.set_theme(self.active_theme.clone());

        let lh = backend.line_height();

        // ── Status bar ────────────────────────────────────────────────
        if let Some(sb_bounds) = layout.status_bar_bounds {
            backend.draw_status_bar(sb_bounds, &self.status_bar(), None, None);
        }

        // ── Sidebar: list content (sidebar system / machines) ────────
        if let Some(full_sidebar_rect) = layout.sidebar_content_bounds {
            // #272: SidebarPanel composes the optional header toolbar (now
            // just the Board "Sync" button — the #270 contextual action bar
            // was retired in favour of the keyboard context menu) + the tree
            // beneath into one primitive.  When there's no header button the
            // slot isn't reserved, so the tree gets the full height; click
            // dispatch routes through `SidebarPanelLayout::hit_test` so the
            // off-by-one math we had to maintain by hand is gone.
            let panel = self.build_sidebar_action_panel(lh);
            let panel_layout = backend.draw_sidebar_panel(
                full_sidebar_rect,
                &panel,
                self.sidebar_action_bar_hover.hovered_id(),
                None,
            );
            let sidebar_rect = panel_layout.content_bounds;
            match self.active_view {
                SidebarView::Board => {
                    self.board_sidebar.render(backend, sidebar_rect);
                }
                SidebarView::Machines => {
                    backend.draw_list(sidebar_rect, &self.machines_list(true));
                }
                SidebarView::Pipeline => {
                    self.pipeline_sidebar.render(backend, sidebar_rect);
                }
                SidebarView::Settings => {
                    // #237: settings is one full-width form — no category
                    // nav.  Render an empty placeholder list so the sidebar
                    // slot keeps a header (consistent with other views)
                    // without offering a misleading affordance.
                    backend.draw_list(sidebar_rect, &self.settings_sidebar_placeholder());
                }
                SidebarView::Terminal => {
                    // #424: no sidebar list for the Terminal view — the
                    // pane is one big PTY surface in the main area.  Draw
                    // an empty placeholder so the sidebar header
                    // (TERMINAL / chrome) keeps a stable height.
                    backend.draw_list(sidebar_rect, &self.terminal_sidebar_placeholder());
                }
                // #638: Kanban sidebar is a placeholder — all content is in the main panel.
                SidebarView::Kanban => {
                    backend.draw_list(sidebar_rect, &self.kanban_sidebar_placeholder());
                }
                // #737: Merge Queue sidebar — entry count + attention indicator.
                SidebarView::MergeQueue => {
                    backend.draw_list(sidebar_rect, &self.merge_queue_sidebar());
                }
            }
        }

        // ── Main: detail panel only (full main_content_bounds) ───────
        let full_m = layout.main_content_bounds;
        // #249 Principle 1: draw the per-panel toolbar at the top of
        // main_content_bounds so every panel verb has a visible
        // affordance (Plan / Notify / Merge / etc.).  Carve the toolbar
        // row off the top; everything else renders into the shrunken
        // rect below it.
        let m = if let Some(toolbar) = self.panel_toolbar() {
            // #272: same SidebarPanel composition for the main-panel
            // toolbar so the tab bar below doesn't have to coordinate
            // its own slot carving.
            let panel = SidebarPanel {
                id: WidgetId::new("panel-toolbar"),
                toolbar: Some(toolbar),
                toolbar_height: Some(self.toolbar_height(lh)),
            };
            let panel_layout = backend.draw_sidebar_panel(
                full_m,
                &panel,
                self.panel_toolbar_hover.hovered_id(),
                None,
            );
            panel_layout.content_bounds
        } else {
            full_m
        };
        // Keep watch_log_list's stick-to-bottom math in sync with the live
        // viewport on every frame (not just when the user scrolls).
        self.last_main_visible_rows
            .set(content_visible_rows(m, lh).max(1));
        match self.active_view {
            SidebarView::Board => {
                // Tab bar (Board / Issue / Chat), then the active tab's content.
                // `#464`: route through `detail_tab_bar_height` so render and
                // hit-test agree on the cell boundary in the TUI backend.
                let tab_bar = self.board_detail_tab_bar();
                let tab_h = detail_tab_bar_height(lh);
                let tab_rect = Rect::new(m.x, m.y, m.width, tab_h);
                let content_rect =
                    Rect::new(m.x, m.y + tab_h, m.width, (m.height - tab_h).max(0.0));
                backend.draw_tab_bar(tab_rect, &tab_bar, None);
                match self.board_detail_tab {
                    BoardDetailTab::Board => {
                        backend.draw_list(content_rect, &self.detail_list());
                    }
                    BoardDetailTab::Issue => {
                        // #669: stash content width so board_issue_body_list can
                        // word-wrap long lines to the viewport.
                        self.last_issue_panel_cols.set(content_rect.width as usize);
                        backend.draw_list(content_rect, &self.board_issue_body_list());
                    }
                    // #316: Chat tab — empty state CTA or live board chat.
                    BoardDetailTab::Chat => {
                        self.render_board_chat_tab(backend, content_rect);
                    }
                    // #675: Terminal tab — per-issue interactive shell, mirrors
                    // PipelineDetailTab::Terminal rendering.
                    BoardDetailTab::Terminal => {
                        self.render_detail_terminal_tab(backend, content_rect);
                    }
                }
                // #316 Phase B: file-issue modal renders on top of the Chat tab.
                if self.board_detail_tab == BoardDetailTab::Chat {
                    if self.file_issue_modal.is_some() {
                        self.render_file_issue_modal(backend, m);
                    }
                }
            }
            SidebarView::Machines => {
                // #207: Reserve two sparkline rows (CPU + mem) at the bottom
                // of the main panel.  Each row is 2 × lh tall: one cell for
                // the label line and one for the chart body.  When there are
                // no metrics yet the area shows a subtle placeholder.
                let chart_h = lh * 4.0; // 2 rows × 2 lh each
                let detail_h = (m.height - chart_h).max(0.0);
                let detail_rect = Rect::new(m.x, m.y, m.width, detail_h);
                let chart_rect = Rect::new(m.x, m.y + detail_h, m.width, chart_h);
                backend.draw_list(detail_rect, &self.machine_detail_list());
                self.render_machine_sparklines(backend, chart_rect, lh);
            }
            SidebarView::Settings => {
                // Build form for the current category and render via
                // FormController (handles scrollbar + layout).
                let form = self.build_settings_form();
                let mut fc = self.settings_form.borrow_mut();
                fc.set_form(form);
                fc.render_and_cache(backend, m);
            }
            SidebarView::Pipeline => {
                // Watch overlay takes over the entire main panel when active —
                // tabs, pipeline view, meta line all hidden while watching.
                if self.pipeline_sel.is_none() && self.pipeline_issues.is_empty() {
                    backend.draw_list(m, &self.pipeline_placeholder_list());
                } else {
                    // Tab bar.  `#464`: route through `detail_tab_bar_height`
                    // so the painted top of the content rect lines up with
                    // the hit-test origin in the TUI backend.
                    let mut tab_bar = self.pipeline_detail_tab_bar();
                    let tab_h = detail_tab_bar_height(lh);
                    let tab_rect = Rect::new(m.x, m.y, m.width, tab_h);
                    let content_rect =
                        Rect::new(m.x, m.y + tab_h, m.width, (m.height - tab_h).max(0.0));
                    // #605: scroll the tab bar so the ACTIVE tab is visible on a
                    // narrow width (7 tabs overflow a small screen). The TUI
                    // painter renders from `scroll_offset` verbatim, so resolve
                    // the offset that keeps the active tab on-screen before
                    // drawing. The click hit-test below derives the same offset.
                    tab_bar.scroll_offset =
                        backend.tab_bar_layout(tab_rect, &tab_bar).correct_scroll_offset;
                    backend.draw_tab_bar(tab_rect, &tab_bar, None);

                    match self.pipeline_detail_tab {
                        PipelineDetailTab::Pipeline => {
                            // #303: button bar above the stage row when a
                            // stage is dispatchable.  Eats `bar_h` from the
                            // top of `content_rect`; the stage row and meta
                            // body shift down by the same amount.
                            // #438: use the shared helper so render and
                            // hover-tracking always agree on the toolbar
                            // shape; pass the live hover id so the button
                            // tints on mouse-over.
                            let action_toolbar = self.pipeline_action_bar_toolbar();
                            let bar_h = pipeline_action_bar_height(action_toolbar.is_some(), lh);
                            let bar_rect = Rect::new(
                                content_rect.x,
                                content_rect.y,
                                content_rect.width,
                                bar_h,
                            );
                            let pv_origin = Rect::new(
                                content_rect.x,
                                content_rect.y + bar_h,
                                content_rect.width,
                                (content_rect.height - bar_h).max(0.0),
                            );
                            let pv_rect = pipeline_detail_pv_rect(pv_origin, lh);
                            let meta_rect = Rect::new(
                                content_rect.x,
                                pv_rect.y + pv_rect.height,
                                content_rect.width,
                                (content_rect.height - bar_h - pv_rect.height).max(0.0),
                            );
                            if let Some(toolbar) = action_toolbar {
                                backend.draw_toolbar(
                                    bar_rect,
                                    &toolbar,
                                    self.pipeline_action_bar_hover.hovered_id(),
                                    None,
                                );
                            }
                            if let Some(view) = self.build_pipeline_widget() {
                                backend
                                    .draw_pipeline_view(pv_rect, &pipeline_view_for_render(&view));
                            } else {
                                backend.draw_list(pv_rect, &self.pipeline_placeholder_list());
                            }
                            backend.draw_list(meta_rect, &self.pipeline_tab_body_list());
                        }
                        PipelineDetailTab::Issue => {
                            // #669: stash content width so pipeline_issue_body_list can
                            // word-wrap long lines to the viewport.
                            self.last_issue_panel_cols.set(content_rect.width as usize);
                            backend.draw_list(content_rect, &self.pipeline_issue_body_list());
                        }
                        PipelineDetailTab::Stages => {
                            backend.draw_list(content_rect, &self.pipeline_stages_list());
                        }
                        PipelineDetailTab::Log => {
                            // #399: reserve 1 column on the right for the
                            // vertical scrollbar.  This keeps the list content
                            // out from under the thumb glyph.  For GTK/macOS
                            // backends, `width` is in pixels and 1 px is below
                            // the thumb minimum so the scrollbar is a no-op —
                            // those backends use native scrolling.
                            let sb_col_w = if content_rect.width >= 2.0 {
                                1.0_f32
                            } else {
                                0.0_f32
                            };
                            let list_rect = Rect::new(
                                content_rect.x,
                                content_rect.y,
                                (content_rect.width - sb_col_w).max(1.0),
                                content_rect.height,
                            );
                            // #385: stash panel width so pipeline_log_list can
                            // word-wrap assistant prose to the viewport.
                            self.last_log_panel_cols.set(list_rect.width as usize);
                            let log_list = self.pipeline_log_list();

                            // #399: draw the vertical scrollbar at the right edge.
                            if sb_col_w > 0.0 {
                                let total = log_list.items.len();
                                let visible = (content_rect.height as usize).max(1);
                                if total > visible {
                                    let sb_track = Rect::new(
                                        content_rect.x + list_rect.width,
                                        content_rect.y,
                                        sb_col_w,
                                        content_rect.height,
                                    );
                                    let vsb = Scrollbar::vertical(
                                        "pipeline-log-vsb",
                                        sb_track,
                                        log_list.scroll_offset as f32,
                                        total as f32,
                                        visible as f32,
                                        1.0,
                                    );
                                    backend.draw_scrollbar(sb_track, &vsb);
                                }
                            }

                            // Collect the visible text for pixel-based backends
                            // (GTK/macOS).  The TUI backend ignores `lines` and
                            // reads selection directly from its ratatui cell
                            // buffer, so this is a no-cost no-op for TUI users.
                            let lines: Vec<String> = log_list
                                .items
                                .iter()
                                .map(|it| it.text.spans.iter().map(|s| s.text.as_str()).collect())
                                .collect();
                            backend.draw_list(list_rect, &log_list);
                            // #312: register as a selectable TextRegion so the
                            // quadraui runtime handles click-drag line selection
                            // and Ctrl-C copy (OSC52 + arboard) automatically.
                            backend.register_text_region(TextRegion {
                                id: WidgetId::new("pipeline-log"),
                                bounds: list_rect,
                                lines,
                            });
                        }
                        PipelineDetailTab::Summary => {
                            // #558: session-history summary — kick the async
                            // fetch if needed (pipeline_summary_list handles
                            // that) then paint the list.
                            let summary_list = self.pipeline_summary_list();
                            backend.draw_list(content_rect, &summary_list);
                        }
                        PipelineDetailTab::Refinement => {
                            // #264: refinement chat lives in its own tab so
                            // the user can flip back to Issue / Stages / Log
                            // while the chat keeps streaming in the
                            // background.  Render the chat whenever it's
                            // open — its status strip already names the
                            // repo + issue, and Backlog rows the user
                            // refines aren't in `pipeline_issues` (they
                            // lack the `coord` label), so a per-pipeline-
                            // sel match would never fire for those.
                            if self.chat_is_refinement() {
                                // Paint an opaque backing first so the chat's
                                // empty transcript zone doesn't bleed
                                // through.
                                backend.draw_list(
                                    content_rect,
                                    &ListView {
                                        id: WidgetId::new("refinement-tab-bg"),
                                        title: None,
                                        items: Vec::new(),
                                        selected_idx: 0,
                                        scroll_offset: 0,
                                        has_focus: false,
                                        bordered: true,
                                        h_scroll: 0,
                                        max_content_width: None,
                                        show_v_scrollbar: false,
                                    },
                                );
                                if let Some(ref chat) = self.inject_chat {
                                    chat.render(backend, content_rect);
                                }
                            } else {
                                backend.draw_list(
                                    content_rect,
                                    &self.refinement_tab_placeholder_list(),
                                );
                            }
                        }
                        PipelineDetailTab::Terminal => {
                            // #440: per-issue interactive shell.  Stashes
                            // dims, reads the session snapshot, paints the
                            // PTY surface.  Spawn / resize / poll happen in
                            // `drive_detail_terminals` on every tick.
                            self.render_detail_terminal_tab(backend, content_rect);
                        }
                    }
                }
            }
            SidebarView::Terminal => {
                // #424: draw the live PTY surface into the full main rect.
                // Render path is `&self`, so we cannot spawn / resize /
                // poll here.  Instead:
                //   - stash the desired (cols, rows) for `tick` to apply
                //     via `TerminalSession::resize`,
                //   - read the session and build a paint snapshot, or
                //   - render a one-line placeholder when no session yet
                //     exists (spawn happens on the next `tick`).
                let cell_w = backend.char_width().max(1.0);
                let cell_h = lh.max(1.0);
                let cols = (m.width / cell_w).floor().max(1.0) as u16;
                let rows = (m.height / cell_h).floor().max(1.0) as u16;
                self.terminal_pending_dims.set(Some((cols, rows)));

                if let Some(ref sess) = self.terminal_session {
                    let total = sess.history_len() + sess.rows() as usize;
                    let sb = if total > sess.rows() as usize {
                        Some(sess.scrollbar_state(None))
                    } else {
                        None
                    };
                    let snapshot = sess.to_terminal(WidgetId::new("coord-terminal:0"), sb);
                    backend.draw_terminal(m, &snapshot);
                } else {
                    // No session yet — show a one-line placeholder.  The
                    // first `tick` after this render will spawn it.
                    let msg = match &self.terminal_spawn_error {
                        Some(err) => format!(
                            "  ⚠ Terminal session error: {}  (press 1/2/3/4 to switch views)",
                            err
                        ),
                        None => "  Starting shell session…".to_string(),
                    };
                    let item = activity_item(
                        &msg,
                        match self.terminal_spawn_error {
                            Some(_) => Color::rgb(220, 80, 80),
                            None => Color::rgb(180, 180, 180),
                        },
                    );
                    backend.draw_list(
                        m,
                        &ListView {
                            id: WidgetId::new("terminal-placeholder"),
                            title: None,
                            items: vec![item],
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
            }
            // #638: Kanban view — render the Board widget into the full main rect.
            SidebarView::Kanban => {
                let layout = backend.draw_board(m, &self.kanban_model);
                *self.kanban_layout.borrow_mut() = Some(layout);
            }
            // #737: Merge Queue panel — render the entry list in the main area.
            SidebarView::MergeQueue => {
                self.render_merge_queue_panel(backend, m, lh);
            }
        }

        // ── Inject chat overlay — renders over the main panel ───────────
        // #264: refinement chats render in the Pipeline Refinement tab above.
        // #316: board chats render in the Board Chat tab above.
        // Worker-guidance chats (`b` over a watched worker) stay modal —
        // those are short bursts where modal hijacking matches intent.
        let chat_is_refinement = self.chat_is_refinement();
        let chat_is_board = self.chat_is_board_chat();
        if let Some(ref chat) = self.inject_chat {
            if !chat_is_refinement && !chat_is_board {
                backend.draw_list(
                    m,
                    &ListView {
                        id: WidgetId::new("chat-backing"),
                        title: None,
                        items: Vec::new(),
                        selected_idx: 0,
                        scroll_offset: 0,
                        has_focus: false,
                        bordered: true,
                        h_scroll: 0,
                        max_content_width: None,
                        show_v_scrollbar: false,
                    },
                );
                chat.render(backend, m);
            }
        }

        // ── #319 Phase A: refinement-notes review modal ────────────────
        // Renders on top of the chat so the user can compare the proposed
        // body against what's in the chat transcript above.  Drawn before
        // toasts/context-menu so those still appear over it if relevant.
        if self.refinement_notes_modal.is_some() {
            self.render_refinement_notes_modal(backend, layout.main_content_bounds);
        }

        // ── Toast overlay (bottom-right of main content) ────────────────
        if let Some(stack) = self.toast_stack() {
            backend.draw_toast_stack(layout.main_content_bounds, &stack);
        }

        // ── #259: open context menu (above everything except dialogs) ───
        // Drawn before dialogs so a prompt dialog still sits on top.
        // The viewport unions the sidebar + main panel so a menu anchored
        // in the sidebar can flow rightward into the main area without
        // being clipped to the (narrow) sidebar width.
        if self.pending_context_menu.is_some() {
            let viewport = union_rects(layout.sidebar_content_bounds, layout.main_content_bounds);
            self.render_context_menu(backend, viewport);
        } else {
            // Keep the cached layout in sync — clear it once the menu
            // is no longer rendered so a stale layout can't satisfy a
            // hit-test on the next click.
            *self.context_menu_layout.borrow_mut() = Vec::new();
        }

        // ── #369/#329: Prompt dialogs (highest z-order) ──────────────────
        // Rendered after everything else so the modal sits on top of the
        // context menu, toasts, and panel content.  The viewport unions
        // sidebar + main so the dialog centers across the whole content
        // area (not just the narrow sidebar).
        let dialog_viewport =
            union_rects(layout.sidebar_content_bounds, layout.main_content_bounds);
        self.render_prompt_dialog(backend, dialog_viewport);

        // ── #541: issue fuzzy finder (topmost overlay) ─────────────────
        // Rendered last so it sits above all other content including
        // prompt dialogs.  When the finder is closed this is a no-op.
        if self.issue_finder.is_some() {
            self.render_issue_finder(backend, dialog_viewport);
        }

        // ── #628 Scope A: fleet-wide live-sessions overlay ───────────────
        // Rendered at the same level as the issue finder — topmost overlay.
        // When the overlay is closed this is a no-op.
        if self.live_sessions_overlay.is_some() {
            self.render_live_sessions_overlay(backend, dialog_viewport);
        }
    }

    fn handle(
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
                    // Platform contract (#464): emit TextCopied so copy-confirmation
                    // UI and future listeners observe terminal copies, matching the
                    // quadraui built-in text-selection copy path (tui/run.rs:258).
                    let _ = self.handle(UiEvent::TextCopied(text), backend, ctx);
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
        if self.active_view == SidebarView::Terminal {
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
        if in_pipeline_terminal || in_board_terminal {
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

        // ── #628 Scope A: fleet-wide live-sessions overlay (L toggle) ────────
        // `L` opens/closes the overlay from any non-PTY, non-modal view.
        // `any_blocking_modal_active()` includes `live_sessions_overlay.is_some()`
        // so Ctrl+P and other global shortcuts can't open on top of this overlay.
        // Note: `L` is intentionally NOT bound to Board/Pipeline view-specific
        // actions, so it is free to use as a global here.
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
                && !self.live_tmux_sessions.is_empty()
            {
                if self.live_sessions_overlay.is_none() {
                    self.live_sessions_overlay = Some(LiveSessionsOverlay::default());
                } else {
                    self.live_sessions_overlay = None;
                }
                return Reaction::Redraw;
            }
        }

        // ── #628 Scope A: live-sessions overlay owns ALL input while open ─────
        // Esc=close, j/k/↑/↓=navigate, r=reattach, k=kill, f=stop.
        // All other keys swallowed so board/pipeline shortcuts can't fire.
        if self.live_sessions_overlay.is_some() {
            if let UiEvent::KeyPressed { key, modifiers, .. } = &event {
                self.handle_live_sessions_overlay_key(key, modifiers);
            }
            return Reaction::Redraw;
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
        //     events to the chat only when the user is actively on the
        //     Refinement tab — otherwise the user can navigate Issue /
        //     Stages / Log freely while the chat keeps streaming in the
        //     background.
        if self.inject_chat.is_some() {
            let chat_is_refinement = self.chat_is_refinement();
            let chat_is_board = self.chat_is_board_chat();
            // Three routing modes:
            //   - **Worker-guidance**: modal — captures ALL events.
            //   - **Refinement** (#264): routed only on Pipeline > Refinement tab.
            //   - **Board chat** (#316): routed only on Board > Chat tab.
            let route_to_chat = if chat_is_refinement {
                self.active_view == SidebarView::Pipeline
                    && self.pipeline_detail_tab == PipelineDetailTab::Refinement
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
                if chat_is_board {
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
                                self.launch_interactive_session_on_machine(mode, machine, None);
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
                    Key::Char('q') | Key::Named(NamedKey::Escape) => return Reaction::Exit,

                    // ── Switch sidebar views ─────────────────────────────
                    Key::Char('1') => {
                        self.active_view = SidebarView::Board;
                        needs_redraw = true;
                    }
                    Key::Char('2') => {
                        self.active_view = SidebarView::Machines;
                        needs_redraw = true;
                    }
                    Key::Char('3') => {
                        self.active_view = SidebarView::Pipeline;
                        self.maybe_kick_pipeline_loader();
                        needs_redraw = true;
                    }
                    Key::Char('4') => {
                        self.active_view = SidebarView::Settings;
                        needs_redraw = true;
                    }
                    // #424: 5 → Terminal pane; entering defaults to
                    // PTY-focused so the user can type immediately
                    // (F12 releases focus back to the TUI chrome).
                    Key::Char('5') => {
                        self.active_view = SidebarView::Terminal;
                        self.terminal_focused = true;
                        needs_redraw = true;
                    }
                    // #638: 6 → Kanban view.
                    Key::Char('6') => {
                        self.active_view = SidebarView::Kanban;
                        needs_redraw = true;
                    }
                    // #737: 7 → Merge Queue panel.
                    Key::Char('7') => {
                        self.active_view = SidebarView::MergeQueue;
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
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Issue =>
                    {
                        self.pipeline_detail_scroll = self.pipeline_detail_scroll.saturating_add(1);
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Issue =>
                    {
                        self.pipeline_detail_scroll = self.pipeline_detail_scroll.saturating_sub(1);
                        needs_redraw = true;
                    }

                    // ── j/k — scroll Stages tab content (plan / log) ──────
                    // `[`/`]` switch the focused stage; j/k scroll the
                    // rendered content (plans + log tails overflow easily).
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Stages =>
                    {
                        self.pipeline_stage_content_scroll =
                            self.pipeline_stage_content_scroll.saturating_add(1);
                        needs_redraw = true;
                    }
                    Key::Char('k') | Key::Named(NamedKey::Up)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Stages =>
                    {
                        self.pipeline_stage_content_scroll =
                            self.pipeline_stage_content_scroll.saturating_sub(1);
                        needs_redraw = true;
                    }

                    // ── j/k — scroll Log tab: sticky-to-bottom ────────────
                    // Up breaks sticky; Down re-sticks when reaching the bottom.
                    Key::Char('j') | Key::Named(NamedKey::Down)
                        if self.active_view == SidebarView::Pipeline
                            && self.pipeline_detail_tab == PipelineDetailTab::Log =>
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
                            && self.pipeline_detail_tab == PipelineDetailTab::Summary =>
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
                            && self.pipeline_detail_tab == PipelineDetailTab::Summary =>
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
                            // #424: Terminal view nav happens via F12 +
                            // PTY passthrough — bare j/k when unfocused
                            // are inert (no list to navigate).
                            SidebarView::Terminal => {}
                            // #638: Kanban j/k handled by the earlier guarded arm.
                            SidebarView::Kanban => {}
                            // #737: MergeQueue j/k handled by the earlier guarded arm.
                            SidebarView::MergeQueue => {}
                        }
                        needs_redraw = true;
                    }

                    // ── Up / k ───────────────────────────────────────────
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
                            // #424: see Down/j arm above.
                            SidebarView::Terminal => {}
                            // #638: Kanban k handled by the earlier guarded arm.
                            SidebarView::Kanban => {}
                            // #737: MergeQueue k handled by the earlier guarded arm.
                            SidebarView::MergeQueue => {}
                        }
                        needs_redraw = true;
                    }

                    // ── [ / ] — cycle focused stage (Pipeline + Stages tabs) ─
                    // Sets `pipeline_focused_stage`, which the rasteriser
                    // draws with an accent border on the stage boxes and
                    // selects which stage's content the scrollable panel shows.
                    // Available on both the Pipeline tab (stage-content panel
                    // below the boxes) and the Stages tab.
                    Key::Char('[')
                        if self.active_view == SidebarView::Pipeline
                            && (self.pipeline_detail_tab == PipelineDetailTab::Stages
                                || self.pipeline_detail_tab == PipelineDetailTab::Pipeline) =>
                    {
                        self.focus_prev_pipeline_stage();
                        needs_redraw = true;
                    }
                    Key::Char(']')
                        if self.active_view == SidebarView::Pipeline
                            && (self.pipeline_detail_tab == PipelineDetailTab::Stages
                                || self.pipeline_detail_tab == PipelineDetailTab::Pipeline) =>
                    {
                        self.focus_next_pipeline_stage();
                        needs_redraw = true;
                    }

                    // ── h/l — cycle Pipeline detail tabs ─────────────────
                    // Order: Pipeline → Issue → Stages → Log → Summary → Refinement → Terminal → Pipeline …
                    Key::Char('h') | Key::Named(NamedKey::Left)
                        if self.active_view == SidebarView::Pipeline =>
                    {
                        let next = match self.pipeline_detail_tab {
                            PipelineDetailTab::Pipeline => PipelineDetailTab::Terminal,
                            PipelineDetailTab::Issue => PipelineDetailTab::Pipeline,
                            PipelineDetailTab::Stages => PipelineDetailTab::Issue,
                            PipelineDetailTab::Log => PipelineDetailTab::Stages,
                            PipelineDetailTab::Summary => PipelineDetailTab::Log,
                            PipelineDetailTab::Refinement => PipelineDetailTab::Summary,
                            PipelineDetailTab::Terminal => PipelineDetailTab::Refinement,
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
                            PipelineDetailTab::Pipeline => PipelineDetailTab::Issue,
                            PipelineDetailTab::Issue => PipelineDetailTab::Stages,
                            PipelineDetailTab::Stages => PipelineDetailTab::Log,
                            PipelineDetailTab::Log => PipelineDetailTab::Summary,
                            PipelineDetailTab::Summary => PipelineDetailTab::Refinement,
                            PipelineDetailTab::Refinement => PipelineDetailTab::Terminal,
                            PipelineDetailTab::Terminal => PipelineDetailTab::Pipeline,
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
                        }
                        needs_redraw = true;
                    }

                    // ── Enter — Stages tab: switch to Log tab to view the
                    //              worker log inline.  Log tab: Go fires
                    //              the active stage.  Other tabs: Go fires.
                    Key::Named(NamedKey::Enter) if self.active_view == SidebarView::Pipeline => {
                        if self.pipeline_detail_tab == PipelineDetailTab::Stages {
                            self.pipeline_detail_tab = PipelineDetailTab::Log;
                            self.pipeline_detail_scroll = usize::MAX;
                            self.ensure_log_tab_sse();
                        } else {
                            self.dispatch_pipeline_active_go();
                        }
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
                    // overrides (Machines, Pipeline) sit further down with
                    // their own `if self.active_view == …` guards and would
                    // be dead code without this exclusion — Rust evaluates
                    // match arms top-to-bottom, so an unguarded `r` here
                    // would silently shadow them.
                    Key::Char('r') if self.active_view != SidebarView::Machines => {
                        self.refresh();
                        self.kick_issue_sync();
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
                    Key::Char('n') => {
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

    /// Sync `active_view` when the shell switches panels via activity bar click.
    fn on_shell_event(&mut self, event: &AppShellEvent) {
        if let AppShellEvent::PanelChanged { panel_id } = event {
            self.active_view = match panel_id.as_str() {
                "panel:board" => SidebarView::Board,
                "panel:machines" => SidebarView::Machines,
                "panel:pipeline" => {
                    self.maybe_kick_pipeline_loader();
                    SidebarView::Pipeline
                }
                "panel:settings" => SidebarView::Settings,
                "panel:terminal" => {
                    // #424: entering the Terminal view defaults to
                    // PTY-focused so the user can start typing
                    // immediately.  F12 releases focus back to the TUI
                    // chrome (1/2/3/4/5 view switching).
                    self.terminal_focused = true;
                    SidebarView::Terminal
                }
                _ => return,
            };
        }
    }

    /// Periodic callback driven by the quadraui runner (~60Hz on TUI).
    /// Does the same time-based work as `handle()` so background refreshes,
    /// command-runner draining, and watch-log polling proceed even when the
    /// user isn't typing.
    fn tick(&mut self, _backend: &mut dyn Backend) -> Reaction {
        // Drain any completed background data load first.  Without this,
        // run_periodic_work() kicks off a new fetch and shows
        // "↻ loading…" but never picks up the completed receiver, so the
        // status bar stays yellow and assignment state (e.g. running→done)
        // doesn't reflect until the user moves the mouse or presses a key.
        let mut needs_redraw = self.apply_pending_data();
        needs_redraw |= self.run_periodic_work();
        // ── #424: drive the embedded terminal PTY ──────────────────────
        // (Lazy spawn, resize-on-demand, poll-for-output.)  Performed on
        // every tick regardless of which view is active so output keeps
        // arriving even when the user has switched away — the same
        // pattern the watch-pool already follows.  Spawn, however, only
        // fires the first time the Terminal view becomes active.
        needs_redraw |= self.drive_terminal_pane();
        // ── #440: drive per-issue detail-view terminals ─────────────────
        // Lazy spawn for the selected issue when the Terminal tab is open;
        // resize and poll all sessions on every tick so background issues'
        // shells keep accumulating output.
        needs_redraw |= self.drive_detail_terminals();
        // ── Leg 2 (#517): auto-advance Work → Review ────────────────────
        // After the board has been refreshed (apply_pending_data above),
        // check whether any interactive work we launched this run has
        // finished (board-driven — never scrapes the session TTY) and, if
        // so, raise the one-key confirm prompt to start its review.
        needs_redraw |= self.detect_completed_interactive_work();
        // ── #685: headless work with test-mode:smoke → interactive smoke ──
        // When headless Work completes on an issue carrying test-mode:smoke,
        // raise the interactive-smoke offer (same UX as interactive Work → Test).
        needs_redraw |= self.detect_headless_smoke_work_done();
        // ── Leg 3 (#517): verdict-driven routing ────────────────────────
        // Route a freshly-reported review verdict: request-changes → rework
        // prompt; approve → smoke/merge notice.  Board-driven, never scraped.
        needs_redraw |= self.detect_review_verdict();
        // ── Leg 3c / A3 (#517, #581): test-verdict routing ──────────────
        // Route a freshly-recorded `coord test` verdict on the work row:
        // failed → fail→fix prompt; passed/skipped → pass→merge prompt.
        // Board-driven (the verdict is written to the DB), never scraped.
        needs_redraw |= self.detect_test_verdict();
        // After data has been applied, the Log tab's preferred assignment may
        // have changed (e.g. auto_loop dispatched a new review/fix). Re-attach
        // SSE so we don't fall back to the polling 'Loading log…' flicker.
        if self.on_pipeline_log_tab() {
            self.ensure_log_tab_sse();
        }
        // #264: while a chat overlay is open, refresh its transcript from
        // the focused pool entry when there is new content.  Skip the
        // rebuild on ticks where neither sse.lines nor inject_transcript
        // grew — at 60 fps with a long-running chat this was scanning and
        // JSON-parsing thousands of accumulated lines every 16 ms and
        // dominated the TUI's CPU budget.
        if self.inject_chat.is_some() {
            if let Some(id) = self.watch_focused.clone() {
                if let Some(ctx) = self.watch_pool.get(&id) {
                    let key = (id.clone(), ctx.sse.lines.len(), ctx.inject_transcript.len());
                    if self.chat_transcript_cache_key.as_ref() != Some(&key) {
                        let transcript = chat_transcript_from_pool(ctx);
                        if let Some(ref mut chat) = self.inject_chat {
                            chat.set_transcript(transcript);
                        }
                        self.chat_transcript_cache_key = Some(key);
                        // Activity stamp drives the busy indicator below.
                        self.chat_last_activity = Some(Instant::now());
                    }
                }
                // #264: reflect worker state in the chat status strip so the
                // user can see when claude -p has exited (stop_reason:
                // end_turn).  Otherwise the strip stays on its initial
                // "Refinement chat → repo #N" label and the chat looks like
                // it's just slow when really the session is dead.
                let assignment_state = self
                    .data
                    .assignments
                    .iter()
                    .find(|a| a.id == id)
                    .map(|a| (a.status.clone(), a.assignment_type.clone()));
                if let Some((status, atype)) = assignment_state {
                    let issue_number = self
                        .watch_pool
                        .get(&id)
                        .map(|c| c.state.issue_number)
                        .unwrap_or(0);
                    let is_board_chat = issue_number == 0;
                    if let Some(ref mut chat) = self.inject_chat {
                        let label = match atype.as_deref() {
                            Some("refinement") if !is_board_chat => "Refinement chat",
                            Some("refinement") => "Board refinement",
                            Some("new-issue-chat") => "New issue chat",
                            _ => "Chat",
                        };
                        let suffix = if self.pending_chat_resume.is_some() {
                            // #315: a resume dispatch is in-flight — tell the
                            // user we're spinning up a new worker.
                            "  ⏳ Resuming session…"
                        } else {
                            match (atype.as_deref(), status.as_str(), is_board_chat) {
                                // #315: refinement chats are resumable from
                                // any terminal state — typing triggers
                                // chat-continue, claude reloads the session,
                                // the chat continues seamlessly.  Never show
                                // a "read-only" warning here; the prompt
                                // suffix stays send-enabled.
                                // #410: Esc now shows Cancel/Save/Send dialog.
                                (Some("refinement"), _, false) => {
                                    "  (Ctrl+S/Alt+Enter = send · Ctrl+N = notes · Esc = Cancel/Save/Send)"
                                }
                                // #316: board chats — no notes, but Ctrl+F = file issue for new-issue-chat.
                                (Some("new-issue-chat"), _, true) => {
                                    "  (Ctrl+S/Alt+Enter = send · Ctrl+F = file issue · Esc = close)"
                                }
                                (_, _, true) => {
                                    "  (Ctrl+S/Alt+Enter = send · Esc = close)"
                                }
                                (_, "done", _) | (_, "failed", _) | (_, "cancelled", _) => {
                                    "  ⚠ Worker exited — chat is read-only.  Esc to close."
                                }
                                _ => "  (Ctrl+S/Alt+Enter = send · Ctrl+N = post notes · Esc = finish)",
                            }
                        };
                        let target = if is_board_chat {
                            let repo = self
                                .watch_pool
                                .get(&id)
                                .map(|c| c.state.repo.clone())
                                .unwrap_or_default();
                            format!("  {} → {}{}", label, repo, suffix)
                        } else {
                            format!("  {} → #{}{}", label, issue_number, suffix)
                        };
                        chat.set_status(StyledText::plain(target));
                    }
                }
            }
            // Busy = activity in the last 2 s.  Animates the chat's
            // spinner so the user can tell the worker is mid-reply.
            // Throttle the spinner-driven redraw to ~10 fps so we don't
            // burn the 60 fps repaint budget on a glyph that only
            // changes ten times a second.
            let busy = self
                .chat_last_activity
                .map(|t| t.elapsed() < std::time::Duration::from_secs(2))
                .unwrap_or(false);
            if let Some(ref mut chat) = self.inject_chat {
                chat.set_busy(busy);
            }
            if busy {
                self.chat_spinner_throttle = self.chat_spinner_throttle.wrapping_add(1);
                // 6 × 16 ms ≈ 96 ms ⇒ ~10 fps spinner.
                if self.chat_spinner_throttle % 6 == 0 {
                    needs_redraw = true;
                }
            }
        } else if self.chat_transcript_cache_key.is_some() {
            // Chat closed — drop the cache key so the next open starts
            // fresh and doesn't false-hit against a stale state.
            self.chat_transcript_cache_key = None;
            self.chat_last_activity = None;
            self.chat_spinner_throttle = 0;
        }
        // #787: accumulate any tick-source redraw request and gate the
        // actual Reaction::Redraw to at most CONTENT_REDRAW_MIN apart
        // (≈15 fps).  `redraw_pending` stays true until a frame fires, so
        // the trailing update always paints within one interval.
        if needs_redraw {
            self.redraw_pending = true;
        }
        let (fire_now, new_last) = coalesce_redraw(
            self.redraw_pending,
            self.last_redraw_at,
            Instant::now(),
            CONTENT_REDRAW_MIN,
        );
        if fire_now {
            self.redraw_pending = false;
            self.last_redraw_at = new_last;
            Reaction::Redraw
        } else {
            Reaction::Continue
        }
    }
}


/// Estimate the number of visible rows in a `ListView` panel.
///
/// Deducts one row for the panel title strip.
pub(crate) fn content_visible_rows(panel: Rect, lh: f32) -> usize {
    if lh <= 0.0 {
        return 10;
    }
    let content_h = (panel.height - lh).max(0.0); // minus list title row
    (content_h / lh) as usize
}

/// #316 Phase B: scan raw SSE log text for the first `TITLE: …` / `---` /
/// body block emitted by a `new-issue-chat` worker.  The worker is
/// instructed to produce exactly this format so the TUI can file the issue.
///
/// Returns `(title, body)` when found, or `None` when no proposal exists yet.
pub(crate) fn parse_issue_proposal(text: &str) -> Option<(String, String)> {
    // Extract JSON "text" content from stream-json lines, then scan for TITLE:.
    let mut full_text = String::new();
    for line in text.lines() {
        if let Some(extracted) = extract_json_text_content(line) {
            full_text.push_str(&extracted);
            full_text.push('\n');
        }
    }
    // Find the LAST TITLE: line — the worker may have drafted multiple
    // proposals during the conversation; the most recent one is what
    // the developer wants to file.  We index by line position (not just
    // pattern match) so the body comes from the same proposal block as
    // the title — fixing a bug where a `skip_while` on the same prefix
    // would skip to the FIRST `TITLE:` and mismatch title vs body when
    // multiple proposals exist in the transcript.
    let title_prefix = "TITLE:";
    let lines: Vec<&str> = full_text.lines().collect();
    let title_idx = lines
        .iter()
        .rposition(|l| l.trim_start().starts_with(title_prefix))?;
    let title = lines[title_idx]
        .trim_start()
        .trim_start_matches(title_prefix)
        .trim()
        .to_string();
    if title.is_empty() {
        return None;
    }
    // Body is everything after the first `---` separator that follows the
    // *selected* TITLE: line (i.e. starts looking from title_idx + 1).
    let after_title = &lines[title_idx + 1..];
    let sep_idx = after_title.iter().position(|l| l.trim() == "---")?;
    let body_lines = &after_title[sep_idx + 1..];
    let body = body_lines.join("\n").trim().to_string();
    Some((title, body))
}

/// Extract the text content from a single stream-json line (type=text events).
pub(crate) fn extract_json_text_content(line: &str) -> Option<String> {
    // Lines look like: {"type":"content_block_delta","delta":{"type":"text_delta","text":"…"}}
    // or {"type":"text","value":"…"} depending on streaming mode.
    // Use a simple substring search rather than full JSON parsing.
    if !line.contains("\"text\"") {
        return None;
    }
    // Try to find `"text":"<value>"` pattern.
    let needle = "\"text\":\"";
    let start = line.find(needle)? + needle.len();
    let rest = &line[start..];
    // Collect until unescaped closing quote.
    let mut out = String::new();
    let mut chars = rest.chars().peekable();
    loop {
        match chars.next() {
            None | Some('"') => break,
            Some('\\') => match chars.next() {
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some(c) => out.push(c),
                None => break,
            },
            Some(c) => out.push(c),
        }
    }
    if out.is_empty() {
        None
    } else {
        Some(out)
    }
}

/// Shrink a `Rect` inward on all sides by `margin` (clamped to zero).
pub(crate) fn shrink_rect(r: Rect, margin: f32) -> Rect {
    let m = margin.min(r.width / 2.0).min(r.height / 2.0).max(0.0);
    Rect::new(
        r.x + m,
        r.y + m,
        (r.width - 2.0 * m).max(0.0),
        (r.height - 2.0 * m).max(0.0),
    )
}

/// #264: Build a chat transcript from a pool entry — merges the
/// developer-typed user turns (`inject_transcript`) with the assistant
/// turns parsed from the SSE stream-json lines, in chronological order.
///
/// Tool calls / system events are excluded — those belong in the Log tab,
/// not the chat.
///
/// For refinement chats the transcript opens with a System turn that
/// explains what the assistant was seeded with so the worker's first
/// reply (a clarifying question about the seeded issue) doesn't read as
/// a message-from-nowhere.
///
/// Ordering rule:
///   * Each user turn carries an `sse_offset_at_send` (its position in
///     `ctx.sse.lines` at submit time).
///   * Each assistant text event has an implicit offset = its index in
///     `ctx.sse.lines`.
///   * Walk both lists in parallel: emit user turns whose offset ≤ the
///     next assistant index, then advance through assistants.  Ties
///     prefer user (the user submitted *just before* the next assistant
///     turn arrived).
///
/// Assistant text events are split on `\n` so multi-line replies (numbered
/// lists, paragraph breaks) render as separate rows in the chat —
/// quadraui's `MessageList` doesn't honour embedded newlines inside a
/// single `StyledText`, so each line has to be its own `ChatTurn`.
/// #319 Phase A: extract the `text` field of a stream-json `assistant`
/// event with `\n` escapes preserved as real newlines.  Mirrors
/// [`extract_text_block`] but uses a newline-preserving unescaper —
/// the shared [`json_str`] helper converts `\n` to spaces so the Log
/// tab can render a multi-line block on one row (#302), which would
/// otherwise flatten markdown structure when we post the body to
/// GitHub.  Returns the empty string if no `text` block is found.
pub(crate) fn extract_text_block_keep_newlines(json: &str) -> String {
    let marker = "\"type\":\"text\"";
    let pos = match json.find(marker) {
        Some(p) => p,
        None => return String::new(),
    };
    let after = &json[pos + marker.len()..];
    let key = "\"text\":\"";
    let start = match after.find(key) {
        Some(p) => p + key.len(),
        None => return String::new(),
    };
    let rest = &after[start..];
    let mut out = String::new();
    let mut chars = rest.chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => break,
            '\\' => match chars.next() {
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some('r') => {}
                Some('"') => out.push('"'),
                Some('\\') => out.push('\\'),
                Some(other) => {
                    out.push('\\');
                    out.push(other);
                }
                None => out.push('\\'),
            },
            c => out.push(c),
        }
    }
    out
}

// ─── Readable log rendering (#385) ───────────────────────────────────────────

/// Parse one stream-json event line into zero or more displayable `ListItem`s
/// using a readable, human-friendly format (#385):
///
/// * `assistant` turns: compact `Turn N  +Xs` header on its own line, then
///   the assistant's prose wrapped to `wrap_width` columns (2-space indent),
///   STATUS:/STUCK: lines remain as single coloured rows.
/// * `tool_use` events: `  → Name: detail` (arrow prefix, no `[tool]` noise).
/// * `system(init)`, `result`, `rate_limit_event`: unchanged compact lines.
///
/// `turn_n` is incremented for each `assistant` event (same counter as the
/// old renderer — the two renderers are not mixed, so the counters stay in
/// sync independently).  `wrap_width == 0` disables wrapping (lines are
/// emitted as-is up to the text block length).
pub(crate) fn parse_json_events_readable(
    line: &str,
    turn_n: &mut usize,
    elapsed: Option<std::time::Duration>,
    wrap_width: usize,
) -> Vec<ListItem> {
    let type_val = match json_str(line, "type") {
        Some(t) => t,
        None => return Vec::new(),
    };

    match type_val.as_str() {
        "system" => {
            let subtype = json_str(line, "subtype").unwrap_or_default();
            if subtype == "init" {
                let model = json_str(line, "model").unwrap_or_else(|| "?".to_string());
                return vec![activity_item(
                    &format!("[init] {}", model),
                    Color::rgb(100, 100, 180),
                )];
            }
            Vec::new()
        }

        "assistant" => {
            *turn_n += 1;
            let n = *turn_n;

            // STATUS: / STUCK: — single special line, same colours as before.
            let text_flat = extract_text_block(line);
            if let Some(idx) = text_flat.find("STATUS:") {
                let rest = &text_flat[idx..];
                let end = rest.find('\n').unwrap_or(rest.len());
                let trimmed = rest[..end].trim();
                return vec![activity_item(trimmed, Color::rgb(80, 210, 80))];
            }
            if let Some(idx) = text_flat.find("STUCK:") {
                let rest = &text_flat[idx..];
                let end = rest.find('\n').unwrap_or(rest.len());
                let trimmed = rest[..end].trim();
                return vec![activity_item(trimmed, Color::rgb(220, 120, 50))];
            }

            let elapsed_str = match elapsed {
                Some(d) if d.as_secs() >= 1 => format!("  +{}s", d.as_secs()),
                _ => String::new(),
            };

            // Readable text — preserve newlines so paragraph structure survives.
            let text_nl = extract_text_block_keep_newlines(line);

            // No prose text block — could be thinking-only, tool-only, or both.
            if text_nl.trim().is_empty() {
                let calls = extract_tool_calls(line);
                if calls.is_empty() {
                    // Thinking-only (or completely empty) turn: header on its own
                    // line, then thinking text wrapped on separate line(s) so it
                    // isn't glued inline to "Turn N".
                    let thinking = collapse_ws(&extract_thinking_block(line));
                    let header = format!("  Turn {}{}", n, elapsed_str);
                    if thinking.is_empty() {
                        return vec![activity_item(&header, Color::rgb(80, 80, 100))];
                    }
                    let mut items = vec![activity_item(&header, Color::rgb(80, 80, 100))];
                    let think_inner_wrap = if wrap_width > 2 { wrap_width - 2 } else { 0 };
                    for wl in word_wrap(&format!("💭 {}", thinking), think_inner_wrap) {
                        items.push(activity_item(
                            &format!("  {}", wl),
                            Color::rgb(130, 110, 150),
                        ));
                    }
                    return items;
                }
                // Tool-only turn: one arrow line per tool call.
                // Drop the bare "Turn N" header — each tool line carries its own
                // content so the header is pure noise (#issue items 1-2).
                let mut items = Vec::new();
                for (call_name, call_detail) in &calls {
                    let prefix = if call_detail.is_empty() {
                        format!("  \u{2192} {}", call_name) // →
                    } else {
                        format!("  \u{2192} {}: {}", call_name, call_detail)
                    };
                    let display = if wrap_width > 0 && prefix.chars().count() > wrap_width {
                        let cut = prefix
                            .char_indices()
                            .nth(wrap_width.saturating_sub(1))
                            .map(|(i, _)| i)
                            .unwrap_or(prefix.len());
                        format!("{}…", &prefix[..cut])
                    } else {
                        prefix
                    };
                    items.push(activity_item(&display, Color::rgb(160, 130, 200)));
                }
                return items;
            }

            // Normal turn: dim header + wrapped prose + any tool calls.
            let indent = "  ";
            let prose_wrap = if wrap_width > indent.len() {
                wrap_width - indent.len()
            } else {
                0
            };
            let mut items = Vec::new();
            let header = format!("  Turn {}{}", n, elapsed_str);
            items.push(activity_item(&header, Color::rgb(80, 80, 100)));
            for wrapped_line in word_wrap(text_nl.trim_end(), prose_wrap) {
                let display = format!("{}{}", indent, wrapped_line);
                items.push(activity_item(&display, Color::rgb(200, 210, 230)));
            }
            // Mixed-content turn: also emit one arrow line per tool call in
            // the same turn (e.g. "I'll read that file" + Read in one turn).
            for (call_name, call_detail) in extract_tool_calls(line) {
                let prefix = if call_detail.is_empty() {
                    format!("  \u{2192} {}", call_name) // →
                } else {
                    format!("  \u{2192} {}: {}", call_name, call_detail)
                };
                let display = if wrap_width > 0 && prefix.chars().count() > wrap_width {
                    let cut = prefix
                        .char_indices()
                        .nth(wrap_width.saturating_sub(1))
                        .map(|(i, _)| i)
                        .unwrap_or(prefix.len());
                    format!("{}…", &prefix[..cut])
                } else {
                    prefix
                };
                items.push(activity_item(&display, Color::rgb(160, 130, 200)));
            }
            items
        }

        "tool_use" => {
            let name = json_str(line, "name").unwrap_or_else(|| "?".to_string());
            let detail = tool_detail(&name, line);
            // Compact single line: "  → Bash: <cmd>" or "  → Read: <path>"
            // Wrap the detail if it's very long, but keep the arrow prefix.
            let prefix = if detail.is_empty() {
                format!("  \u{2192} {}", name) // →
            } else {
                format!("  \u{2192} {}: {}", name, detail)
            };
            // For very long bash commands, truncate at wrap_width with ellipsis
            // so the arrow line stays on screen without horizontal scrolling.
            let display = if wrap_width > 0 && prefix.chars().count() > wrap_width {
                let cut = prefix
                    .char_indices()
                    .nth(wrap_width.saturating_sub(1))
                    .map(|(i, _)| i)
                    .unwrap_or(prefix.len());
                format!("{}…", &prefix[..cut])
            } else {
                prefix
            };
            vec![activity_item(&display, Color::rgb(160, 130, 200))]
        }

        "result" => {
            let turns = json_num(line, "num_turns").unwrap_or(0.0) as u64;
            let cost = json_num(line, "total_cost_usd").unwrap_or(0.0);
            let stop = json_str(line, "stop_reason").unwrap_or_else(|| "?".to_string());
            let dur_ms = json_num(line, "duration_ms").unwrap_or(0.0) as u64;
            let dur = fmt_dur(dur_ms / 1000);
            let text = format!(
                "[result] {} turns  ${:.2}  {}  stop={}",
                turns, cost, dur, stop
            );
            vec![activity_item(&text, Color::rgb(200, 200, 100))]
        }

        "rate_limit_event" => vec![activity_item("[rate_limit]", Color::rgb(220, 150, 50))],

        _ => Vec::new(),
    }
}

/// Pre-compute per-turn timing anchors from embedded `user.timestamp` fields.
///
/// Scans `lines` and returns one entry per `"type":"assistant"` event found:
/// the epoch-seconds of the **last** `"type":"user"` event seen *before* that
/// assistant turn.  Entries are `None` when no user event preceded the turn
/// (i.e. the very first turn, or a turn reached without any tool-result in
/// between).
///
/// `user` events in the `claude -p --output-format stream-json` protocol carry
/// an ISO-8601 `"timestamp"` field recording when the tool result was submitted.
/// Because these timestamps are **embedded in the log content** rather than
/// derived from reception time, they survive byte-0 SSE replay intact — all
/// historical lines arrive in one burst sharing a single `Instant::now()`, but
/// the JSON timestamps inside each line are real wall-clock values.
///
/// Using these timestamps to drive the `+Ns` inter-turn display (instead of
/// arrival `Instant`s) fixes the #309 regression where replayed logs showed
/// every delta as 0 and suppressed all timing labels.
pub(crate) fn user_epoch_per_turn<'a>(lines: impl Iterator<Item = &'a str>) -> Vec<Option<f64>> {
    let mut result: Vec<Option<f64>> = Vec::new();
    let mut last_user_epoch: Option<f64> = None;
    for line in lines {
        match json_str(line, "type").as_deref() {
            Some("user") => {
                if let Some(ts) = json_str(line, "timestamp") {
                    last_user_epoch = parse_iso8601_to_epoch(&ts);
                }
            }
            Some("assistant") => {
                result.push(last_user_epoch);
            }
            _ => {}
        }
    }
    result
}

/// Compute the inter-turn `Duration` for assistant turn `idx` using the
/// pre-computed `user_epochs` table from [`user_epoch_per_turn`].
///
/// Returns `Some(d)` only when both this turn and the previous turn have a
/// recorded user-event epoch AND the difference is ≥ 1 s (same suppression
/// threshold used for arrival-`Instant` timing).  Returns `None` otherwise —
/// callers fall back to arrival-`Instant` deltas in that case.
pub(crate) fn user_epoch_elapsed(user_epochs: &[Option<f64>], idx: usize) -> Option<std::time::Duration> {
    if idx == 0 {
        return None;
    }
    let this_epoch = (*user_epochs.get(idx)?)?;
    let prev_epoch = (*user_epochs.get(idx - 1)?)?;
    if this_epoch > prev_epoch {
        let secs = (this_epoch - prev_epoch) as u64;
        if secs >= 1 {
            return Some(std::time::Duration::from_secs(secs));
        }
    }
    None
}

/// #787: decide whether to emit a tick-driven redraw this frame.
///
/// Returns `(fire_now, new_last_redraw_at)`:
/// - `(true, now)` when `pending` is set and at least `min` has elapsed since `last`.
/// - `(false, last)` otherwise (caller should leave `last_redraw_at` unchanged).
///
/// Kept pure (no `Instant::now()` call inside) so unit tests can inject
/// controlled `Instant` values without real-time delays.
pub(crate) fn coalesce_redraw(
    pending: bool,
    last: Instant,
    now: Instant,
    min: Duration,
) -> (bool, Instant) {
    if pending && now.duration_since(last) >= min {
        (true, now)
    } else {
        (false, last)
    }
}

pub(crate) fn pipeline_view_for_render(view: &QuiPipelineView) -> QuiPipelineView {
    QuiPipelineView {
        id: view.id.clone(),
        stages: view
            .stages
            .iter()
            .map(|s| QuiPipelineStage {
                label: s.label.clone(),
                status: s.status.clone(),
                action: None,
            })
            .collect(),
        focused_stage: view.focused_stage,
    }
}

/// #303: Height of the pipeline button bar strip above the stage row when
/// any stage has a dispatchable action.  Zero when the bar is empty (no
/// vertical space stolen from the stage row in that case).
pub(crate) fn pipeline_action_bar_height(has_button: bool, lh: f32) -> f32 {
    if has_button {
        lh * 1.5
    } else {
        0.0
    }
}

/// Carve out the rect used by the PipelineView primitive at the top of the
/// Pipeline detail pane.  Reserves 6 rows by default (icon row + label row
/// + action row + 1 row of padding/border), clamped to ≤ 50 % of the
/// available height so the issue summary below remains visible.
pub(crate) fn pipeline_detail_pv_rect(main: Rect, lh: f32) -> Rect {
    if lh <= 0.0 {
        return Rect::new(main.x, main.y, main.width, 0.0);
    }
    let want_rows = 6.0_f32;
    let max_h = (main.height * 0.55).max(lh);
    let h = (want_rows * lh).min(max_h);
    Rect::new(main.x, main.y, main.width, h)
}

/// Render an issue's GitHub body as a ListView. Shared between the Pipeline
/// view's Issue tab and the Board view's Issue tab so the rendering and
/// scroll handling stay in lock-step.
///
/// `issue` is `Some((number, title, body, labels))` for the selected issue,
/// or `None` when no issue is selected (renders a placeholder).
///
/// `width` is the content-area width in backend units (character columns for
/// TUI, pixels for GTK).  Passed to `render_markdown_to_styled_wrapped` so
/// long body lines are word-wrapped to the panel width (#669); fenced code
/// blocks are never wrapped.  Pass 0 to disable wrapping.
pub(crate) fn issue_body_list(
    issue: Option<(u64, &str, &str, &[String])>,
    scroll_offset: usize,
    widget_id: &'static str,
    width: usize,
) -> ListView {
    let mut items: Vec<ListItem> = Vec::new();
    match issue {
        None => {
            items.push(kv_item(
                "",
                " No issue selected",
                Some(Color::rgb(100, 100, 100)),
            ));
        }
        Some((number, title, body, labels)) => {
            items.push(ListItem {
                text: StyledText {
                    spans: vec![
                        StyledSpan::with_fg(format!(" #{}", number), Color::rgb(150, 150, 240)),
                        StyledSpan::with_fg(format!("  {}", title), Color::rgb(230, 230, 255)),
                    ],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Header,
            });
            if !labels.is_empty() {
                items.push(kv_item(
                    "",
                    &format!(" labels: {}", labels.join(", ")),
                    Some(Color::rgb(160, 160, 180)),
                ));
            }
            items.push(kv_item("", "", None));
            if body.is_empty() {
                items.push(kv_item(
                    "",
                    " (no description)",
                    Some(Color::rgb(100, 100, 100)),
                ));
            } else {
                // #669: render through `render_markdown_to_styled_wrapped` so
                // long body lines are wrapped to the panel width.  Fenced code
                // blocks are never wrapped (quadraui guarantees that).  When
                // width == 0 the function falls back to the unwrapped path.
                // #372-pattern: headings, bold, italic, inline code, lists,
                // blockquotes, and fenced code blocks are styled.
                // TODO(#217): pass active_theme once issue_body_list accepts a theme
                // parameter; for now the quadraui dark default is a reasonable fallback.
                let md_theme = quadraui::Theme::default();
                let rendered =
                    quadraui::render_markdown_to_styled_wrapped(body, &md_theme, width);
                for md_line in rendered.lines {
                    items.push(ListItem {
                        text: md_line,
                        icon: None,
                        detail: None,
                        decoration: Decoration::Normal,
                    });
                }
            }
        }
    }
    ListView {
        id: WidgetId::new(widget_id),
        title: None,
        items,
        selected_idx: 0,
        scroll_offset,
        has_focus: false,
        bordered: false,
        h_scroll: 0,
        max_content_width: None,
        show_v_scrollbar: false,
    }
}
