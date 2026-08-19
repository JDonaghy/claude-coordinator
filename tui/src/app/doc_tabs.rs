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
//! keeps Board's and Pipeline's sets from merging (contract §3b) — #2282
//! wired `Board`; #2284 wires `Pipeline` against this same shape, one
//! `DocTabGroup` per scope, so the two never merge, reorder or drop into
//! each other when the active panel switches.
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

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::app::format::trunc;
use crate::app::types::{BoardData, BoardDetailTab, PipelineDetailTab};

/// Which panel's document set a tab belongs to.
///
/// Board's and Pipeline's tab sets, active tabs and preview slots are stored
/// per scope and never merge (contract §3b).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub(crate) enum PanelScope {
    Board,
    /// #2284: the Pipeline panel's own independent tab set — same
    /// preview/pin semantics as Board, stored and revealed against the
    /// Pipeline sidebar (never the Board's). See `pipeline.rs`'s
    /// "#2284 (ms-65 §3): Pipeline document tabs" section.
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

/// The detail pane's sub-state for **one document** (#2285, contract §5).
///
/// Before #2285 these lived as single fields on `CoordApp`, so with three tabs
/// open all three shared one sub-tab, one scroll offset and one expanded
/// stage: scroll one tab's Issue body and the other two jumped. Holding them
/// per document is what makes the strip a set of tabs rather than one pane
/// with a strip above it.
///
/// # Scope ownership
///
/// A record belongs to exactly one [`PanelScope`] (it is stored inside that
/// scope's [`DocTabGroup`]), so only that scope's fields are ever read or
/// written on it — Board touches [`Self::board_tab`], Pipeline touches
/// [`Self::pipeline_tab`] / [`Self::stage_scroll`] / [`Self::focused_stage`],
/// and the two never see each other's. [`Self::scroll`] is the pane's body
/// scroll under either scope (`CoordApp::detail_scroll` for Board,
/// `pipeline_detail_scroll` for Pipeline) — the same *meaning*, a different
/// live field, which is why it is one field here and not two.
///
/// # Lifetime
///
/// Created lazily on the first tab switch away from a document (see
/// `CoordApp::checkpoint_detail_sub_state`) and dropped by
/// [`DocTabGroup::prune_sub_state`] the moment its tab leaves the strip —
/// closed, or evicted from the preview slot. Contract §5: "closing a tab
/// discards its sub-state; re-opening the same issue starts from the
/// defaults."
///
/// Deliberately NOT persisted across a restart — that is #2286's slice.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct DetailSubState {
    /// Board scope: which Board detail sub-tab this document is on.
    pub(crate) board_tab: BoardDetailTab,
    /// Pipeline scope: which Pipeline detail sub-tab this document is on.
    pub(crate) pipeline_tab: PipelineDetailTab,
    /// The detail pane body's scroll offset (Issue/Log/Summary bodies).
    pub(crate) scroll: usize,
    /// Pipeline scope: the Overview sub-tab's own stage-content scroll.
    pub(crate) stage_scroll: usize,
    /// Pipeline scope: the focused/expanded stage on the Overview sub-tab.
    pub(crate) focused_stage: Option<usize>,
}

/// One panel's ordered set of open documents.
///
/// Field invariants (upheld by every mutator, asserted by
/// [`Self::debug_check`]):
/// - `active` and `preview`, when `Some`, are valid indices into `tabs`.
/// - `active` is `Some` iff `tabs` is non-empty.
/// - at most one preview tab exists, and it is `tabs[preview]`.
/// - `sub_state` only ever holds keys that are still in `tabs` (#2285).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct DocTabGroup {
    tabs: Vec<DocKey>,
    active: Option<usize>,
    preview: Option<usize>,
    /// #2285: per-document detail sub-state, keyed by the same [`DocKey`] the
    /// strip is ordered by. Sparse — a document that has never been switched
    /// away from has no entry and reads as [`DetailSubState::default`].
    sub_state: HashMap<DocKey, DetailSubState>,
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

    // ── #2285 (ms-65 §5): per-document detail sub-state ──────────────────

    /// `key`'s stored sub-state, or `None` for a document that has never been
    /// switched away from (callers read that as [`DetailSubState::default`] —
    /// contract §5's "defaults on open match today's defaults").
    pub(crate) fn sub_state(&self, key: &DocKey) -> Option<&DetailSubState> {
        self.sub_state.get(key)
    }

    /// Store `state` against `key`. A no-op for a key that is not open, so a
    /// checkpoint racing a close can never resurrect discarded sub-state.
    pub(crate) fn set_sub_state(&mut self, key: DocKey, state: DetailSubState) {
        if self.index_of(&key).is_some() {
            self.sub_state.insert(key, state);
        }
    }

    /// Contract §5: a tab that leaves the strip takes its sub-state with it.
    ///
    /// Called from every mutator that can *remove* a key — [`Self::close`] and
    /// [`Self::open_preview`]'s replace-in-place branch (which evicts the
    /// previous preview document just as surely as a close does). Re-opening
    /// the same issue number afterwards therefore starts from the defaults.
    fn prune_sub_state(&mut self) {
        if self.sub_state.is_empty() {
            return;
        }
        self.sub_state.retain(|k, _| self.tabs.contains(k));
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
        // #2285: the replace-in-place branch above evicted a document — its
        // sub-state goes with it, exactly as a close would discard it.
        self.prune_sub_state();
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
        // #2285 (§5): closing a tab discards its sub-state.
        self.prune_sub_state();
        self.debug_check();
        true
    }

    /// #2287 (ms-65 §8c): the tab context menu's "Close others" item —
    /// close every tab except the one at `idx`, which becomes the sole
    /// survivor at index 0 and the active tab. Returns `false` (no-op) when
    /// `idx` is out of range.
    ///
    /// Preserves `idx`'s preview-ness rather than always clearing it: §8c
    /// only pins the effect on tab count/order, and collapsing to a single
    /// tab that was already the preview should not silently promote it —
    /// that is "Pin tab"'s job, not "Close others"'s.
    pub(crate) fn close_others(&mut self, idx: usize) -> bool {
        if idx >= self.tabs.len() {
            return false;
        }
        let was_preview = self.preview == Some(idx);
        let survivor = self.tabs[idx].clone();
        self.tabs = vec![survivor];
        self.active = Some(0);
        self.preview = if was_preview { Some(0) } else { None };
        // #2285 (§5): every closed tab's sub-state goes with it.
        self.prune_sub_state();
        self.debug_check();
        true
    }

    /// #2287 (ms-65 §8c): the tab context menu's "Close all" item — close
    /// every open tab, reaching "the same end state as closing the last
    /// tab" (contract §8c / §4's empty state, #2283). Returns `false`
    /// (no-op) when nothing is open.
    pub(crate) fn close_all(&mut self) -> bool {
        if self.tabs.is_empty() {
            return false;
        }
        self.tabs.clear();
        self.active = None;
        self.preview = None;
        self.prune_sub_state();
        self.debug_check();
        true
    }

    /// #2287 (ms-65 §8c): the tab context menu's "Pin tab" item — promote
    /// the tab at `idx` out of the preview slot, without changing which tab
    /// is active or the strip's order.
    ///
    /// Distinct from [`Self::promote_active`] (§2e rule 3's click-driven
    /// path, which only ever acts on the ACTIVE tab): the context menu's
    /// "Pin tab" targets the RIGHT-CLICKED tab, which need not be the
    /// active one. A no-op — contract §8c's "inert" reading of "hidden or
    /// inert" on an already-pinned tab — when `idx` is not the (single)
    /// preview tab, whether because it's already pinned or out of range.
    pub(crate) fn promote(&mut self, idx: usize) -> bool {
        if self.preview == Some(idx) {
            self.preview = None;
            self.debug_check();
            true
        } else {
            false
        }
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

    // ── #2286 (ms-65 §6): persistence — drop dead documents on load ─────

    /// Contract §6 bullets 2/3: drop every tab whose key is not in `known` —
    /// e.g. its issue was closed and dropped from the board since this group
    /// was last persisted. "A surviving neighbour becomes active" and "if
    /// none survive, start with no tabs" both fall out of [`Self::close`]'s
    /// own already-tested active-neighbour rebasing, applied once per dead
    /// tab, rather than a second hand-rolled "pick a neighbour" algorithm:
    /// closing the tab at its own index rebases `active`/`preview` and
    /// prunes `sub_state` for it exactly as a live `Ctrl-W` close would.
    ///
    /// Walks indices highest-to-lowest so each `close(idx)` call still
    /// targets the right (not-yet-shifted) tab — [`Vec::remove`] only moves
    /// elements *after* the removed index, so indices below the one being
    /// processed are never disturbed by an earlier iteration.
    pub(crate) fn retain_known(&mut self, known: &HashSet<DocKey>) {
        for idx in (0..self.tabs.len()).rev() {
            if !known.contains(&self.tabs[idx]) {
                self.close(idx);
            }
        }
    }

    /// Rebuild a group from its persisted form ([`PersistedScope`]), trusting
    /// the file as-is. Pruning against the live board is the separate
    /// [`Self::retain_known`] step — at real startup (`CoordApp::new`) the
    /// board hasn't loaded yet, and pruning against an empty known-set would
    /// throw away everything this function just restored before the first
    /// real data tick ever reconciles it (mirrors `Workspace::load` /
    /// `sync_workspace_repos`, `app/workspace.rs`).
    fn from_persisted(scope: PersistedScope, owner: PanelScope) -> Self {
        let tabs: Vec<DocKey> = scope.tabs.iter().map(PersistedDoc::key).collect();
        let mut sub_state = HashMap::new();
        for doc in &scope.tabs {
            if let Some(state) = doc.sub_state(owner) {
                sub_state.insert(doc.key(), state);
            }
        }
        let mut group = DocTabGroup {
            tabs,
            active: None,
            preview: None,
            sub_state,
        };
        group.active = scope.active.as_ref().and_then(|d| group.index_of(&d.key()));
        group.preview = scope.preview.as_ref().and_then(|d| group.index_of(&d.key()));
        if group.active.is_none() && !group.tabs.is_empty() {
            // A malformed-but-parseable file (e.g. a `null` active alongside
            // a non-empty `tabs`, or an `active` key that isn't itself in
            // `tabs`) would otherwise violate `debug_check`'s "active is Some
            // iff tabs is non-empty" invariant for the rest of the group's
            // life. Anchor on the leftmost tab rather than carry that in.
            group.active = Some(0);
        }
        group.debug_check();
        group
    }

    /// This group's persisted form ([`PersistedScope`], contract §6).
    fn to_persisted(&self, owner: PanelScope) -> PersistedScope {
        let doc = |key: &DocKey| PersistedDoc::from_key(key, self.sub_state.get(key), owner);
        PersistedScope {
            tabs: self.tabs.iter().map(&doc).collect(),
            active: self.active_key().map(&doc),
            preview: self.preview.and_then(|i| self.tabs.get(i)).map(&doc),
        }
    }
}

/// Every panel's document tabs, keyed by scope.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
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

    /// Contract §6 bullets 2/3, applied to both scopes at once — the
    /// `CoordApp` integration point (`mod.rs`'s `sync_doc_tabs` for real
    /// startup, `fixtures.rs`'s `make_test_app` for the fixture path) calls
    /// this once real board data is known.
    pub(crate) fn retain_known(&mut self, known: &HashSet<DocKey>) {
        self.board.retain_known(known);
        self.pipeline.retain_known(known);
    }

    // ─── Persistence: `~/.coord/tabs.json` ──────────────────────────────

    /// Path to the persisted tabs file (`~/.coord/tabs.json`), or `None`
    /// when `HOME` is unset — matches `TuiSettings::path()` / `Workspace::path()`.
    ///
    /// # Test / `test-support` builds only: real, un-sandboxed `HOME` is refused
    ///
    /// `DocTabs` is unlike every sibling `~/.coord/*` persistence type in one
    /// respect: [`DocTabs::retain_known`] means a correct restore can only be
    /// observed by actually constructing a `CoordApp` from a fixture that
    /// already knows the board (`fixtures.rs::make_test_app`'s doc comment —
    /// and `tests/acceptance/ms-65/manifest.yml` finding 14 — explain why
    /// this is unavoidable), so — unlike `Workspace`'s fixtures, which derive
    /// fresh and never touch disk — that fixture path genuinely calls
    /// [`DocTabs::load`]. And unlike `Workspace`'s CoordApp-integration
    /// methods (`open_project`/`close_project`/…), which no existing test
    /// exercises, this crate's #2282-#2287 TuiDriver suites exercise
    /// `open_board_doc_tab`/`activate_board_doc_tab`/… (and therefore
    /// [`DocTabs::save_if_exists`]) *extensively* — hundreds of clicks across
    /// dozens of tests, none of which sandbox `HOME`.
    ///
    /// With no injectable `~/.coord` seam (`tests/acceptance/ms-65/
    /// manifest.yml` finding 14b — a repo-wide fix flagged there as the
    /// coordinator's follow-up, not this issue's), an unguarded `path()`
    /// would mean every one of those clicks reads and writes the REAL
    /// developer's `~/.coord/tabs.json` — verified while authoring this: it
    /// self-pollutes within a single `cargo test` process (one test's writes
    /// leak into the very next test's `DocTabs::load()`, since both hit the
    /// same real file) and even a single `q`-quit test creates the file for
    /// every other unsandboxed test to trip over afterward.
    ///
    /// The guard: `tests/acceptance/ms-65/tabs_persistence_2286.rs`'s own
    /// `HomeSandbox` (and any other test that wants real doc-tabs I/O)
    /// points `HOME` at a fresh directory under [`std::env::temp_dir`] — an
    /// ordinary developer or CI `$HOME` is never there. So under `#[cfg(test)]`
    /// / `test-support` only, `path()` additionally requires `HOME` to
    /// resolve inside the system temp directory; otherwise it refuses (same
    /// as `HOME` being unset) rather than touching a real home directory.
    /// Compiled out entirely for the shipped binary — real users are
    /// unaffected, exactly like `Workspace`/`TuiSettings` today.
    pub(crate) fn path() -> Option<PathBuf> {
        let home = std::env::var_os("HOME").map(PathBuf::from)?;
        #[cfg(any(test, feature = "test-support"))]
        {
            if !home.starts_with(std::env::temp_dir()) {
                return None;
            }
        }
        Some(home.join(".coord").join("tabs.json"))
    }

    /// Load from a specific path, trusting the file as-is (no pruning —
    /// see [`Self::retain_known`]). Contract §6 bullet 4: a missing file, an
    /// empty file, or a file that fails to parse all produce the SAME
    /// result — every scope starts with no tabs, never a panic, never a
    /// partial/best-effort parse. `serde_json::from_str` already rejects a
    /// truncated document wholesale (no partial-object recovery), so the
    /// malformed and empty cases fall out of the same `Err` branch as a
    /// missing file falls out of `read_to_string`'s `Err`.
    pub(crate) fn load_from_path(path: &std::path::Path) -> Self {
        let Ok(text) = std::fs::read_to_string(path) else {
            return Self::default();
        };
        match serde_json::from_str::<PersistedTabs>(&text) {
            Ok(persisted) => Self {
                board: DocTabGroup::from_persisted(persisted.board.unwrap_or_default(), PanelScope::Board),
                pipeline: DocTabGroup::from_persisted(
                    persisted.pipeline.unwrap_or_default(),
                    PanelScope::Pipeline,
                ),
            },
            Err(_) => Self::default(),
        }
    }

    /// Load from `~/.coord/tabs.json`. Returns the default (empty) tab set
    /// when `HOME` is unset, the file is absent, or it fails to parse.
    pub(crate) fn load() -> Self {
        match Self::path() {
            Some(path) => Self::load_from_path(&path),
            None => Self::default(),
        }
    }

    /// Persist to a specific path, creating parent directories as needed.
    pub(crate) fn save_to_path(&self, path: &std::path::Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("create tabs dir: {e}"))?;
        }
        let persisted = PersistedTabs {
            board: Some(self.board.to_persisted(PanelScope::Board)),
            pipeline: Some(self.pipeline.to_persisted(PanelScope::Pipeline)),
        };
        let text =
            serde_json::to_string_pretty(&persisted).map_err(|e| format!("serialize tabs: {e}"))?;
        std::fs::write(path, text).map_err(|e| format!("write tabs: {e}"))?;
        Ok(())
    }

    /// Persist to `~/.coord/tabs.json` unconditionally — creating the file
    /// if it doesn't exist yet. A no-op (`Ok`) when `HOME` is unset — the
    /// TUI stays functional without a home directory, it just won't
    /// remember open tabs across restarts.
    ///
    /// Called exactly once per run, on the way out (`CoordApp`'s `handle`
    /// wrapper in `render.rs`, on `Reaction::Exit`) — see [`Self::save_if_exists`]
    /// for the far more frequent mutation-triggered save, which deliberately
    /// does NOT create a fresh file.
    pub(crate) fn save(&self) -> Result<(), String> {
        match Self::path() {
            Some(path) => self.save_to_path(&path),
            None => Ok(()),
        }
    }

    /// Persist to `~/.coord/tabs.json`, but ONLY when that file already
    /// exists — never creates it. A no-op (`Ok`) when `HOME` is unset or the
    /// file isn't there yet.
    ///
    /// Called from every tab mutator (open/pin/activate/close/…, both
    /// scopes) so a restored session (or one that has already quit once,
    /// creating the file via [`Self::save`]) stays fresh in near-real-time
    /// rather than only on the next clean exit. Deliberately does NOT create
    /// a brand-new file: this crate's `#[cfg(test)]`/`test-support` fixture
    /// path (`fixtures.rs::make_test_app`) reads real `~/.coord/tabs.json`
    /// state at construction (contract §6 / #2286 needs this — see that
    /// function's doc comment), and the overwhelming majority of this
    /// crate's TuiDriver click-driven test suites (#2282-#2287) build their
    /// app this way WITHOUT sandboxing `HOME`. If every click that opens or
    /// activates a tab could CREATE `~/.coord/tabs.json` on whatever machine
    /// is running the tests, those tests would start writing into the real
    /// developer's home directory and — worse — polluting every other
    /// unsandboxed test that runs in the same process afterward, since
    /// `DocTabs::load()` would then pick up whatever a completely unrelated
    /// test just wrote. Gating on "already exists" closes that hole while
    /// still satisfying contract §6 exactly: a session that has quit once
    /// (creating the file) keeps it live-updated afterward, and the sealed
    /// acceptance slice's own pruning test (`tabs_persistence_2286::
    /// a_document_whose_issue_is_absent_from_the_board_is_pruned_on_load`)
    /// seeds the file BEFORE the mutation it checks, so this still fires for
    /// it.
    pub(crate) fn save_if_exists(&self) -> Result<(), String> {
        let Some(path) = Self::path() else {
            return Ok(());
        };
        if !path.exists() {
            return Ok(());
        }
        self.save_to_path(&path)
    }
}

// ─── §6's pinned file shape ──────────────────────────────────────────────

/// One document key as written to `~/.coord/tabs.json` — `{"repo": …,
/// "issue": …}`, contract §6's pinned shape, verbatim. Also carries the
/// owning scope's "cheap" per-tab sub-state (§6's last bullet: the sub-tab
/// selection, persisted "as a string"; scroll offsets are deliberately NOT
/// here — the issue explicitly allows dropping them on restart, so they
/// simply reset to defaults on load) under the field name matching the
/// document's `active`/`preview` role: the tabs-list entry itself.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
struct PersistedDoc {
    repo: String,
    issue: u64,
    /// The sub-tab this document was on — `"board"`/`"issue"`/`"chat"`/
    /// `"terminal"` for a Board-scope document, `"overview"`/`"issue"`/
    /// `"log"`/`"summary"`/`"terminal"`/`"completed"` for a Pipeline-scope
    /// one. Absent on an older file, or when the document was never
    /// switched away from (`DetailSubState` is sparse, contract §5) — reads
    /// back as the scope's default sub-tab either way. An unrecognised
    /// string (future variant, hand-edited file) is likewise read as the
    /// default rather than rejecting the whole document.
    #[serde(skip_serializing_if = "Option::is_none")]
    sub_tab: Option<String>,
}

impl PersistedDoc {
    fn key(&self) -> DocKey {
        (self.repo.clone(), self.issue)
    }

    /// This document's restored sub-state, under `owner`'s scope — `None`
    /// when `sub_tab` is absent or unrecognised, which callers read as "no
    /// entry", exactly like a document that was never checkpointed.
    fn sub_state(&self, owner: PanelScope) -> Option<DetailSubState> {
        let s = self.sub_tab.as_deref()?;
        match owner {
            PanelScope::Board => board_tab_from_str(s).map(|board_tab| DetailSubState {
                board_tab,
                ..DetailSubState::default()
            }),
            PanelScope::Pipeline => pipeline_tab_from_str(s).map(|pipeline_tab| DetailSubState {
                pipeline_tab,
                ..DetailSubState::default()
            }),
        }
    }

    /// Build the persisted form of `key`, pulling `owner`'s sub-tab out of
    /// `state` (`None` for `active`/`preview`'s call sites, which pass
    /// `self.sub_state.get(key)` same as a tabs-list entry would — the
    /// document's sub-tab is a property of the document, not of which role
    /// it plays in the scope).
    fn from_key(key: &DocKey, state: Option<&DetailSubState>, owner: PanelScope) -> Self {
        let sub_tab = state.map(|s| match owner {
            PanelScope::Board => board_tab_to_str(s.board_tab).to_string(),
            PanelScope::Pipeline => pipeline_tab_to_str(s.pipeline_tab).to_string(),
        });
        PersistedDoc {
            repo: key.0.clone(),
            issue: key.1,
            sub_tab,
        }
    }
}

fn board_tab_to_str(tab: BoardDetailTab) -> &'static str {
    match tab {
        BoardDetailTab::Board => "board",
        BoardDetailTab::Issue => "issue",
        BoardDetailTab::Chat => "chat",
        BoardDetailTab::Terminal => "terminal",
    }
}

fn board_tab_from_str(s: &str) -> Option<BoardDetailTab> {
    match s {
        "board" => Some(BoardDetailTab::Board),
        "issue" => Some(BoardDetailTab::Issue),
        "chat" => Some(BoardDetailTab::Chat),
        "terminal" => Some(BoardDetailTab::Terminal),
        _ => None,
    }
}

fn pipeline_tab_to_str(tab: PipelineDetailTab) -> &'static str {
    match tab {
        PipelineDetailTab::Overview => "overview",
        PipelineDetailTab::Issue => "issue",
        PipelineDetailTab::Log => "log",
        PipelineDetailTab::Summary => "summary",
        PipelineDetailTab::Terminal => "terminal",
        PipelineDetailTab::Completed => "completed",
    }
}

fn pipeline_tab_from_str(s: &str) -> Option<PipelineDetailTab> {
    match s {
        "overview" => Some(PipelineDetailTab::Overview),
        "issue" => Some(PipelineDetailTab::Issue),
        "log" => Some(PipelineDetailTab::Log),
        "summary" => Some(PipelineDetailTab::Summary),
        "terminal" => Some(PipelineDetailTab::Terminal),
        "completed" => Some(PipelineDetailTab::Completed),
        _ => None,
    }
}

/// One scope's persisted object: ordered `tabs`, `active`, `preview`
/// (contract §6's pinned shape).
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
struct PersistedScope {
    tabs: Vec<PersistedDoc>,
    active: Option<PersistedDoc>,
    preview: Option<PersistedDoc>,
}

/// The whole `~/.coord/tabs.json` (contract §6). Top-level keys are the
/// lowercase `PanelScope` names in use today — `serde`'s default
/// `snake_case`-of-the-Rust-field-name behaviour already produces exactly
/// `"board"`/`"pipeline"` from these field names, so no `#[serde(rename)]`
/// is needed. A scope absent from the file starts with no tabs.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
struct PersistedTabs {
    board: Option<PersistedScope>,
    pipeline: Option<PersistedScope>,
}

/// #2286 (ms-65 §6): every document key currently known to the loaded board —
/// the set [`DocTabs::retain_known`] prunes against, both on load and after
/// every real data refresh ("drop any document whose issue is absent from
/// the board"). Sourced from `open_issues` only, mirroring
/// `board_data_from_payload`'s `"issues"` → `open_issues` mapping every
/// fixture in this suite seeds through.
pub(crate) fn known_doc_keys(data: &BoardData) -> HashSet<DocKey> {
    data.open_issues
        .iter()
        .map(|oi| (oi.repo_name.clone(), oi.number))
        .collect()
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

    // ── #2287 (ms-65 §8c): tab context menu — close_others / close_all / promote ──

    #[test]
    fn close_others_leaves_only_the_target_tab_active() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103));
        assert!(g.close_others(1));
        assert_eq!(g.tabs(), &[k(102)]);
        assert_eq!(g.active_index(), Some(0));
    }

    #[test]
    fn close_others_out_of_range_is_a_no_op() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        assert!(!g.close_others(9));
        assert_eq!(g.tabs(), &[k(101), k(102)]);
    }

    #[test]
    fn close_others_preserves_the_survivors_preview_state() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        assert!(g.is_preview(1));
        assert!(g.close_others(1));
        assert_eq!(g.tabs(), &[k(102)]);
        assert!(g.is_preview(0), "the survivor was the preview tab");
    }

    #[test]
    fn close_all_empties_the_group() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        assert!(g.close_all());
        assert!(g.is_empty());
        assert_eq!(g.active_index(), None);
    }

    #[test]
    fn close_all_on_an_empty_group_is_a_no_op() {
        let mut g = DocTabGroup::default();
        assert!(!g.close_all());
    }

    #[test]
    fn promote_drops_the_preview_marker_without_moving_active_or_order() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.open_preview(k(102));
        assert_eq!(g.active_index(), Some(1), "single click activates #102");
        // Re-activate #101 so the promoted tab (idx 0) is NOT the active one —
        // "Pin tab" must target the clicked tab, not whichever is active.
        g.activate_index(0);
        assert_eq!(g.active_index(), Some(0));
        assert!(g.is_preview(1));
        assert!(g.promote(1));
        assert!(!g.is_preview(1));
        assert_eq!(g.tabs(), &[k(101), k(102)], "order unchanged");
        assert_eq!(g.active_index(), Some(0), "active tab unchanged");
    }

    #[test]
    fn promote_on_an_already_pinned_tab_is_a_no_op() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert!(!g.promote(0));
        assert_eq!(g.tabs(), &[k(101)]);
    }

    #[test]
    fn promote_out_of_range_is_a_no_op() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert!(!g.promote(9));
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

    // ── per-document sub-state: contract §5 (#2285) ──────────────────────

    fn issue_sub_state(scroll: usize) -> DetailSubState {
        DetailSubState {
            board_tab: BoardDetailTab::Issue,
            scroll,
            ..DetailSubState::default()
        }
    }

    #[test]
    fn sub_state_round_trips_for_an_open_document() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        assert_eq!(g.sub_state(&k(101)), None, "sparse until first written");
        g.set_sub_state(k(101), issue_sub_state(5));
        assert_eq!(g.sub_state(&k(101)), Some(&issue_sub_state(5)));
    }

    #[test]
    fn set_sub_state_ignores_a_document_that_is_not_open() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.set_sub_state(k(999), issue_sub_state(5));
        assert_eq!(
            g.sub_state(&k(999)),
            None,
            "a checkpoint racing a close must not resurrect discarded state"
        );
    }

    #[test]
    fn closing_a_tab_discards_its_sub_state() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.set_sub_state(k(101), issue_sub_state(5));
        assert!(g.close(0));
        g.pin(k(101));
        assert_eq!(
            g.sub_state(&k(101)),
            None,
            "§5: re-opening the same issue number starts from the defaults"
        );
    }

    #[test]
    fn closing_one_tab_leaves_the_other_tabs_sub_state_alone() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.set_sub_state(k(101), issue_sub_state(5));
        g.set_sub_state(k(102), issue_sub_state(9));
        assert!(g.close(1));
        assert_eq!(g.sub_state(&k(101)), Some(&issue_sub_state(5)));
        assert_eq!(g.sub_state(&k(102)), None);
    }

    /// Replace-in-place evicts a document just as surely as a close does, so
    /// its record has to go with it — otherwise re-opening that issue into a
    /// later preview slot would resume sub-state §5 says was discarded.
    #[test]
    fn evicting_the_preview_document_discards_its_sub_state() {
        let mut g = DocTabGroup::default();
        g.open_preview(k(101));
        g.set_sub_state(k(101), issue_sub_state(5));
        g.open_preview(k(102)); // replaces #101 in place
        assert_eq!(g.tabs(), &[k(102)]);
        assert_eq!(g.sub_state(&k(101)), None);
        g.open_preview(k(101));
        assert_eq!(g.sub_state(&k(101)), None);
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

    // ── #2286 (ms-65 §6): persistence — retain_known / save / load ────────

    #[test]
    fn retain_known_drops_a_dead_tab_and_leaves_the_rest_alone() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(103)); // active
        let known: HashSet<DocKey> = [k(101), k(103)].into_iter().collect();
        g.retain_known(&known);
        assert_eq!(g.tabs(), &[k(101), k(103)]);
        assert_eq!(g.active_key(), Some(&k(103)), "active tab survived, untouched");
    }

    #[test]
    fn retain_known_activates_a_surviving_neighbour_when_active_is_pruned() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(102));
        g.pin(k(199)); // active, about to be pruned
        let known: HashSet<DocKey> = [k(101), k(102)].into_iter().collect();
        g.retain_known(&known);
        assert_eq!(g.tabs(), &[k(101), k(102)]);
        assert!(
            g.active_key() == Some(&k(101)) || g.active_key() == Some(&k(102)),
            "a surviving neighbour must become active, got {:?}",
            g.active_key()
        );
    }

    #[test]
    fn retain_known_empties_the_group_when_nothing_survives() {
        let mut g = DocTabGroup::default();
        g.pin(k(198));
        g.pin(k(199));
        g.retain_known(&HashSet::new());
        assert!(g.is_empty());
        assert_eq!(g.active_index(), None);
    }

    #[test]
    fn retain_known_discards_a_dead_tabs_sub_state() {
        let mut g = DocTabGroup::default();
        g.pin(k(101));
        g.pin(k(199));
        g.set_sub_state(k(199), issue_sub_state(7));
        let known: HashSet<DocKey> = [k(101)].into_iter().collect();
        g.retain_known(&known);
        assert_eq!(g.sub_state(&k(199)), None);
    }

    fn tmp_json_path(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "coord_doc_tabs_test_{tag}_{}.json",
            std::process::id()
        ))
    }

    #[test]
    fn save_then_load_round_trips_three_pinned_board_tabs() {
        let path = tmp_json_path("save_board_pins");
        let mut tabs = DocTabs::default();
        {
            let g = tabs.group_mut(PanelScope::Board);
            g.pin(k(101));
            g.pin(k(102));
            g.pin(k(103));
        }
        tabs.save_to_path(&path).expect("save should succeed");
        let raw = std::fs::read_to_string(&path).unwrap();
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        // Shape-level: `active`/`preview` are document keys or `null`.
        assert!(raw.contains("\"board\""));
        assert!(raw.contains("\"pipeline\""));

        let g = loaded.group(PanelScope::Board);
        assert_eq!(g.tabs(), &[k(101), k(102), k(103)]);
        assert_eq!(g.active_key(), Some(&k(103)));
        assert!(!g.is_preview(0) && !g.is_preview(1) && !g.is_preview(2));
    }

    #[test]
    fn save_then_load_round_trips_a_preview_tab_as_a_preview_tab() {
        let path = tmp_json_path("save_preview");
        let mut tabs = DocTabs::default();
        {
            let g = tabs.group_mut(PanelScope::Board);
            g.pin(k(101));
            g.open_preview(k(102)); // preview, active
        }
        tabs.save_to_path(&path).expect("save should succeed");
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        let g = loaded.group(PanelScope::Board);
        assert_eq!(g.tabs(), &[k(101), k(102)]);
        assert!(!g.is_preview(0));
        assert!(g.is_preview(1), "the preview tab must be restored AS a preview, not promoted");
        assert_eq!(g.active_key(), Some(&k(102)));
    }

    #[test]
    fn save_then_load_keeps_board_and_pipeline_scopes_separate() {
        let path = tmp_json_path("save_scopes");
        let mut tabs = DocTabs::default();
        tabs.group_mut(PanelScope::Board).pin(k(101));
        tabs.group_mut(PanelScope::Pipeline).pin(k(201));
        tabs.save_to_path(&path).expect("save");
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        assert_eq!(loaded.group(PanelScope::Board).tabs(), &[k(101)]);
        assert_eq!(loaded.group(PanelScope::Pipeline).tabs(), &[k(201)]);
    }

    #[test]
    fn load_from_path_missing_file_starts_with_no_tabs() {
        let path = tmp_json_path("missing_9999999");
        let _ = std::fs::remove_file(&path);
        let loaded = DocTabs::load_from_path(&path);
        assert!(loaded.group(PanelScope::Board).is_empty());
        assert!(loaded.group(PanelScope::Pipeline).is_empty());
    }

    #[test]
    fn load_from_path_empty_file_starts_with_no_tabs() {
        let path = tmp_json_path("empty");
        std::fs::write(&path, b"").unwrap();
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        assert!(loaded.group(PanelScope::Board).is_empty());
        assert!(loaded.group(PanelScope::Pipeline).is_empty());
    }

    #[test]
    fn load_from_path_malformed_json_starts_with_no_tabs_never_a_partial_parse() {
        let path = tmp_json_path("malformed");
        std::fs::write(
            &path,
            b"{\"board\": {\"tabs\": [{\"repo\": \"r\", \"issue\": 101},",
        )
        .unwrap();
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);
        assert!(
            loaded.group(PanelScope::Board).is_empty(),
            "a truncated file must not recover its well-formed prefix"
        );
        assert!(loaded.group(PanelScope::Pipeline).is_empty());
    }

    #[test]
    fn load_from_path_prunes_a_document_whose_issue_is_gone() {
        // #199 is not in `known` at restore time (simulated directly against
        // a persisted-shaped file, mirroring what `retain_known` does once
        // real board data is available).
        let path = tmp_json_path("prune_on_load");
        let raw = r#"{"board": {"tabs": [
            {"repo": "claude-coordinator", "issue": 101},
            {"repo": "claude-coordinator", "issue": 199},
            {"repo": "claude-coordinator", "issue": 102}
        ], "active": {"repo": "claude-coordinator", "issue": 101}, "preview": null}}"#;
        std::fs::write(&path, raw).unwrap();
        let mut loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        let known: HashSet<DocKey> = [k(101), k(102)].into_iter().collect();
        loaded.retain_known(&known);
        assert_eq!(loaded.group(PanelScope::Board).tabs(), &[k(101), k(102)]);
    }

    #[test]
    fn sub_tab_selection_round_trips_through_save_and_load() {
        let path = tmp_json_path("sub_tab");
        let mut tabs = DocTabs::default();
        {
            let g = tabs.group_mut(PanelScope::Board);
            g.pin(k(101));
            g.set_sub_state(
                k(101),
                DetailSubState {
                    board_tab: BoardDetailTab::Issue,
                    ..DetailSubState::default()
                },
            );
        }
        tabs.save_to_path(&path).expect("save");
        let loaded = DocTabs::load_from_path(&path);
        let _ = std::fs::remove_file(&path);

        assert_eq!(
            loaded.group(PanelScope::Board).sub_state(&k(101)),
            Some(&DetailSubState {
                board_tab: BoardDetailTab::Issue,
                ..DetailSubState::default()
            }),
        );
    }

    #[test]
    fn known_doc_keys_reads_repo_and_issue_from_open_issues() {
        use crate::app::types::OpenIssue;
        let data = BoardData {
            open_issues: vec![OpenIssue {
                repo_name: "claude-coordinator".to_string(),
                number: 101,
                title: "t".to_string(),
                body: String::new(),
                labels: Vec::new(),
                state: "open".to_string(),
                milestone_number: None,
                milestone_title: None,
            }],
            ..BoardData::default()
        };
        let known = known_doc_keys(&data);
        assert!(known.contains(&k(101)));
        assert_eq!(known.len(), 1);
    }
}
