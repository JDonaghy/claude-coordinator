//! Embedded terminal pane plumbing and PTY key encoders extracted from `app/mod.rs` (#744).
//!
//! Covers coordinate helpers, the `drive_terminal_pane` / `drive_detail_terminals` tick
//! routines, PTY input forwarding helpers, and the VT100/xterm key-to-byte encoders.
//!
//! **Import pattern:** `use super::*` is intentional — the impl methods live on `CoordApp`
//! and need the full parent namespace. See `sessions.rs` for the full rationale.
#[allow(unused_imports)]
use super::*;

// ─── Terminal mouse coordinate helpers ────────────────────────────────────────

/// Height of the detail-panel tab bar (Board / Pipeline tabs row), in
/// pixels for the GTK / macOS backends and in cell rows for the TUI
/// backend.
///
/// Both backends paint the tab row at `(lh * 1.4).round()`:
///
/// - GTK/macOS (`lh ≈ 20 px`): `28 px` — the design height; rounding is
///   a no-op since `20 * 1.4 = 28` already.
/// - TUI (`lh = 1 cell`): `1 cell` — the ratatui backend rasterises
///   into integer rows via [`quadraui::tui::backend::q_rect_to_ratatui`],
///   so the painted tab row is **one whole cell** even though `lh * 1.4`
///   would suggest a fractional `1.4` rows.
///
/// `#464`: hit-tests against the terminal content area must use the
/// SAME rounded origin as the render path, or the click-to-cell mapping
/// drifts by one row.  Before this helper existed, render used `lh*1.4`
/// (rounded to `1` cell at draw time by `q_rect_to_ratatui`) while the
/// hit-tests used the unrounded `1.4` — so a click at the top content
/// cell mapped to "row -1" (rejected) and a click one row below mapped
/// to row 0, etc.  Funnelling both code paths through this helper keeps
/// them in lock-step.
///
/// `.max(lh)` guarantees at least one full line height, which matters
/// for any future call site that might pass `lh < 1.0`.
pub(crate) fn detail_tab_bar_height(lh: f32) -> f32 {
    (lh * 1.4).round().max(lh)
}

/// Translate a pixel position into terminal (col, row) cell coordinates.
///
/// `rect` is the full bounding box of the PTY surface (in pixels).
/// `origin_y` is the Y pixel coordinate where **row 0** starts:
///   - Standalone `SidebarView::Terminal`: pass `rect.y`
///     (the entire main-content rect is the PTY area, no toolbar above it).
///   - `SidebarView::Pipeline` with the Terminal tab: pass
///     `rect.y + detail_tab_bar_height(lh)` (the rounded tab bar height —
///     `#464`: must match the render path, which `q_rect_to_ratatui`
///     rounds the same way).
///
/// `char_w` and `line_h` are the backend's character cell dimensions in pixels.
/// Both are clamped to `1.0` to guard against zero/sub-pixel values.
///
/// Returns `None` when `pos` lies outside the active PTY area
/// (left of `rect.x`, right of `rect.x + rect.width`, above `origin_y`, or
/// below `rect.y + rect.height`).
pub(crate) fn terminal_pixel_to_cell(
    pos: Point,
    rect: Rect,
    origin_y: f32,
    char_w: f32,
    line_h: f32,
) -> Option<(u16, u16)> {
    let cw = char_w.max(1.0);
    let ch = line_h.max(1.0);
    if pos.x < rect.x
        || pos.x >= rect.x + rect.width
        || pos.y < origin_y
        || pos.y >= rect.y + rect.height
    {
        return None;
    }
    let col = ((pos.x - rect.x) / cw) as u16;
    let row = ((pos.y - origin_y) / ch) as u16;
    Some((col, row))
}

/// Clamping variant of [`terminal_pixel_to_cell`] used by the `Release`
/// path (#454): when the cursor has been dragged outside the PTY content
/// area, we still need to forward a `Release` to the embedded terminal —
/// the canonical xterm-mouse protocol assumes every `Press` is matched by
/// a `Release`.  Clamping `pos` to the content rect yields a valid
/// `(col, row)` for the edge cell instead of dropping the event.
///
/// The right/bottom edges are exclusive, mirroring
/// [`terminal_pixel_to_cell`]: we clamp to `width - 1` / `height - 1` so
/// the returned cell stays inside the visible grid.
pub(crate) fn terminal_pixel_to_cell_clamped(
    pos: Point,
    rect: Rect,
    origin_y: f32,
    char_w: f32,
    line_h: f32,
) -> (u16, u16) {
    let cw = char_w.max(1.0);
    let ch = line_h.max(1.0);
    // `rect.width.max(1.0) - 1.0` keeps the right edge inclusive for
    // clamping while keeping `terminal_pixel_to_cell`'s exclusive
    // semantics for the bounds check above.
    let right_inclusive = rect.x + (rect.width.max(1.0) - 1.0);
    let bottom_inclusive = rect.y + (rect.height.max(1.0) - 1.0);
    let cx = pos.x.clamp(rect.x, right_inclusive);
    let cy = pos.y.clamp(origin_y, bottom_inclusive);
    let col = ((cx - rect.x) / cw) as u16;
    let row = ((cy - origin_y) / ch) as u16;
    (col, row)
}

/// Map a [`MouseButton`] to the bit used in `pty_pressed_buttons` (#454).
/// Returns `0` for buttons we do not track (X1/X2/Other) so that a Press
/// for those buttons never sets a Release-pending flag.
pub(crate) fn pty_button_bit(button: MouseButton) -> u8 {
    match button {
        MouseButton::Left => 1,
        MouseButton::Middle => 2,
        MouseButton::Right => 4,
        _ => 0,
    }
}


// ─── #424: embedded terminal pane plumbing ────────────────────────────────────

/// Convert a `std::panic::catch_unwind` payload to a displayable string.
///
/// Panic payloads are `Box<dyn Any + Send>`.  The two most common cases are:
/// - `&'static str`  — `unwrap()` / arithmetic overflow / index OOB panics
/// - `String`        — format-string panics (`panic!("msg {}", val)`)
///
/// Used by the vt100 crash-isolation wrappers in `drive_terminal_pane` and
/// `drive_detail_terminals` (#597).
pub(crate) fn vt100_panic_to_string(e: &Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = e.downcast_ref::<String>() {
        s.clone()
    } else if let Some(s) = e.downcast_ref::<&'static str>() {
        (*s).to_string()
    } else {
        "unknown panic in vt100 parser".to_string()
    }
}

impl CoordApp {
    /// Raise a dismissible modal dialog (#816) reporting that a vt100 parser
    /// panic has evicted a terminal session.
    ///
    /// Called by the `catch_unwind` eviction paths in both
    /// [`drive_terminal_pane`] (standalone Terminal view) and
    /// [`drive_detail_terminals`] (Pipeline / Board detail Terminal tab).
    ///
    /// `msg` is the raw panic payload string produced by [`vt100_panic_to_string`].
    /// The dialog persists until the operator explicitly dismisses it (Esc /
    /// Enter / outside-click); it renders on top of all other UI and blocks
    /// keyboard input via [`any_blocking_modal_active`].
    pub(crate) fn report_terminal_panic(&mut self, msg: String) {
        self.pty_panic_dialog = Some(msg);
    }

    /// #995: height of the pinned stage strip ("universal stage strip",
    /// #818) drawn above the content on every non-Overview Pipeline detail
    /// tab, given the post-tab-bar content rect `content_rect`.
    ///
    /// Returns `0.0` when [`Self::build_pipeline_widget`] has nothing to
    /// draw (no pipeline data for the selected issue) — the render path's
    /// `content_below_strip` closure (`render.rs`) skips the strip
    /// entirely in that case, so hit-testing must agree.
    ///
    /// This is the single source of truth for "how tall is the strip" so
    /// the render path and [`Self::pipeline_terminal_content_y`] below
    /// can never drift apart the way they did in #995 (the strip term
    /// was simply missing from the hit-test math).
    pub(crate) fn pipeline_stage_strip_height(&self, content_rect: Rect, lh: f32) -> f32 {
        if self.build_pipeline_widget().is_none() {
            return 0.0;
        }
        pipeline_detail_pv_rect_strip(content_rect, lh).height
    }

    /// #995: Y origin (in the same units as `main_b`) where PTY row 0
    /// starts for the Pipeline / Terminal detail tab — tab bar height
    /// plus the pinned stage strip height, mirroring the render path's
    /// `tab_rect` then `content_below_strip` layout in `render.rs`.
    ///
    /// `main_b` is the full main-content rect passed to the terminal
    /// mouse-event / hit-test helpers (same rect the render path calls
    /// `m`). All four Pipeline/Terminal call sites in `events.rs`
    /// (`terminal_mouse_event`, `terminal_force_release`,
    /// `active_terminal_pixel_to_cell`, and the `mouse_main_scroll`
    /// wheel-forwarding arm) route through this one function so they
    /// can't independently drift from the render origin again.
    pub(crate) fn pipeline_terminal_content_y(&self, main_b: Rect, lh: f32) -> f32 {
        let tab_h = detail_tab_bar_height(lh);
        let content_rect = Rect::new(
            main_b.x,
            main_b.y + tab_h,
            main_b.width,
            (main_b.height - tab_h).max(0.0),
        );
        main_b.y + tab_h + self.pipeline_stage_strip_height(content_rect, lh)
    }

    /// Per-tick maintenance for the embedded terminal pane (#424).
    ///
    /// Performs three things in order, each idempotent:
    ///
    /// 1. **Lazy spawn** — the first time the user opens the Terminal
    ///    view (so `terminal_pending_dims` has been written by
    ///    `render_content`), spawn a fresh [`TerminalSession`] running
    ///    the user's `$SHELL`.  Any failure is captured in
    ///    `terminal_spawn_error` so the pane shows a readable diagnostic
    ///    instead of silently being blank.
    ///
    /// 2. **Resize** — if the pending dims differ from the session's
    ///    current `(cols, rows)`, propagate via
    ///    [`TerminalSession::resize`] (which sends SIGWINCH so `vim`,
    ///    `htop`, etc. re-layout).
    ///
    /// 3. **Poll** — drain pending PTY output and the child-exit
    ///    status.  Returns `true` when a repaint is needed.
    ///
    /// Runs on every tick (not just when the Terminal view is active)
    /// so output continues to accumulate while the user is on Board /
    /// Pipeline / Settings, matching the watch-pool's behaviour.  This
    /// is cheap: `poll()` short-circuits when there's nothing on the
    /// channel and `try_wait()` is non-blocking.
    pub(crate) fn drive_terminal_pane(&mut self) -> bool {
        let mut changed = false;

        // ── #955: fleet-terminal attach/poll ──────────────────────────
        //
        // Runs BEFORE the bare-shell fallback below: when the Terminal
        // tree has a leaf selected, that leaf's attached PTY is what the
        // main pane shows (see `standalone_pty_session[_mut]` /
        // `render.rs`), so only the bare-shell branch needs the
        // `is_none()` spawn guard to stay silent — spawning a $SHELL
        // nobody will see would waste a process for every operator who
        // never leaves fleet terminals.
        let selected_fleet_key = self.selected_fleet_terminal_key();
        if let Some(ref key) = selected_fleet_key {
            self.ensure_fleet_terminal_attached(key);
        }
        // Resize + poll EVERY cached fleet session (not just the selected
        // one) so background output keeps accumulating while the operator
        // is looking at a different leaf — mirrors `drive_detail_terminals`.
        if let Some((cols, rows)) = self.terminal_pending_dims.get() {
            for sess in self.fleet_terminal_sessions.values_mut() {
                if cols != sess.cols() || rows != sess.rows() {
                    sess.resize(cols, rows);
                }
            }
        }
        let mut fleet_keys_changed: Vec<(String, String)> = Vec::new();
        let mut fleet_panicked: Vec<((String, String), String)> = Vec::new();
        for (key, sess) in self.fleet_terminal_sessions.iter_mut() {
            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| sess.poll())) {
                Ok(c) => {
                    if c {
                        fleet_keys_changed.push(key.clone());
                    }
                }
                Err(e) => {
                    fleet_panicked.push((key.clone(), vt100_panic_to_string(&e)));
                    fleet_keys_changed.push(key.clone());
                }
            }
        }
        // #597: evict panicked sessions outside the borrow, same treatment
        // as the bare pane and the per-issue detail terminals.
        for (key, msg) in fleet_panicked {
            self.fleet_terminal_sessions.remove(&key);
            self.fleet_terminal_spawn_errors
                .insert(key, format!("Session ended (renderer fault: {})", msg));
            self.report_terminal_panic(msg);
        }
        // Only the currently-visible fleet session's output should trigger
        // a repaint here — background leaves have already drained into
        // their own scrollback (#789-style suppression, mirrors
        // `drive_detail_terminals`'s `visible_key_changed`).
        let visible_fleet_changed = selected_fleet_key
            .as_ref()
            .map_or(false, |k| fleet_keys_changed.contains(k));
        changed |= visible_fleet_changed;

        // ── Bare-shell fallback ────────────────────────────────────────
        // Only spawned/driven while NO fleet-terminal leaf is selected
        // (empty tree, or a machine row selected) — this is the pre-#955
        // scratch shell behaviour, preserved as-is for that case.
        //
        // 1. Lazy spawn — only triggered the first time the user opens
        //    the Terminal view (so the renderer has stashed real dims).
        if selected_fleet_key.is_none()
            && self.terminal_session.is_none()
            && self.terminal_spawn_error.is_none()
        {
            if let Some((cols, rows)) = self.terminal_pending_dims.get() {
                let cwd = std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("/"));
                let shell = quadraui::terminal_engine::default_shell();
                match quadraui::terminal_engine::TerminalSession::spawn(
                    cols.max(20),
                    rows.max(5),
                    &shell,
                    &cwd,
                    10_000, // 10 000-line scrollback
                ) {
                    Ok(sess) => {
                        self.terminal_session = Some(sess);
                        changed = true;
                    }
                    Err(e) => {
                        self.terminal_spawn_error = Some(e.to_string());
                        changed = true;
                    }
                }
            }
        }

        // #955: whether the bare-shell pane is the one actually shown in
        // the main pane right now — gates the `changed` contributions
        // below so a background bare-shell update doesn't force a redraw
        // while a fleet-terminal leaf is what's visible (kept warm, but
        // silently, exactly like a background fleet session would be).
        let bare_visible = selected_fleet_key.is_none();

        // 2. Resize on dimension change.
        if let Some(ref mut sess) = self.terminal_session {
            if let Some((cols, rows)) = self.terminal_pending_dims.get() {
                if cols != sess.cols() || rows != sess.rows() {
                    sess.resize(cols, rows);
                    changed |= bare_visible;
                }
            }
        }

        // 3. Drain PTY output + observe child exit.
        //
        //    Wrapped in catch_unwind (#597): the vt100 parser can panic on
        //    malformed or unexpected escape sequences (observed:
        //    `screen.rs:934 unwrap on None`, `grid.rs:672 subtract overflow`).
        //    A panic here must NOT abort the TUI — instead, drop the session
        //    and record an error so the pane shows a readable banner, while
        //    subsequent ticks continue normally (board-driven post-exit actions
        //    such as detect_completed_interactive_work fire independently of
        //    the terminal emulator state).
        let terminal_poll = self
            .terminal_session
            .as_mut()
            .map(|sess| std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| sess.poll())));
        match terminal_poll {
            Some(Ok(c)) => changed |= c && bare_visible,
            Some(Err(e)) => {
                // #672: renderer-only fault — the session is treated as
                // terminated so the pane shows a clean "session ended"
                // notice rather than a scary panic string.  Board-driven
                // post-exit actions (detect_completed_interactive_work etc.)
                // fire independently on the next tick because they check
                // session_pane_live(), which returns false when the session
                // is gone — regardless of whether it exited normally or was
                // evicted here.
                let panic_msg = vt100_panic_to_string(&e);
                self.terminal_session = None;
                self.terminal_spawn_error = Some(format!(
                    "Session ended (renderer fault: {})",
                    panic_msg
                ));
                // #816: surface a dismissible modal so the operator sees an
                // explicit notification rather than silently losing the pane.
                self.report_terminal_panic(panic_msg);
                changed |= bare_visible;
            }
            None => {}
        }

        // Only signal a repaint when the Terminal view is currently visible.
        // When the user is on any other view the PTY output has already been
        // consumed into the VT100 scrollback buffer (no output is lost), but
        // there is nothing on screen to update — suppressing the redraw here
        // eliminates the ≈286 µs full-screen refresh per background PTY tick.
        // The next view switch sets `needs_redraw = true` via the normal
        // event handler, so all accumulated output paints immediately.
        changed && self.active_view == SidebarView::Terminal
    }

    /// Forward a key press to the embedded terminal PTY (#424).
    ///
    /// Encodes the key + modifiers into the appropriate xterm-256color
    /// escape sequence via [`key_to_pty_bytes`] and writes them to the
    /// PTY via [`TerminalSession::write_input`].  Returns `true` when
    /// the event was consumed (caller should suppress further routing
    /// and request a redraw); `false` when there is no live session or
    /// the key produced no PTY bytes (e.g. CapsLock).
    /// #605: minimal `Ctrl-W` pane leader for keyboard focus movement — a
    /// keyboard-only slice of the #578 focus model (no mouse focus).
    ///
    /// Returns `Some(Reaction)` when the event was consumed as part of a leader
    /// sequence (either the `Ctrl-W` prefix itself, or the key that resolves
    /// it), and `None` when the event is unrelated and should fall through to
    /// normal dispatch.
    ///
    ///   `Ctrl-W h` / `Ctrl-W Left`   → focus the side panel: blur the embedded
    ///                                   terminal so bare `j`/`k` drive the
    ///                                   sidebar again.  Works from the terminal
    ///                                   or any tab.
    ///   `Ctrl-W l` / `Ctrl-W Right`  → cycle focus forward: Sidebar → Main → Detail → Sidebar.
    ///                                   When a PTY is visible in the target pane, also toggles
    ///                                   `terminal_focused` / `detail_terminal_focused`.
    ///   `Ctrl-W h` / `Ctrl-W Left`   → cycle focus backward: Sidebar → Detail → Main → Sidebar.
    ///   `Ctrl-W Ctrl-W`              → forward a literal `Ctrl-W` to the focused
    ///                                   PTY, so an inner app's own window key
    ///                                   (e.g. vim) is still reachable.
    /// Any other key after the prefix cancels the leader (swallowed, no action).
    ///
    /// §4 (#782): `focused_region` tracks Sidebar / Main / Detail across all views.
    /// The existing terminal blur/focus behaviour is preserved as a side-effect of
    /// the region change: moving to Sidebar blurs PTYs; moving to Main focuses the
    /// standalone terminal PTY; moving to Detail focuses the per-issue detail PTY.
    ///
    /// When a PTY view is active (standalone Terminal, Pipeline Terminal tab),
    /// the cycle can skip the "Main" step and jump directly to "Detail" so that
    /// a single `Ctrl-W l` from the sidebar focuses the PTY — matching the old
    /// two-region (Sidebar ↔ content) behaviour that existed before this refactor.
    pub(crate) fn handle_ctrl_w_leader(
        &mut self,
        key: &Key,
        modifiers: &quadraui::Modifiers,
    ) -> Option<Reaction> {
        use quadraui::NamedKey;

        // Step 2 — a key following the `Ctrl-W` prefix resolves the chord.
        if self.ctrl_w_pending {
            self.ctrl_w_pending = false;
            match key {
                // → cycle focus LEFT (backward).
                Key::Char('h') | Key::Named(NamedKey::Left)
                    if !modifiers.ctrl && !modifiers.alt =>
                {
                    self.focused_region = self.next_region_left();
                    self.apply_pty_focus_for_region();
                }
                // → cycle focus RIGHT (forward).
                Key::Char('l') | Key::Named(NamedKey::Right)
                    if !modifiers.ctrl && !modifiers.alt =>
                {
                    self.focused_region = self.next_region_right();
                    self.apply_pty_focus_for_region();
                }
                // `Ctrl-W Ctrl-W` → literal `Ctrl-W` to the focused PTY.
                Key::Char('w') | Key::Char('W') if modifiers.ctrl => {
                    if self.active_view == SidebarView::Terminal && self.terminal_focused {
                        let _ = self.forward_key_to_pty(key, modifiers);
                    } else if self.active_view == SidebarView::Pipeline
                        && self.pipeline_detail_tab == PipelineDetailTab::Terminal
                        && self.detail_terminal_focused
                    {
                        let _ = self.forward_key_to_detail_terminal(key, modifiers);
                    }
                }
                // Any other key cancels the leader.
                _ => {}
            }
            return Some(Reaction::Redraw);
        }

        // Step 1 — the `Ctrl-W` prefix itself: arm the latch, consume the key.
        if matches!(key, Key::Char('w') | Key::Char('W'))
            && modifiers.ctrl
            && !modifiers.alt
        {
            self.ctrl_w_pending = true;
            return Some(Reaction::Redraw);
        }

        None
    }

    // ─── §4 (#782): focus-region helpers ─────────────────────────────────────

    /// Derive the "effective" focused region from the PTY focus flags.
    ///
    /// PTY focus flags (`terminal_focused`, `detail_terminal_focused`) may be
    /// set by code paths outside the Ctrl-W handler (e.g. F12, entering the
    /// Terminal view via the activity bar).  Reading them here ensures the
    /// cycle always starts from the correct position even when `focused_region`
    /// hasn't been synchronised yet.
    fn effective_focused_region(&self) -> FocusedRegion {
        if self.detail_terminal_focused {
            FocusedRegion::Detail
        } else if self.terminal_focused && self.active_view == SidebarView::Terminal {
            FocusedRegion::Main
        } else {
            self.focused_region
        }
    }

    /// True when the active view has a PTY in the Detail region.
    fn has_detail_pty(&self) -> bool {
        self.active_view == SidebarView::Pipeline
            && self.pipeline_detail_tab == PipelineDetailTab::Terminal
    }

    /// True when the active view has a PTY in the Main region.
    fn has_main_pty(&self) -> bool {
        self.active_view == SidebarView::Terminal
    }

    /// The ordered set of focus regions the current view actually exposes.
    ///
    /// The Ctrl-W cycler walks this list, so a view only ever stops on regions
    /// that exist.  Three cases:
    /// - **Pipeline detail Terminal tab** — the PTY lives in the Detail region
    ///   and there is no separately focusable Main content, so the cycle is
    ///   `Sidebar ↔ Detail`.  A single `Ctrl-W l` from the sidebar lands on the
    ///   PTY, preserving the pre-§4 two-region UX (and the `ctrl_w_l_focuses_
    ///   detail_terminal_content` test).
    /// - **Standalone Terminal view** — the PTY *is* the Main region and there
    ///   is no Detail pane, so the cycle is `Sidebar ↔ Main`.
    /// - **Every other view** (Board, Pipeline non-terminal tabs, Machines,
    ///   Settings, Kanban, Merge Queue) — a full list-plus-detail layout, so all
    ///   three regions `Sidebar → Main → Detail` participate.  This is what makes
    ///   `Ctrl-W h`/`l` reach `Detail` on the Board and Pipeline views (#782
    ///   review finding: the old logic only ever toggled Sidebar ↔ Main there).
    fn available_regions(&self) -> Vec<FocusedRegion> {
        if self.has_detail_pty() {
            vec![FocusedRegion::Sidebar, FocusedRegion::Detail]
        } else if self.has_main_pty() {
            vec![FocusedRegion::Sidebar, FocusedRegion::Main]
        } else {
            vec![
                FocusedRegion::Sidebar,
                FocusedRegion::Main,
                FocusedRegion::Detail,
            ]
        }
    }

    /// Step through [`available_regions`] from the current position.
    ///
    /// `forward` cycles Sidebar → Main → Detail → Sidebar; `!forward` reverses
    /// it (Sidebar → Detail → Main → Sidebar).  Both directions visit every
    /// region the view exposes, so the two-way cycle is symmetric.
    fn cycle_region(&self, forward: bool) -> FocusedRegion {
        let regions = self.available_regions();
        let len = regions.len();
        // `effective_focused_region` may report a region the current view no
        // longer exposes (e.g. a stale Detail flag); fall back to the first
        // region so the cycle always has a valid anchor.
        let idx = regions
            .iter()
            .position(|&r| r == self.effective_focused_region())
            .unwrap_or(0);
        let next = if forward {
            (idx + 1) % len
        } else {
            (idx + len - 1) % len
        };
        regions[next]
    }

    /// Compute the next region when cycling RIGHT (Sidebar → Main → Detail → Sidebar).
    fn next_region_right(&self) -> FocusedRegion {
        self.cycle_region(true)
    }

    /// Compute the next region when cycling LEFT (Sidebar → Detail → Main → Sidebar).
    fn next_region_left(&self) -> FocusedRegion {
        self.cycle_region(false)
    }

    /// §4 (#782): Update `terminal_focused` / `detail_terminal_focused` to
    /// match the new `focused_region`.
    ///
    /// Called after every `focused_region` change caused by a `Ctrl-W`
    /// chord so that PTY-passthrough behaviour stays in sync with the
    /// region model:
    /// - Sidebar  → blur all PTYs (bare-key nav drives the sidebar list).
    /// - Main     → focus the standalone Terminal PTY when it is the
    ///              active view; blur detail PTY.
    /// - Detail   → focus the Pipeline detail Terminal PTY when that tab
    ///              is open; blur the standalone PTY.
    fn apply_pty_focus_for_region(&mut self) {
        match self.focused_region {
            FocusedRegion::Sidebar => {
                self.terminal_focused = false;
                self.detail_terminal_focused = false;
            }
            FocusedRegion::Main => {
                self.detail_terminal_focused = false;
                if self.active_view == SidebarView::Terminal {
                    self.terminal_focused = true;
                } else {
                    self.terminal_focused = false;
                }
            }
            FocusedRegion::Detail => {
                self.terminal_focused = false;
                if self.active_view == SidebarView::Pipeline
                    && self.pipeline_detail_tab == PipelineDetailTab::Terminal
                {
                    self.detail_terminal_focused = true;
                } else {
                    self.detail_terminal_focused = false;
                }
            }
        }
    }

    /// #955: whichever PTY currently backs the standalone Terminal view's
    /// main pane — the attached fleet-terminal session for the selected
    /// tree leaf (`fleet_terminal_sessions`), or the bare local-shell
    /// fallback (`terminal_session`) when nothing/only a machine row is
    /// selected. Centralizes the fleet-vs-bare routing so mouse/keyboard/
    /// selection code doesn't have to re-derive it at every call site.
    pub(crate) fn standalone_pty_session(
        &self,
    ) -> Option<&quadraui::terminal_engine::TerminalSession> {
        if let Some(key) = self.selected_fleet_terminal_key() {
            return self.fleet_terminal_sessions.get(&key);
        }
        self.terminal_session.as_ref()
    }

    /// Mutable counterpart of [`Self::standalone_pty_session`].
    pub(crate) fn standalone_pty_session_mut(
        &mut self,
    ) -> Option<&mut quadraui::terminal_engine::TerminalSession> {
        if let Some(key) = self.selected_fleet_terminal_key() {
            return self.fleet_terminal_sessions.get_mut(&key);
        }
        self.terminal_session.as_mut()
    }

    pub(crate) fn forward_key_to_pty(&mut self, key: &Key, mods: &quadraui::Modifiers) -> bool {
        let Some(sess) = self.standalone_pty_session_mut() else {
            return false;
        };
        if sess.is_exited() {
            // Don't write to a dead PTY — but DO swallow the keypress
            // (the user is on the Terminal view; we don't want stray
            // keys to drive nav while the dead-process pane is up).
            return true;
        }
        // Any input pops the user back to the live view (matches the
        // quadraui terminal example's behaviour).
        sess.scroll_reset();
        if let Some(bytes) = key_to_pty_bytes(key.clone(), *mods) {
            sess.write_input(&bytes);
        }
        true
    }

    /// Forward a clipboard paste to the embedded terminal PTY (#468).
    ///
    /// If the PTY has bracketed-paste mode active (DEC private mode 2004,
    /// `ESC[?2004h`), wraps the text in `ESC[200~` … `ESC[201~` before
    /// writing so the receiving application can distinguish a paste from
    /// typed characters.  Otherwise the text is sent as raw bytes.
    ///
    /// No trailing carriage-return is appended — the human must press
    /// Enter to submit (avoids auto-execute / paste-injection risks).
    ///
    /// Returns `true` when the paste was consumed (session exists);
    /// `false` when no live session is available.
    pub(crate) fn forward_paste_to_pty(&mut self, text: &str) -> bool {
        let Some(sess) = self.standalone_pty_session_mut() else {
            return false;
        };
        if sess.is_exited() {
            // Swallow paste to a dead PTY rather than letting it fall
            // through to TUI chrome — matches forward_key_to_pty.
            return true;
        }
        sess.scroll_reset();
        if sess.bracketed_paste_enabled() {
            let mut bytes = Vec::with_capacity(text.len() + 12);
            bytes.extend_from_slice(b"\x1b[200~");
            bytes.extend_from_slice(text.as_bytes());
            bytes.extend_from_slice(b"\x1b[201~");
            sess.write_input(&bytes);
        } else {
            sess.write_input(text.as_bytes());
        }
        true
    }

    // ── #440: per-issue detail-view terminal helpers ──────────────────────

    /// Return the issue number for the currently-selected pipeline issue,
    /// or `None` when nothing is selected.
    /// Kept for unit tests; production code uses `selected_issue_key()`.
    #[cfg(test)]
    pub(crate) fn selected_issue_number(&self) -> Option<u64> {
        self.pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .map(|issue| issue.number)
    }

    /// Return the `(repo_slug, issue_number)` key for the currently selected
    /// Pipeline issue, used to index `detail_terminal_sessions` and
    /// `detail_terminal_spawn_errors` (#455).
    pub(crate) fn selected_issue_key(&self) -> Option<(String, u64)> {
        // #675 BUG 3: When on the Board panel, use the Board selection so
        // each board row has its OWN per-issue terminal slot.  Falling back
        // to `pipeline_sel` here caused every board row to share the same
        // pipeline terminal (the "singleton" symptom).
        if self.active_view == SidebarView::Board {
            if let Some((coord_repo, num)) = self.board_selected_issue() {
                let slug = self
                    .data
                    .pipeline_repos
                    .iter()
                    .find(|(name, _)| *name == coord_repo)
                    .map(|(_, s)| s.clone())
                    .unwrap_or(coord_repo);
                return Some((slug, num));
            }
            return None;
        }
        // For all other views (Pipeline, Terminal, etc.) use the pipeline
        // selection as the primary source.
        self.pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .map(|issue| (issue.repo_slug.clone(), issue.number))
    }

    /// Return a sensible cwd for a per-issue detail terminal.  Uses the
    /// repo path from `pipeline_repo_paths` when it exists and is a
    /// directory; falls back to `current_dir()`.
    pub(crate) fn detail_terminal_cwd(&self, issue_key: &(String, u64)) -> std::path::PathBuf {
        // Pipeline path: look up via the selected PipelineIssue.
        if let Some(issue) = self
            .pipeline_sel
            .and_then(|i| self.pipeline_issues.get(i))
            .filter(|iss| iss.repo_slug == issue_key.0 && iss.number == issue_key.1)
        {
            let repo_key = issue.coord_repo.as_deref().unwrap_or(&issue.repo_slug);
            if let Some(path) = self.data.pipeline_repo_paths.get(repo_key) {
                let p = std::path::Path::new(path);
                if p.is_dir() {
                    return p.to_path_buf();
                }
            }
        }
        // #675: Board path: look up via board_active_repo() → pipeline_repo_paths.
        if self.active_view == SidebarView::Board {
            if let Some(coord_repo) = self.board_active_repo() {
                if let Some(path) = self.data.pipeline_repo_paths.get(coord_repo) {
                    let p = std::path::Path::new(path);
                    if p.is_dir() {
                        return p.to_path_buf();
                    }
                }
            }
        }
        std::env::current_dir().unwrap_or_else(|_| std::path::PathBuf::from("/"))
    }

    /// Drive all per-issue detail terminal sessions (#440).
    ///
    /// 1. **Lazy spawn** — when the Terminal tab is active for a selected
    ///    issue and no session exists yet, spawn one (cwd = repo path or
    ///    `current_dir()`).
    ///
    /// 2. **Resize** — propagate pending dims to all live sessions.
    ///
    /// 3. **Poll** — drain PTY output for every session (not just the
    ///    selected one) so output keeps arriving in the background.
    ///
    /// Called on every `tick()`, mirroring the standalone
    /// `drive_terminal_pane`.
    pub(crate) fn drive_detail_terminals(&mut self) -> bool {
        let mut changed = false;

        // 1. Lazy spawn for the selected issue when the Terminal tab is shown.
        // #675: also covers the Board Terminal tab.
        if (self.active_view == SidebarView::Pipeline
            && self.pipeline_detail_tab == PipelineDetailTab::Terminal)
            || (self.active_view == SidebarView::Board
                && self.board_detail_tab == BoardDetailTab::Terminal)
        {
            if let Some(issue_key) = self.selected_issue_key() {
                if !self.detail_terminal_sessions.contains_key(&issue_key)
                    && !self.detail_terminal_spawn_errors.contains_key(&issue_key)
                {
                    if let Some((cols, rows)) = self.detail_terminal_pending_dims.get() {
                        let cwd = self.detail_terminal_cwd(&issue_key);
                        let shell = quadraui::terminal_engine::default_shell();
                        match quadraui::terminal_engine::TerminalSession::spawn(
                            cols.max(20),
                            rows.max(5),
                            &shell,
                            &cwd,
                            10_000, // 10 000-line scrollback
                        ) {
                            Ok(sess) => {
                                self.detail_terminal_sessions.insert(issue_key, sess);
                                changed = true;
                            }
                            Err(e) => {
                                self.detail_terminal_spawn_errors
                                    .insert(issue_key, e.to_string());
                                changed = true;
                            }
                        }
                    }
                }
            }
        }

        // 2. Resize all sessions when dims change.
        // Track separately so the redraw decision below can gate on visibility.
        let mut resize_changed = false;
        if let Some((cols, rows)) = self.detail_terminal_pending_dims.get() {
            for sess in self.detail_terminal_sessions.values_mut() {
                if cols != sess.cols() || rows != sess.rows() {
                    sess.resize(cols, rows);
                    resize_changed = true;
                }
            }
        }

        // 3. Poll all sessions for output.
        //
        //    Note: #446 explored auto-injecting a kickoff prompt here
        //    (gated on `TerminalSession::bracketed_paste_enabled()` and
        //    later via a clipboard hand-off), but the readiness signal
        //    never flipped true through ssh.  The current design
        //    (#467) drops the auto-inject entirely: the launcher line
        //    runs `coord assign --interactive`, which uses the existing
        //    `interactive.py` seed path to land the briefing in the
        //    claude input box.  The TUI does not touch the PTY output
        //    beyond drawing it — the session stays strictly human-
        //    attended (Anthropic ToS §3.7 / #437).
        //
        //    Each poll is wrapped in catch_unwind (#597): vt100 parser bugs
        //    (unwrap-on-None, arithmetic overflow) can fire when parsing the
        //    final PTY flush on /exit.  Catching per-session ensures a single
        //    pane crash never aborts the TUI — the panicked session is removed
        //    and its error recorded; the post-exit board actions
        //    (detect_completed_interactive_work etc.) continue firing normally.
        //
        //    Track which session keys produced new output so we can suppress
        //    redraws for sessions that are not currently displayed (#789).
        let mut keys_changed: Vec<(String, u64)> = Vec::new();
        let mut panicked: Vec<((String, u64), String)> = Vec::new();
        for (key, sess) in self.detail_terminal_sessions.iter_mut() {
            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| sess.poll())) {
                Ok(c) => {
                    if c {
                        keys_changed.push(key.clone());
                    }
                }
                Err(e) => {
                    panicked.push((key.clone(), vt100_panic_to_string(&e)));
                    // Record the panicked key so the eviction below triggers a
                    // redraw when the affected session is the visible one.
                    keys_changed.push(key.clone());
                }
            }
        }
        // Evict sessions that panicked (done outside the borrow to avoid
        // simultaneous mutable borrows of detail_terminal_sessions).
        //
        // #672: store a clean "session ended" notice rather than a raw
        // panic string.  Evicting the session (not leaving it as exited)
        // has the same effect on session_pane_live() — it returns false
        // — so the board-driven detection functions
        // (detect_completed_interactive_work, detect_review_verdict,
        // detect_test_verdict) fire on the next tick exactly as they
        // would for a normally-exited session.
        for (key, msg) in panicked {
            self.detail_terminal_sessions.remove(&key);
            self.detail_terminal_spawn_errors
                .insert(key, format!("Session ended (renderer fault: {msg})"));
            // #816: surface a dismissible modal for explicit operator notification.
            self.report_terminal_panic(msg);
        }

        // Only signal a repaint when the Terminal tab is currently displayed.
        // Off-screen sessions have already had their output consumed into the
        // VT100 scrollback buffer (no output is lost); suppressing the redraw
        // eliminates spurious full-screen repaints from background PTY ticks.
        // The view/tab-switch event handler sets `needs_redraw = true`, so
        // accumulated content paints immediately when the user navigates here.
        //
        // `changed` here captures only section-1 spawn events, which are
        // themselves gated on the Terminal tab being visible — so when the
        // tab is off-screen, `changed` is always false and we return it as-is.
        let terminal_tab_visible = (self.active_view == SidebarView::Pipeline
            && self.pipeline_detail_tab == PipelineDetailTab::Terminal)
            || (self.active_view == SidebarView::Board
                && self.board_detail_tab == BoardDetailTab::Terminal);

        if !terminal_tab_visible {
            return changed; // spawn can't happen off-screen → always false here
        }

        // Terminal tab is visible: propagate resize events and check whether
        // the currently-displayed session produced new output.
        let visible_key_changed = self
            .selected_issue_key()
            .map_or(false, |k| keys_changed.contains(&k));
        changed || resize_changed || visible_key_changed
    }

    /// Forward a keypress to the selected issue's detail terminal PTY (#440).
    ///
    /// Mirrors `forward_key_to_pty` but routes to the per-issue session map
    /// rather than the standalone terminal session.
    pub(crate) fn forward_key_to_detail_terminal(
        &mut self,
        key: &Key,
        mods: &quadraui::Modifiers,
    ) -> bool {
        let Some(issue_key) = self.selected_issue_key() else {
            return false;
        };
        let Some(sess) = self.detail_terminal_sessions.get_mut(&issue_key) else {
            return false;
        };
        if sess.is_exited() {
            // Swallow the keypress — don't let stray keys drive TUI nav
            // while the dead-process pane is shown.
            return true;
        }
        sess.scroll_reset();
        if let Some(bytes) = key_to_pty_bytes(key.clone(), *mods) {
            sess.write_input(&bytes);
        }
        true
    }

    /// Forward a clipboard paste to the selected issue's detail terminal
    /// PTY (#468).
    ///
    /// Mirrors `forward_paste_to_pty` but routes to the per-issue session
    /// map.  When bracketed-paste mode (DEC private mode 2004) is active
    /// the text is wrapped in `ESC[200~` … `ESC[201~`; otherwise it is
    /// sent as raw bytes.  No trailing carriage-return is appended.
    ///
    /// Returns `true` when the paste was consumed; `false` when no live
    /// session exists for the selected issue.
    pub(crate) fn forward_paste_to_detail_terminal(&mut self, text: &str) -> bool {
        let Some(issue_key) = self.selected_issue_key() else {
            return false;
        };
        let Some(sess) = self.detail_terminal_sessions.get_mut(&issue_key) else {
            return false;
        };
        if sess.is_exited() {
            return true;
        }
        sess.scroll_reset();
        if sess.bracketed_paste_enabled() {
            let mut bytes = Vec::with_capacity(text.len() + 12);
            bytes.extend_from_slice(b"\x1b[200~");
            bytes.extend_from_slice(text.as_bytes());
            bytes.extend_from_slice(b"\x1b[201~");
            sess.write_input(&bytes);
        } else {
            sess.write_input(text.as_bytes());
        }
        true
    }
}

/// Convert a `Key` + `Modifiers` pair to the byte sequence sent to a
/// PTY (#424).  Mirrors the helper that ships in
/// `quadraui/examples/common/terminal_app.rs` — re-implemented here
/// because the example file is not part of the published API.
///
/// Covers the common VT100 / xterm-256color sequences (cursor keys,
/// function keys, Home/End/PgUp/PgDn, Ctrl+letter -> control codes,
/// printable chars).  Keys with no PTY mapping (CapsLock, etc.) return
/// `None`.
pub(crate) fn key_to_pty_bytes(key: Key, mods: quadraui::Modifiers) -> Option<Vec<u8>> {
    match key {
        Key::Char(ch) => {
            if mods.ctrl {
                // Ctrl+A..Ctrl+Z → bytes 0x01..0x1A.
                let c = ch.to_ascii_uppercase();
                if c.is_ascii_alphabetic() {
                    return Some(vec![c as u8 - b'@']);
                }
                // Ctrl+[ → ESC, Ctrl+\ → FS, Ctrl+] → GS, Ctrl+^ → RS, Ctrl+_ → US.
                match ch {
                    '[' => return Some(vec![0x1b]),
                    '\\' => return Some(vec![0x1c]),
                    ']' => return Some(vec![0x1d]),
                    '^' => return Some(vec![0x1e]),
                    '_' => return Some(vec![0x1f]),
                    _ => {}
                }
            }
            // Regular printable character — encode as UTF-8.
            let mut buf = [0u8; 4];
            let s = ch.encode_utf8(&mut buf);
            Some(s.as_bytes().to_vec())
        }
        Key::Named(named) => named_key_to_pty_bytes(named, mods),
    }
}

/// Helper for [`key_to_pty_bytes`] — maps named keys to escape sequences.
pub(crate) fn named_key_to_pty_bytes(key: quadraui::NamedKey, mods: quadraui::Modifiers) -> Option<Vec<u8>> {
    use quadraui::NamedKey;
    let mod_param = pty_modifier_param(mods);
    match key {
        NamedKey::Enter => Some(b"\r".to_vec()),
        NamedKey::Tab => {
            if mods.shift {
                Some(b"\x1b[Z".to_vec()) // back-tab
            } else {
                Some(b"\t".to_vec())
            }
        }
        NamedKey::BackTab => Some(b"\x1b[Z".to_vec()),
        NamedKey::Backspace => Some(b"\x7f".to_vec()),
        NamedKey::Delete => Some(pty_xterm_seq(b"3", mod_param)),
        NamedKey::Escape => Some(b"\x1b".to_vec()),
        NamedKey::Up => Some(pty_xterm_cursor_seq(b"A", mod_param)),
        NamedKey::Down => Some(pty_xterm_cursor_seq(b"B", mod_param)),
        NamedKey::Right => Some(pty_xterm_cursor_seq(b"C", mod_param)),
        NamedKey::Left => Some(pty_xterm_cursor_seq(b"D", mod_param)),
        NamedKey::Home => Some(pty_xterm_seq(b"1", mod_param)),
        NamedKey::End => Some(pty_xterm_seq(b"4", mod_param)),
        NamedKey::Insert => Some(pty_xterm_seq(b"2", mod_param)),
        NamedKey::PageUp => Some(pty_xterm_seq(b"5", mod_param)),
        NamedKey::PageDown => Some(pty_xterm_seq(b"6", mod_param)),
        NamedKey::F(n) => pty_f_key_bytes(n, mod_param),
        // Keys with no PTY mapping.
        NamedKey::CapsLock | NamedKey::NumLock | NamedKey::ScrollLock | NamedKey::Menu => None,
    }
}

/// Build an xterm modifier parameter (1-based; plain = `None`).
pub(crate) fn pty_modifier_param(mods: quadraui::Modifiers) -> Option<u8> {
    let n: u8 = 1
        + if mods.shift { 1 } else { 0 }
        + if mods.alt { 2 } else { 0 }
        + if mods.ctrl { 4 } else { 0 };
    if n == 1 {
        None
    } else {
        Some(n)
    }
}

/// Build `\x1b[<code>~` or `\x1b[<code>;<mod>~` for tilde-terminated sequences.
pub(crate) fn pty_xterm_seq(code: &[u8], mod_param: Option<u8>) -> Vec<u8> {
    let mut v = b"\x1b[".to_vec();
    v.extend_from_slice(code);
    if let Some(m) = mod_param {
        v.push(b';');
        v.push(b'0' + m);
    }
    v.push(b'~');
    v
}

/// Build cursor-movement sequences: `\x1b[<letter>` or `\x1b[1;<mod><letter>`.
pub(crate) fn pty_xterm_cursor_seq(letter: &[u8], mod_param: Option<u8>) -> Vec<u8> {
    match mod_param {
        None => {
            let mut v = b"\x1b[".to_vec();
            v.extend_from_slice(letter);
            v
        }
        Some(m) => {
            let mut v = b"\x1b[1;".to_vec();
            v.push(b'0' + m);
            v.extend_from_slice(letter);
            v
        }
    }
}

/// Function-key byte sequences (xterm encoding).
pub(crate) fn pty_f_key_bytes(n: u8, mod_param: Option<u8>) -> Option<Vec<u8>> {
    let bytes = match n {
        1 => {
            if mod_param.is_none() {
                b"\x1bOP".to_vec()
            } else {
                pty_xterm_cursor_seq(b"P", mod_param)
            }
        }
        2 => {
            if mod_param.is_none() {
                b"\x1bOQ".to_vec()
            } else {
                pty_xterm_cursor_seq(b"Q", mod_param)
            }
        }
        3 => {
            if mod_param.is_none() {
                b"\x1bOR".to_vec()
            } else {
                pty_xterm_cursor_seq(b"R", mod_param)
            }
        }
        4 => {
            if mod_param.is_none() {
                b"\x1bOS".to_vec()
            } else {
                pty_xterm_cursor_seq(b"S", mod_param)
            }
        }
        5 => pty_xterm_seq(b"15", mod_param),
        6 => pty_xterm_seq(b"17", mod_param),
        7 => pty_xterm_seq(b"18", mod_param),
        8 => pty_xterm_seq(b"19", mod_param),
        9 => pty_xterm_seq(b"20", mod_param),
        10 => pty_xterm_seq(b"21", mod_param),
        11 => pty_xterm_seq(b"23", mod_param),
        12 => pty_xterm_seq(b"24", mod_param),
        _ => return None,
    };
    Some(bytes)
}

