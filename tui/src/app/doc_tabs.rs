//! Per-panel document tabs with VS Code preview/pin semantics (#2282, ms-65 §2).
//!
//! # What this is
//!
//! Today both detail panes are a pure function of the sidebar selection: move
//! the selection and the issue you were reading is gone. [`DocTabs`] adds a
//! second, *independent* notion of "what am I looking at" — an ordered set of
//! open documents per panel, one of them active, and **at most one preview
//! slot** — so two issues can be held open at once and returning to one is a
//! click rather than a re-navigation.
//!
//! # The model
//!
//! A document key is `(repo, issue_number)`. Each [`PanelScope`] owns its own
//! [`DocTabGroup`]: an ordered `Vec<DocKey>`, the index of the active tab, and
//! optionally the index of the single preview tab. The scope split is what
//! keeps Board's and Pipeline's sets from merging (contract §3b) — this slice
//! only wires `Board`, but the shape is the one #2284 inherits.
//!
//! # Open semantics (contract §2e)
//!
//! 1. Open a document that is already open → activate its tab. No new tab, no
//!    replace ([`DocTabGroup::open_preview`]'s first branch).
//! 2. Open a not-yet-open document while a preview tab exists → **replace the
//!    preview in place**: same index, same active state.
//! 3. Otherwise → append a new preview tab and activate it.
//! 4. Pinning ([`DocTabGroup::pin`]) is open-or-activate *followed by*
//!    promotion — it drops `is_preview`, which is what makes the tab stop being
//!    the replaceable slot.
//!
//! Invariant, checked by `debug_assert` on every mutation: there is never more
//! than one preview tab, and `preview`/`active` always index a live tab.
//!
//! # Why not quadraui's `TabGroupController`
//!
//! Its content is a `Box<dyn BackendWidget>`, which cannot borrow `&CoordApp`
//! — the detail pane's content is derived from app state on every frame, so
//! there is nothing to box. quadraui #596/#597 (`WorkspaceController` + the
//! preview tier) are the intended long-term home for this model and do not
//! exist at any pushed quadraui rev yet; when they land, this module is the
//! thing to delete in favour of them. The public surface here is deliberately
//! small and free of rendering concerns so that swap stays cheap.

use crate::app::format::trunc;

/// Which panel's document set a tab belongs to.
///
/// Board's and Pipeline's tab sets, active tabs and preview slots are stored
/// per scope and never merge (contract §3b).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) enum PanelScope {
    Board,
    /// Reserved for #2284, which gives the Pipeline panel its own tab set.
    /// [`DocTabs::group`]/[`DocTabs::group_mut`] already route to it, but no
    /// caller constructs this variant until that slice lands — hence the
    /// allow, which should be deleted (not widened) by #2284.
    #[allow(dead_code)]
    Pipeline,
}

/// A document key: which repo, which issue number.
pub(crate) type DocKey = (String, u64);

/// Maximum rendered width, in display columns, of a document tab's
/// `#<N> <title>` label — inclusive of the `#<N> ` prefix (contract §2b).
///
/// Contract note: this is a pinned constant of the milestone, not a value
/// derived from anything in the codebase. It is chosen to keep 3–4 tabs
/// visible in the 82-column main panel without truncating short titles.
pub(crate) const DOC_TAB_LABEL_COLS: usize = 20;

/// Plain-text marker prefixed to a preview tab's label (contract §1).
///
/// `∘ ` (U+2218 RING OPERATOR, one display column + one space). The preview
/// tab is *also* rendered italic via `TabItem::is_preview`, but a
/// `TestBackend` screen dump is symbols-only, so italic alone is not an
/// assertable fact. The marker is additive to the italic styling, never a
/// replacement for it, and is never itself truncated away — it pushes the
/// §2b budget out by its own 2 columns rather than eating into it.
pub(crate) const PREVIEW_MARKER: &str = "∘ ";

/// §4 (#2283) overflow affordances: baked into the leftmost/rightmost
/// *visible* tab's label when tabs exist beyond that edge of the strip.
///
/// Baked into label text for the same reason [`PREVIEW_MARKER`] and the
/// close glyph are (see [`doc_tab_label`]'s doc comment): quadraui's TUI
/// tab-bar rasteriser never paints scroll arrows itself —
/// `TuiBackend::draw_tab_bar` / `tab_bar_layout` hardcode
/// `scroll_arrow_width: 0.0` ("no scroll arrows in TUI") and simply honour
/// whatever `scroll_offset` the caller supplies — so the app has to paint
/// them. See `CoordApp::board_doc_tab_strip` (render.rs).
pub(crate) const SCROLL_LEFT_MARKER: char = '‹';
pub(crate) const SCROLL_RIGHT_MARKER: char = '›';

/// Truncate `s` to at most `max_cols` display columns, appending `…` (which
/// occupies the last column) when anything was dropped.
///
/// Distinct from [`trunc`], which hard-cuts with no ellipsis: the contract's
/// pinned tab labels (`"#101 Fix login race…"`) carry the ellipsis *inside*
/// the 20-column budget, so a 26-column label becomes 19 columns of text plus
/// the marker, not 20 plus a 21st column.
pub(crate) fn truncate_with_ellipsis(s: &str, max_cols: usize) -> String {
    if s.chars().count() <= max_cols {
        return s.to_string();
    }
    if max_cols == 0 {
        return String::new();
    }
    format!("{}…", trunc(s, max_cols - 1))
}

/// One panel's ordered set of open documents.
///
/// Field invariants (upheld by every mutator, asserted by
/// [`Self::debug_check`]):
/// - `active` and `preview`, when `Some`, are valid indices into `tabs`.
/// - `active` is `Some` iff `tabs` is non-empty.
/// - at most one preview tab exists, and it is `tabs[preview]`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct DocTabGroup {
    tabs: Vec<DocKey>,
    active: Option<usize>,
    preview: Option<usize>,
}

impl DocTabGroup {
    /// The open documents, in strip order.
    pub(crate) fn tabs(&self) -> &[DocKey] {
        &self.tabs
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.tabs.is_empty()
    }

    /// Index of the active tab, or `None` when nothing is open.
    pub(crate) fn active_index(&self) -> Option<usize> {
        self.active
    }

    /// The active document's key, or `None` when nothing is open.
    pub(crate) fn active_key(&self) -> Option<&DocKey> {
        self.active.and_then(|i| self.tabs.get(i))
    }

    /// Whether the tab at `idx` is the (single) preview tab.
    pub(crate) fn is_preview(&self, idx: usize) -> bool {
        self.preview == Some(idx)
    }

    /// Index of `key` in the strip, if it is open.
    pub(crate) fn index_of(&self, key: &DocKey) -> Option<usize> {
        self.tabs.iter().position(|k| k == key)
    }

    /// Contract §2e rules 1/2/4 — the single-click path.
    ///
    /// Already open → activate it, unchanged. Else a preview exists → replace
    /// it **in place** (same index, same active state). Else → append a new
    /// preview tab and activate it.
    ///
    /// Deliberately does *not* promote: rule 2 says an already-open tab is
    /// activated with "no new tab, no replace", and promoting the preview here
    /// would make the very next single click append instead of replace,
    /// breaking rule 4's "at most one preview tab per tab group, ever".
    pub(crate) fn open_preview(&mut self, key: DocKey) {
        if let Some(idx) = self.index_of(&key) {
            self.active = Some(idx);
        } else if let Some(slot) = self.preview {
            self.tabs[slot] = key;
            self.active = Some(slot);
        } else {
            self.tabs.push(key);
            let idx = self.tabs.len() - 1;
            self.preview = Some(idx);
            self.active = Some(idx);
        }
        self.debug_check();
    }

    /// Contract §2e rule 3 — the double-click path: open-or-activate exactly as
    /// [`Self::open_preview`] does, **then** promote the resulting tab to
    /// pinned.
    pub(crate) fn pin(&mut self, key: DocKey) {
        self.open_preview(key);
        self.promote_active();
    }

    /// Drop the active tab out of the preview slot, if it is in it. A no-op
    /// when the active tab is already pinned or nothing is open.
    pub(crate) fn promote_active(&mut self) {
        if self.preview.is_some() && self.preview == self.active {
            self.preview = None;
        }
        self.debug_check();
    }

    /// Activate the tab at `idx` from the tab strip itself.
    ///
    /// This is the milestone's **promote-on-select** trigger: selecting a
    /// preview tab directly (as opposed to re-opening its document from the
    /// sidebar, which rule 2 leaves untouched) is a deliberate "I want to keep
    /// this one" gesture, so it pins the tab. Returns `true` when anything
    /// changed, so the caller can decide whether a redraw is needed.
    pub(crate) fn activate_index(&mut self, idx: usize) -> bool {
        if idx >= self.tabs.len() {
            return false;
        }
        let changed = self.active != Some(idx) || self.preview == Some(idx);
        self.active = Some(idx);
        self.promote_active();
        changed
    }

    /// Contract §4 (#2283) — close the tab at `idx`. Returns `false` (no-op)
    /// when `idx` is out of range.
    ///
    /// **Active-neighbour rule**, pinned by the contract: closing the
    /// ACTIVE tab activates the tab immediately to its left, or — when the
    /// closed tab was the leftmost — the new leftmost. Both branches reduce
    /// to the same arithmetic: the tab that was at `idx.saturating_sub(1)`
    /// keeps that index after the removal (elements before `idx` are
    /// untouched by `Vec::remove(idx)`), so `active` becomes
    /// `idx.saturating_sub(1)` whenever `idx` was active. Closing the LAST
    /// remaining tab clears `active` to `None` (§4's empty state, #2283).
    ///
    /// Closing an INACTIVE tab leaves the active *document* unchanged: the
    /// active index is only re-based (decremented) when the closed tab sat
    /// to its left, never retargeted.
    ///
    /// The (single) preview slot follows the same re-basing: cleared if the
    /// closed tab WAS the preview, decremented if the preview sat to the
    /// closed tab's right, else untouched.
    pub(crate) fn close(&mut self, idx: usize) -> bool {
        if idx >= self.tabs.len() {
            return false;
        }
        let was_active = self.active == Some(idx);
        self.tabs.remove(idx);

        self.preview = match self.preview {
            Some(p) if p == idx => None,
            Some(p) if p > idx => Some(p - 1),
            other => other,
        };

        if self.tabs.is_empty() {
            self.active = None;
        } else if was_active {
            self.active = Some(idx.saturating_sub(1));
        } else if let Some(a) = self.active {
            if a > idx {
                self.active = Some(a - 1);
            }
        }
        self.debug_check();
        true
    }

    #[inline]
    fn debug_check(&self) {
        debug_assert!(
            self.active.map_or(self.tabs.is_empty(), |i| i < self.tabs.len()),
            "active index must point at a live tab: {self:?}"
        );
        debug_assert!(
            self.preview.map_or(true, |i| i < self.tabs.len()),
            "preview index must point at a live tab: {self:?}"
        );
    }
}

/// Every panel's document tabs, keyed by scope.
#[derive(Debug, Clone, Default)]
pub(crate) struct DocTabs {
    board: DocTabGroup,
    pipeline: DocTabGroup,
}

impl DocTabs {
    pub(crate) fn group(&self, scope: PanelScope) -> &DocTabGroup {
        match scope {
            PanelScope::Board => &self.board,
            PanelScope::Pipeline => &self.pipeline,
        }
    }

    pub(crate) fn group_mut(&mut self, scope: PanelScope) -> &mut DocTabGroup {
        match scope {
            PanelScope::Board => &mut self.board,
            PanelScope::Pipeline => &mut self.pipeline,
        }
    }
}

/// Build one document tab's rendered label (contract §2b/§2c/§2d/§1).
///
/// Shape, outermost first:
///
/// ```text
/// active   [∘ <repo> #101 Fix login race… ×]␠
/// inactive  ∘ <repo> #101 Fix login race… ×␠
///           ^ ^      ^                    ^
///           | |      |                    └─ §2d close glyph (TAB_CLOSE_CHAR)
///           | |      └─ §2b: `#<N> <title>` truncated to 20 columns
///           | └─ repo prefix, only when the open set spans >1 repo
///           └─ §1 preview marker, only on the preview tab
/// ```
///
/// The trailing space is the inter-tab separator: quadraui's TUI tab-bar
/// rasteriser paints labels back-to-back with no gap of its own, so without it
/// `#101 …×#102 …` would run together. It sits *outside* the §2c brackets,
/// which wrap the tab's own content only.
///
/// The whole tab — close glyph included — lives in `TabItem::label` rather than
/// being assembled from `TabBar::show_tab_close`, because the rasteriser paints
/// the close glyph *after* the label and follows it with a separator space:
/// there is no way to get the §2c closing `]` to land to the right of `×` via
/// that path. `is_preview` is still set on the `TabItem` so the italic styling
/// the contract asks for is real; the `∘ ` marker is the symbols-only stand-in
/// for it, not a substitute (see [`PREVIEW_MARKER`]).
pub(crate) fn doc_tab_label(
    repo: &str,
    number: u64,
    title: &str,
    show_repo: bool,
    is_preview: bool,
    is_active: bool,
) -> String {
    let base = truncate_with_ellipsis(&format!("#{number} {title}"), DOC_TAB_LABEL_COLS);
    let mut inner = String::new();
    if is_preview {
        inner.push_str(PREVIEW_MARKER);
    }
    if show_repo {
        inner.push_str(repo);
        inner.push(' ');
    }
    inner.push_str(&base);
    inner.push(' ');
    inner.push(quadraui::tui::TAB_CLOSE_CHAR);
    if is_active {
        format!("[{inner}] ")
    } else {
        format!("{inner} ")
    }
}

/// Character offset of the §2d close glyph within a rendered tab label, or
/// `None` if the label carries none (shouldn't happen — every tab built by
/// [`doc_tab_label`] appends exactly one, per
/// `every_label_carries_the_close_glyph` below). Used by the click
/// hit-test ([`resolve_doc_tab_click`]) to tell a click on the `×` from a
/// click on the rest of the tab.
///
/// The **last** occurrence is the close glyph, never the first:
/// [`doc_tab_label`] embeds the issue title verbatim (modulo truncation), so
/// a title like "Fix 2×2 grid" puts a `×` in the label's *body*. The glyph
/// [`doc_tab_label`] appends is always to the right of every title char
/// (only `]` and the separator space follow it), so scanning from the end
/// finds it unambiguously.
pub(crate) fn doc_tab_close_col(label: &str) -> Option<usize> {
    let total = label.chars().count();
    label
        .chars()
        .rev()
        .position(|c| c == quadraui::tui::TAB_CLOSE_CHAR)
        .map(|from_end| total - 1 - from_end)
}

/// Which part of a tab a resolved click landed on — contract §4's
/// "clicking a tab's `×` closes it; clicking its body activates it".
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum TabClickKind {
    /// Click landed on the tab's `×` close glyph. Index is into the strip.
    Close(usize),
    /// Click landed anywhere else on the tab. Index is into the strip.
    Body(usize),
}

/// Resolve a click at `click_x` (same coordinate space as `origin_x`)
/// against a rendered doc-tab strip's labels, honouring `scroll_offset`
/// exactly the way `hit_tab_index_from_labels` (dialogs.rs) does — tabs
/// before `scroll_offset` are skipped, and labels are walked left-to-right
/// from `origin_x` accumulating `chars().count()` widths, matching what the
/// TUI rasteriser actually paints (§0: every glyph this milestone
/// introduces, including `×`/`‹`/`›`, is one display column).
///
/// Deliberately reimplemented here rather than calling
/// `hit_tab_index_from_labels` and separately re-deriving each tab's start
/// column: the close-glyph offset needs the SAME cumulative-width walk that
/// function already does internally, and duplicating just the "where does
/// tab `idx` start" half without the shared loop would be the real second
/// algorithm the ms-65 design note warns against.
pub(crate) fn resolve_doc_tab_click(
    labels: &[&str],
    origin_x: f32,
    click_x: f32,
    scroll_offset: usize,
) -> Option<TabClickKind> {
    let mut cursor = origin_x;
    for (i, label) in labels.iter().enumerate().skip(scroll_offset) {
        let width = label.chars().count() as f32;
        let end = cursor + width;
        if click_x >= cursor && click_x < end {
            let offset_in_tab = (click_x - cursor).floor() as usize;
            return Some(match doc_tab_close_col(label) {
                Some(close_col) if close_col == offset_in_tab => TabClickKind::Close(i),
                _ => TabClickKind::Body(i),
            });
        }
        cursor = end;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(n: u64) -> DocKey {
        ("claude-coordinator".to_string(), n)
    }

    // ── model: contract §2e ──────────────────────────────────────────────

    #[test]
    fn single_open_appends_one_preview_tab_and_activates_it() {
        let mut g = DocTabGroup::default();
        g.open_preview(k(102));
        assert_eq!(g.tabs(), &[k(102)]);
        assert_eq!(g.active_index(), Some(0));
        assert!(g.is_preview(0));
    }

    #[test]
    fn second_open_replaces_the_preview_in_place() {
        let mut g = DocTabGroup::default();
        g.open_preview(k(102));
        g.open_preview(k(103));
        assert_eq!(g.tabs(), &[k(103)], "preview replaced, not appended");
        assert_eq!(g.active_index(), Some(0));
        assert!(g.is_preview(0));
    }

    #[test]
    fn opening_an_already_open_document_only_activates_it() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        g.open_preview(k(101));
        assert_eq!(g.tabs(), &[k(101), k(102)]);
        assert_eq!(g.active_index(), Some(0));
        assert!(
            g.is_preview(1),
            "activating a pinned tab must not promote or evict the open preview"
        );
    }

    #[test]
    fn pin_drops_the_preview_slot() {
        let mut g = DocTabGroup::default();
        g.pin(k(102));
        assert_eq!(g.tabs(), &[k(102)]);
        assert!(!g.is_preview(0));
    }

    #[test]
    fn a_pinned_tab_plus_an_open_yields_two_tabs_one_preview() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        assert_eq!(g.tabs(), &[k(101), k(102)]);
        assert!(!g.is_preview(0));
        assert!(g.is_preview(1));
    }

    /// The sequence contract §2e explicitly warns must not be conflated with
    /// the two-tab intermediate state above.
    #[test]
    fn three_pins_yield_three_pinned_tabs_with_the_last_active() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103));
        assert_eq!(g.tabs(), &[k(101), k(102), k(103)]);
        assert_eq!(g.active_index(), Some(2));
        assert!((0..3).all(|i| !g.is_preview(i)), "all three are pinned");
    }

    /// …and the branch that *does* extend it: a pin landing while a preview is
    /// open replaces that preview rather than appending (§2e's trace).
    #[test]
    fn pinning_while_a_preview_is_open_replaces_it_in_place() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        g.pin(k(103));
        assert_eq!(g.tabs(), &[k(101), k(103)], "#102 evicted, not kept");
        assert!(!g.is_preview(1));
    }

    #[test]
    fn never_more_than_one_preview_tab() {
        let mut g = DocTabGroup::default();
        for n in [101, 102, 103, 104, 105] {
            g.open_preview(k(n));
            assert_eq!(g.tabs().len(), 1, "preview slot is reused every time");
        }
    }

    #[test]
    fn activating_a_preview_tab_from_the_strip_promotes_it() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        assert!(g.is_preview(1));
        assert!(g.activate_index(1));
        assert!(!g.is_preview(1), "promote-on-select");
        assert_eq!(g.active_index(), Some(1));
    }

    #[test]
    fn activating_out_of_range_is_a_no_op() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert!(!g.activate_index(7));
        assert_eq!(g.active_index(), Some(0));
    }

    // ── close: contract §4 (#2283) ───────────────────────────────────────

    #[test]
    fn closing_the_active_rightmost_tab_activates_its_left_neighbour() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103));
        assert!(g.close(2), "#103 was active and rightmost");
        assert_eq!(g.tabs(), &[k(101), k(102)]);
        assert_eq!(g.active_index(), Some(1), "left neighbour (#102) activates");
    }

    #[test]
    fn closing_the_active_leftmost_tab_activates_the_new_leftmost() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103));
        g.activate_index(0); // #101 active, leftmost
        assert!(g.close(0));
        assert_eq!(g.tabs(), &[k(102), k(103)]);
        assert_eq!(
            g.active_index(),
            Some(0),
            "no left neighbour — the new leftmost (#102) activates, not an \
             index-1 underflow wrap to the end"
        );
    }

    #[test]
    fn closing_an_inactive_tab_leaves_the_active_document_unchanged() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103)); // active, index 2
        assert!(g.close(0)); // close #101, an inactive left neighbour
        assert_eq!(g.tabs(), &[k(102), k(103)]);
        assert_eq!(
            g.active_key(),
            Some(&k(103)),
            "the active DOCUMENT is unchanged — only its index re-based"
        );
        assert_eq!(g.active_index(), Some(1));
    }

    #[test]
    fn closing_the_last_open_tab_clears_active() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert!(g.close(0));
        assert!(g.is_empty());
        assert_eq!(g.active_index(), None);
    }

    #[test]
    fn closing_out_of_range_is_a_no_op() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert!(!g.close(7));
        assert_eq!(g.tabs(), &[k(101)]);
    }

    #[test]
    fn closing_the_preview_tab_clears_the_preview_slot() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102)); // preview, index 1
        assert!(g.close(1));
        assert_eq!(g.tabs(), &[k(101)]);
        assert!(!g.is_preview(0));
    }

    #[test]
    fn closing_a_tab_left_of_the_preview_rebases_the_preview_index() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.open_preview(k(103)); // preview, index 2
        g.activate_index(0); // move active off the preview so close(0) hits an inactive tab
        assert!(g.close(0));
        assert_eq!(g.tabs(), &[k(102), k(103)]);
        assert!(g.is_preview(1), "preview index re-based from 2 to 1");
    }

    // ── click resolution: contract §4 ─────────────────────────────────────

    #[test]
    fn doc_tab_close_col_finds_the_close_glyph() {
        let label = doc_tab_label("claude-coordinator", 101, "Fix login race timeout", false, false, false);
        // "#101 Fix login race… × " — 20-column base + a space, so × sits at
        // char index 21.
        assert_eq!(doc_tab_close_col(&label), Some(21));
    }

    #[test]
    fn doc_tab_close_col_skips_a_close_char_inside_the_title() {
        // The title itself contains `×` ("2×2"), which lands in the rendered
        // label verbatim. The close glyph is the LAST occurrence — the one
        // doc_tab_label appends — never the title's.
        let label = doc_tab_label("claude-coordinator", 104, "Fix 2×2 grid layout", false, false, false);
        let col = doc_tab_close_col(&label).expect("label carries a close glyph");
        let title_x = label
            .chars()
            .position(|c| c == quadraui::tui::TAB_CLOSE_CHAR)
            .unwrap();
        assert!(
            col > title_x,
            "close col {col} must be the trailing glyph, not the title's × at {title_x}: {label:?}"
        );
        // And it is genuinely the appended glyph: only the separator space
        // (and, on an active tab, `]`) may follow it.
        assert_eq!(
            label.chars().nth(col),
            Some(quadraui::tui::TAB_CLOSE_CHAR)
        );
        assert_eq!(label.chars().skip(col + 1).collect::<String>(), " ");
    }

    #[test]
    fn resolve_doc_tab_click_distinguishes_body_from_close() {
        // Two 4-char labels back-to-back: "ab×d" then "ef×h", starting at x=10.
        let labels = ["ab×d", "ef×h"];
        // Body click on the first tab.
        assert_eq!(
            resolve_doc_tab_click(&labels, 10.0, 10.5, 0),
            Some(TabClickKind::Body(0))
        );
        // Close click on the first tab's × (offset 2 within the label).
        assert_eq!(
            resolve_doc_tab_click(&labels, 10.0, 12.5, 0),
            Some(TabClickKind::Close(0))
        );
        // Close click on the second tab's ×, at absolute column 16.
        assert_eq!(
            resolve_doc_tab_click(&labels, 10.0, 16.5, 0),
            Some(TabClickKind::Close(1))
        );
        // Past the last tab.
        assert_eq!(resolve_doc_tab_click(&labels, 10.0, 18.5, 0), None);
    }

    #[test]
    fn resolve_doc_tab_click_honours_scroll_offset() {
        let labels = ["ab×d", "ef×h"];
        // With the first tab scrolled out, the second starts at origin_x.
        assert_eq!(
            resolve_doc_tab_click(&labels, 10.0, 12.5, 1),
            Some(TabClickKind::Close(1))
        );
        // A click before origin_x (where the hidden tab would have been)
        // never resolves to the hidden tab.
        assert_eq!(resolve_doc_tab_click(&labels, 10.0, 8.0, 1), None);
    }

    #[test]
    fn scopes_never_merge() {
        let mut t = DocTabs::default();
        t.group_mut(PanelScope::Board).pin(k(101));
        t.group_mut(PanelScope::Pipeline).pin(k(201));
        assert_eq!(t.group(PanelScope::Board).tabs(), &[k(101)]);
        assert_eq!(t.group(PanelScope::Pipeline).tabs(), &[k(201)]);
    }

    // ── labels: contract §2b/§2c/§2d/§1 ──────────────────────────────────

    #[test]
    fn truncate_with_ellipsis_keeps_short_strings_whole() {
        assert_eq!(truncate_with_ellipsis("#101 Fix", 20), "#101 Fix");
    }

    #[test]
    fn truncate_with_ellipsis_fits_the_ellipsis_inside_the_budget() {
        let out = truncate_with_ellipsis("#101 Fix login race timeout", 20);
        assert_eq!(out, "#101 Fix login race…");
        assert_eq!(out.chars().count(), 20);
    }

    #[test]
    fn truncate_with_ellipsis_zero_budget_is_empty() {
        assert_eq!(truncate_with_ellipsis("#101 Fix", 0), "");
    }

    /// Exact strings lifted from the ms-65 §2 mocks.
    #[test]
    fn pinned_inactive_label_matches_the_mock() {
        assert_eq!(
            doc_tab_label("claude-coordinator", 101, "Fix login race timeout", false, false, false),
            "#101 Fix login race… × "
        );
    }

    #[test]
    fn pinned_active_label_is_bracketed() {
        assert_eq!(
            doc_tab_label("claude-coordinator", 103, "Race condition in poller", false, false, true),
            "[#103 Race condition… ×] "
        );
    }

    #[test]
    fn preview_label_carries_the_marker_outside_the_20_column_budget() {
        let label =
            doc_tab_label("claude-coordinator", 102, "Auth token refresh bug", false, true, true);
        assert_eq!(label, "[∘ #102 Auth token ref… ×] ");
        assert!(label.contains("∘ #102 Auth token ref… ×"));
    }

    #[test]
    fn multi_repo_labels_carry_the_repo_prefix() {
        let label = doc_tab_label("quadraui", 597, "Preview tier", true, false, false);
        assert!(
            label.starts_with("quadraui #597 Preview tier"),
            "got {label:?}"
        );
    }

    #[test]
    fn every_label_carries_the_close_glyph() {
        for active in [false, true] {
            for preview in [false, true] {
                let label = doc_tab_label("r", 1, "t", false, preview, active);
                assert_eq!(
                    label.matches(quadraui::tui::TAB_CLOSE_CHAR).count(),
                    1,
                    "one close glyph per tab (active={active}, preview={preview})"
                );
            }
        }
    }
}
