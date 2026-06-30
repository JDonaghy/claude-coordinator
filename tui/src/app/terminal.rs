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
