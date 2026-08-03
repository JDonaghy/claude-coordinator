//! Reusable `MultiSectionView` event-routing helpers (#1741).
//!
//! coord-tui's first `MultiSectionView` consumer is the Reports panel
//! (`app/reports.rs`), but the primitive is the one epic #571's Vocabulary
//! section names for the whole Control Center consolidation — #572 (Backlog
//! tree), #573 (Kanban) and #575 (Conversations) each expect it. So the
//! *routing* (cache-the-layout, hit-test, descend into a section body's own
//! primitive) lives here, report-agnostic, rather than inlined into
//! `reports.rs`: the next consumer inherits it by constructing an
//! [`MsvLayoutCache`] and calling [`MsvLayoutCache::route_click`].
//!
//! # The layout contract
//!
//! `MultiSectionView`'s docs are explicit that paint and hit-test must
//! consume **one** layout produced by **one** set of metrics — re-deriving
//! geometry at click time is exactly the cached-layout-vs-paint drift the
//! #1094 audit-panel work had to fix. `Backend::msv_layout` and
//! `Backend::draw_multi_section_view` both delegate to the rasteriser's own
//! `tui_msv_layout`, so a panel renders by:
//!
//! 1. building the `MultiSectionView`,
//! 2. asking the backend for its layout,
//! 3. drawing with the same view + rect,
//! 4. stashing the layout (plus each form body's own layout) in an
//!    [`MsvLayoutCache`] on the app, in a `RefCell` (render takes `&self`).
//!
//! Mouse handling in `events.rs` — which has no `Backend` handle — then
//! routes against that cache. Same render-then-hit-test discipline as
//! `audit_table_layout` / `kanban_layout`.
#[allow(unused_imports)]
use super::*;

/// The last-painted geometry of a `MultiSectionView`, plus the geometry of
/// each `SectionBody::Form` body inside it.
///
/// `forms[i]` is the `FormLayout` for section `i` when that section's body is
/// a non-collapsed `Form`, and `None` otherwise (collapsed, or a non-form
/// body). **`FormLayout` bounds are form-local** (origin `(0, 0)`), unlike
/// `MultiSectionViewLayout`'s absolute bounds — `route_click` handles the
/// conversion so callers never have to.
#[derive(Default)]
pub(crate) struct MsvLayoutCache {
    /// The view-level layout, in absolute backend coordinates.
    pub(crate) view: Option<MultiSectionViewLayout>,
    /// Per-section form layout, indexed by section. Always the same length as
    /// `view.sections` when `view` is `Some`.
    pub(crate) forms: Vec<Option<FormLayout>>,
}

/// What a click on a `MultiSectionView` resolved to.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum MsvClick {
    /// A header chevron or title area — the host toggles `collapsed` (the
    /// primitive draws the ▾/▸ but does not own the flag, see
    /// `MultiSectionView::allow_collapse`'s doc).
    ToggleSection(usize),
    /// A right-aligned header action button.
    #[allow(dead_code)]
    HeaderAction { section: usize, action: String },
    /// A field inside a section's `Form` body. `field` is the id the form
    /// layout reports, which for a `SegmentedControl` is the synthetic
    /// per-option id `"{field_id}__seg_{idx}"` — split it with
    /// [`split_segment_id`].
    Field { section: usize, field: WidgetId },
    /// Inside the view but not on anything interactive (including a body
    /// that isn't a form, or empty space inside a form).
    Inert,
    /// Outside the view's bounds entirely — the caller should keep looking.
    Outside,
}

impl MsvLayoutCache {
    /// Replace the cached geometry. Called once per frame from the panel's
    /// render fn, immediately after `Backend::msv_layout`.
    pub(crate) fn set(&mut self, view: MultiSectionViewLayout, forms: Vec<Option<FormLayout>>) {
        self.view = Some(view);
        self.forms = forms;
    }

    /// Drop the cached geometry (e.g. when the panel renders a state with no
    /// section stack at all), so a stale click can never hit-test against
    /// geometry that is no longer on screen.
    pub(crate) fn clear(&mut self) {
        self.view = None;
        self.forms.clear();
    }

    /// Route a click at absolute position `pos` against the last-painted
    /// layout. `Outside` when nothing has been cached yet — a stale click
    /// fails closed rather than guessing at geometry.
    pub(crate) fn route_click(&self, pos: Point) -> MsvClick {
        let Some(view) = self.view.as_ref() else {
            return MsvClick::Outside;
        };
        match view.hit_test(pos.x, pos.y) {
            MultiSectionViewHit::Header { section, kind } => match kind {
                HeaderHit::Chevron | HeaderHit::TitleArea => MsvClick::ToggleSection(section),
                HeaderHit::Action(action) => MsvClick::HeaderAction { section, action },
            },
            MultiSectionViewHit::Body { section } => {
                // The body's own primitive owns sub-hit-testing (the
                // `MultiSectionViewHit::Body` doc says so explicitly). For a
                // `Form` body that means `FormLayout::hit_test`, in
                // form-local coordinates.
                let Some(Some(form)) = self.forms.get(section) else {
                    return MsvClick::Inert;
                };
                let Some(body) = view.sections.get(section).map(|s| s.body_bounds) else {
                    return MsvClick::Inert;
                };
                match form.hit_test(pos.x - body.x, pos.y - body.y) {
                    FormHit::Field(field) => MsvClick::Field { section, field },
                    FormHit::Empty => MsvClick::Inert,
                }
            }
            MultiSectionViewHit::Outside => MsvClick::Outside,
            // Aux rows, dividers and scrollbars are unused by the current
            // consumer (no `SectionAux`, `allow_resize: false`,
            // `ScrollMode::WholePanel`) — inert rather than silently
            // swallowed as a section toggle.
            _ => MsvClick::Inert,
        }
    }
}

/// Split a form field id into `(field_id, Some(option_idx))` when it is the
/// synthetic per-option id a `FieldKind::SegmentedControl` reports
/// (`"{field_id}__seg_{idx}"`, see `quadraui::tui::form`'s measurer), or
/// `(id, None)` for any other field.
pub(crate) fn split_segment_id(id: &str) -> (&str, Option<usize>) {
    match id.rsplit_once("__seg_") {
        Some((base, idx)) => match idx.parse::<usize>() {
            Ok(n) => (base, Some(n)),
            // A field whose own id happens to end in `__seg_<not-a-number>`
            // is not a segmented option — leave it whole.
            Err(_) => (id, None),
        },
        None => (id, None),
    }
}
