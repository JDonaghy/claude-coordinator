//! Reports ActivityBar panel (#1741) — Phase B of the Reports feature.
//!
//! **#1911 split the panel across sidebar and main.** A stack of
//! collapsible sections, one per entry in the report catalogue (`GET
//! /report`, #1742), renders in the **sidebar** (`render_reports_sidebar`);
//! each section's body is that report's parameter form plus a `Run` button.
//! Running a report renders the resulting `ReportResult` as a `DataTable`,
//! with the notes block beneath it, in the **main panel**
//! (`render_reports_panel`) — nothing else lives there. Before #1911 the
//! whole stack-then-table layout was stacked in the main panel alone,
//! competing with the grid for height; see those two functions' docs for
//! the split's mechanics, and `app/msv.rs` for the `MultiSectionView`
//! render-then-hit-test contract both still follow (the sections just paint
//! into a different `Rect` now — the click routing that consumes the cache
//! moved from `mouse_main_click` to `mouse_sidebar_click` in `events.rs`,
//! but nothing about the cache itself changed).
//!
//! **This module is a pure client over #1742.** It builds its section list
//! and every parameter control from the catalogue's metadata at runtime —
//! nothing about any specific report (not `issue-activity`, not its `since`
//! window, not its columns) appears in Rust. Adding report #2 to
//! `coord/reports.py` must require zero `tui/**` changes; the
//! `reports_two_param_catalogue_renders_both_controls` test in `tests.rs`
//! pins that.
//!
//! Structurally modelled on `audit.rs` — the closest existing panel:
//! server-sourced read-only data, a background fetch gated to only run while
//! this panel is the active view (see `settings_ui.rs`'s poll loop), a simple
//! `usize` selection index clamped on navigation, no `SidebarSystem` tree.
//!
//! **coord-tui's first `MultiSectionView`.** The reusable half of that wiring
//! (cache-the-painted-layout, hit-test, descend into a `Form` body) lives in
//! `app/msv.rs`, deliberately report-agnostic, because epic #571's panel
//! consolidation (#572/#573/#575) expects the same primitive. This module
//! only supplies the sections and interprets the routed clicks.
//!
//! **#1762 — the result table reads `column_meta`.** #1741 shipped with
//! every column flexing equally and every cell dumped as raw JSON, because
//! the wire carried no display metadata: timestamps arrived as
//! `1785780878.1551082` and lists as `["dellserver","precision"]`, and the
//! panel had no basis for treating `title` differently from `issue`. #1760
//! put that metadata on the wire, so cells are now formatted by their
//! declared `kind`, columns sized by their `weight`, and a header click
//! sorts the table.
//!
//! The generic-vs-hardcoded line is unchanged and load-bearing: the
//! formatting switch is on **`kind`**, never on a column id, and every
//! piece of metadata is optional. A daemon that sends no `column_meta`
//! renders exactly as it did before #1762; a daemon that declares a `kind`
//! this binary has never heard of falls back to plain stringification. Add
//! a per-report `match` here and #1741's "adding report #2 requires zero
//! `tui/**` changes" property dies.
//!
//! Sorting is client-side, which is correct *here* and would not have been
//! for Audit: a `ReportResult` is a complete bounded set, where `/audit` is
//! server-paginated newest-first and a client-side sort would reorder only
//! the loaded page while presenting itself as the answer (see `audit.rs`,
//! which defers column sort for exactly that reason).
//!
//! **#1853 — the result table's columns are drag-resizable.** `weight` is a
//! good default and a bad *fixed* choice: `drive-queue-status` puts a wide
//! TITLE and a wide LAST REASON on the same row, and which one the operator
//! needs to read depends on whether they are chasing a stall or scanning the
//! queue. Dragging a header divider now moves width between that divider's
//! two columns, for every report in the catalogue at once — the panel builds
//! one table, so nothing about this is per-report.
//!
//! Two things make it more than Audit's `audit_column_overrides` copied
//! across:
//!
//! 1. **The column set is dynamic.** Overrides are stored keyed by
//!    `(report_id, column ids)` (`ReportsColumnKey`) and read back through
//!    `reports_active_overrides`, which yields nothing when the key doesn't
//!    match the result on screen. A width dragged in one report can therefore
//!    never re-apply to another report's identically-positioned column.
//! 2. **`h_scroll` stays pinned at `0.0`.** The drag goes through
//!    `DataTableLayout::drag_divider` (quadraui), which holds the dragged
//!    *pair's* combined width constant and freezes every other column at its
//!    resolved width — so a resize cannot make the content wider than it
//!    already was, and cannot newly push the table into horizontal scrolling.
//!    That matters because `DataTableLayout::hit_test` has no concept of
//!    `h_scroll` while the renderer *does* subtract it, so a non-zero
//!    `h_scroll` would shift the painted headers out from under the hit-test
//!    and route #1762's sort clicks to the wrong column. See the comment on
//!    the `h_scroll` field in `render_reports_result`.
//!
//! Session-only, like the sort and like Audit's widths: nothing is written to
//! settings.
//!
//! **#1910 — the result table's vertical scroll was wired but not working.**
//! The wheel arm (`events.rs`, #1741) and `show_scrollbar: true` both
//! predated this fix; two independent gaps made them ineffective anyway.
//! The wheel clamped against `content_visible_rows(main_b, lh)` — the whole
//! main panel's row count — instead of the result table's own (smaller)
//! viewport, since the section stack sits above it and the notes block can
//! take a slice below; `reports_table_visible_rows` now reads the real
//! count off the painted `DataTableLayout`. And the scrollbar thumb was
//! paint-only: `DataTableLayout::hit_test` has no concept of the strip it
//! reserves for it (the same gap #1094 found and fixed for Audit's table),
//! so a click/drag there fell through to a row hit; `reports_scrollbar_hit`
//! / `reports_apply_vscroll` close it the same way Audit's
//! `audit_scrollbar_hit` / `audit_apply_vscroll` do. The scroll offset
//! itself was never the problem — `reports_result_scroll` only resets on an
//! explicit sort click or a new run, both intentional, never on an ordinary
//! repaint.
//!
//! No sealed acceptance contract exists for this panel (there is no
//! `tests/acceptance/**` slice for #1741) — the in-crate `TuiDriver` tests in
//! `app/tests.rs` are the gate.
#[allow(unused_imports)]
use super::*;

impl CoordApp {
    // ── Catalogue / parameter accessors ──────────────────────────────────

    /// The fetched report catalogue, or an empty slice before the first
    /// fetch completes. Unlike the audit list, "not fetched yet" and
    /// "fetched, zero reports" are *not* rendered identically — see
    /// `render_reports_panel`, which distinguishes them so a daemon that
    /// predates #1742 (405/404 on `/report`) is diagnosable from the UI.
    pub(crate) fn reports_catalogue(&self) -> &[ReportDef] {
        self.reports_catalogue.as_deref().unwrap_or(&[])
    }

    /// Selected section index, clamped against the current catalogue length.
    pub(crate) fn reports_selected_idx(&self) -> usize {
        let n = self.reports_catalogue().len();
        if n == 0 {
            0
        } else {
            self.reports_sel.min(n - 1)
        }
    }

    /// The currently-selected catalogue entry, or `None` on an empty
    /// catalogue.
    pub(crate) fn reports_selected(&self) -> Option<&ReportDef> {
        self.reports_catalogue().get(self.reports_selected_idx())
    }

    /// Current value of one parameter: the operator's edit if there is one,
    /// otherwise the catalogue's `default`. Never a hardcoded fallback —
    /// the catalogue is the only source of defaults.
    pub(crate) fn reports_param_value(&self, report_id: &str, param: &ReportParamDef) -> String {
        self.reports_params
            .get(report_id)
            .and_then(|m| m.get(&param.id))
            .cloned()
            .unwrap_or_else(|| param.default.clone())
    }

    /// Record an operator edit to one parameter.
    pub(crate) fn reports_set_param(&mut self, report_id: &str, param_id: &str, value: String) {
        self.reports_params
            .entry(report_id.to_string())
            .or_default()
            .insert(param_id.to_string(), value);
    }

    /// Whether a section is currently expanded. Sections start **expanded**
    /// (the catalogue is short and the whole point is the parameter form) —
    /// `reports_expanded` therefore records the ones the operator has
    /// explicitly *collapsed* plus the ones re-expanded, seeded on first
    /// render by `reports_seed_expansion`.
    pub(crate) fn reports_is_expanded(&self, report_id: &str) -> bool {
        self.reports_expanded.contains(report_id)
    }

    /// Expand every catalogue entry that the operator has never touched.
    /// Called when a catalogue lands, so the first frame shows open forms
    /// rather than a stack of collapsed titles.
    pub(crate) fn reports_seed_expansion(&mut self) {
        let ids: Vec<String> = self
            .reports_catalogue()
            .iter()
            .map(|d| d.id.clone())
            .collect();
        for id in ids {
            if !self.reports_touched.contains(&id) {
                self.reports_expanded.insert(id);
            }
        }
    }

    /// Move the selected section by `delta` (`j`/`Down` = `1`, `k`/`Up` =
    /// `-1`), clamped to the catalogue's bounds, and reset focus to the
    /// section's first field. A no-op on an empty catalogue.
    ///
    /// Pulled out of the `j`/`k` key-handling match arms in `events.rs` so
    /// a test can drive this exact behaviour directly: `reports_view`'s
    /// `active_section` has no textual (or even colour) rendering in the
    /// TUI rasteriser today — `paint_header` never reads it — so a
    /// `TuiDriver`-based test has nothing on screen to assert against for
    /// "which section did j/k select". Same trade `reports_step_choice`
    /// already makes for `SegmentedControl`'s colour-only selection state.
    pub(crate) fn reports_move_selection(&mut self, delta: isize) {
        let n = self.reports_catalogue().len();
        if n == 0 {
            return;
        }
        let next = (self.reports_sel as isize + delta).clamp(0, n as isize - 1);
        self.reports_sel = next as usize;
        self.reports_field_sel = 0;
        self.reports_clamp_field_sel();
    }

    /// Toggle one section's collapsed state (header click, or `Space`).
    pub(crate) fn reports_toggle_expanded(&mut self, report_id: &str) {
        self.reports_touched.insert(report_id.to_string());
        if self.reports_expanded.contains(report_id) {
            self.reports_expanded.remove(report_id);
        } else {
            self.reports_expanded.insert(report_id.to_string());
        }
    }

    // ── Field ids ────────────────────────────────────────────────────────
    //
    // One namespaced id scheme shared by form construction and click
    // routing, so the two can't drift: `report:<report-id>:param:<param-id>`
    // and `report:<report-id>:run`. Report/param ids come from the daemon
    // and are slug-shaped (`issue-activity`, `since`); the parser below
    // splits on the fixed prefix rather than on every `:` so an exotic id
    // degrades to "no match" instead of mis-routing.

    pub(crate) fn reports_param_field_id(report_id: &str, param_id: &str) -> WidgetId {
        WidgetId::new(format!("report:{report_id}:param:{param_id}"))
    }

    pub(crate) fn reports_run_field_id(report_id: &str) -> WidgetId {
        WidgetId::new(format!("report:{report_id}:run"))
    }

    /// Inverse of the two constructors above: `("issue-activity",
    /// Some("since"))` for a param field, `("issue-activity", None)` for a
    /// Run button, `None` for anything else.
    pub(crate) fn reports_parse_field_id(id: &str) -> Option<(String, Option<String>)> {
        let rest = id.strip_prefix("report:")?;
        if let Some((report_id, param_id)) = rest.split_once(":param:") {
            return Some((report_id.to_string(), Some(param_id.to_string())));
        }
        let report_id = rest.strip_suffix(":run")?;
        Some((report_id.to_string(), None))
    }

    // ── Form construction (catalogue-driven) ─────────────────────────────

    /// Build one report's parameter form straight from its catalogue
    /// metadata:
    ///
    /// - a `choice` param with ≤ 5 choices → `FieldKind::SegmentedControl`
    ///   (the time-range preset row),
    /// - a `choice` param with more → `FieldKind::Dropdown`,
    /// - anything else (`text`) → `FieldKind::TextInput`,
    /// - plus a trailing `FieldKind::Button` labelled `Run`.
    ///
    /// The field order here **is** the keyboard-focus order (`Tab`), and
    /// `reports_field_sel` indexes into it, so the Run button is always at
    /// index `params.len()`.
    pub(crate) fn reports_param_form(&self, def: &ReportDef, focused: Option<usize>) -> Form {
        let mut fields: Vec<FormField> = Vec::with_capacity(def.params.len() + 1);
        for param in &def.params {
            let value = self.reports_param_value(&def.id, param);
            let id = Self::reports_param_field_id(&def.id, &param.id);
            let kind = if param.kind == "choice" && !param.choices.is_empty() {
                let selected_idx = param.choices.iter().position(|c| *c == value).unwrap_or(0);
                if param.choices.len() <= Self::REPORTS_SEGMENTED_MAX {
                    FieldKind::SegmentedControl {
                        options: param.choices.clone(),
                        selected_idx,
                    }
                } else {
                    FieldKind::Dropdown {
                        options: param
                            .choices
                            .iter()
                            .map(|c| StyledText::plain(c.as_str()))
                            .collect(),
                        selected_idx,
                    }
                }
            } else {
                FieldKind::TextInput {
                    value: value.clone(),
                    placeholder: param.help.clone(),
                    cursor: None,
                    selection_anchor: None,
                }
            };
            let label = if param.label.is_empty() {
                param.id.clone()
            } else {
                param.label.clone()
            };
            fields.push(FormField {
                id,
                label: StyledText::plain(label.as_str()),
                kind,
                hint: StyledText::plain(""),
                disabled: false,
                validation: None,
            });
        }
        // The Run button is a form field, not a header action, so it lands
        // inside the collapsible body: collapsing a section must hide the
        // ability to fire it, not leave a dangling trigger in the header.
        fields.push(FormField {
            id: Self::reports_run_field_id(&def.id),
            label: StyledText::plain("Run"),
            kind: FieldKind::Button,
            hint: StyledText::plain(""),
            disabled: self.reports_running.is_some(),
            validation: None,
        });
        let focused_field = focused.and_then(|i| fields.get(i)).map(|f| f.id.clone());
        Form {
            id: WidgetId::new(format!("reports-form-{}", def.id)),
            fields,
            focused_field,
            scroll_offset: 0,
            has_focus: focused.is_some(),
        }
    }

    /// Choice params with at most this many options render as a segmented
    /// control (every option visible and one click away); more than this and
    /// the row would not fit, so they fall back to a dropdown.
    const REPORTS_SEGMENTED_MAX: usize = 5;

    /// Number of form fields a report has (params + the Run button) — the
    /// modulus for `Tab` focus cycling and the body height of an expanded
    /// section.
    pub(crate) fn reports_field_count(def: &ReportDef) -> usize {
        def.params.len() + 1
    }

    /// The section-header badge: run state for this report, or nothing.
    fn reports_badge(&self, def: &ReportDef) -> Option<StyledText> {
        if self.reports_running.as_deref() == Some(def.id.as_str()) {
            return Some(StyledText {
                spans: vec![StyledSpan::with_fg(" running…", Color::rgb(230, 200, 120))],
            });
        }
        let result = self.reports_result.as_ref()?;
        if result.report_id != def.id {
            return None;
        }
        let n = result.rows.len();
        Some(StyledText {
            spans: vec![StyledSpan::with_fg(
                format!(" {n} row{}", if n == 1 { "" } else { "s" }),
                Color::rgb(140, 200, 140),
            )],
        })
    }

    // ── #1765: the Export header action ──────────────────────────────────

    /// The `HeaderAction::id` of the per-section Export button. Routed by
    /// id, never by position, so adding a second action later can't silently
    /// re-point this one.
    pub(crate) const REPORTS_EXPORT_ACTION: &'static str = "export";

    /// Whether `def`'s section currently has a result to export.
    ///
    /// The panel holds **one** result at a time (a run clears the previous
    /// one), so this is "the last run was this report and it succeeded" —
    /// which is exactly the condition under which exporting means anything.
    pub(crate) fn reports_has_result(&self, report_id: &str) -> bool {
        self.reports_result
            .as_ref()
            .is_some_and(|r| r.report_id == report_id)
    }

    /// The section header's Export action, enabled only when that report has
    /// a result.
    ///
    /// Disabled rather than absent: quadraui renders a disabled action
    /// dimmed and reserves no hit region for it (clicks fall through to the
    /// title area), so an unrun report shows a visibly inert button instead
    /// of either a confusing no-op or an affordance that pops into existence
    /// the first time you run something.
    fn reports_export_action(&self, def: &ReportDef) -> HeaderAction {
        HeaderAction {
            id: Self::REPORTS_EXPORT_ACTION.to_string(),
            // A one-character fallback on purpose: quadraui's TUI header
            // draws `icon.fallback` at its full width but hit-tests a fixed
            // 2-cell region per action, so a wider glyph would paint outside
            // the region that actually accepts the click.
            icon: Icon::new("\u{f019}", "⤓"),
            tooltip: Some("Export this report as CSV".to_string()),
            enabled: self.reports_has_result(&def.id) && self.reports_running.is_none(),
        }
    }

    /// The section stack: one `Section` per catalogue entry, titled from the
    /// catalogue.
    pub(crate) fn reports_view(&self) -> MultiSectionView {
        let sel = self.reports_selected_idx();
        let sections = self
            .reports_catalogue()
            .iter()
            .enumerate()
            .map(|(i, def)| {
                let expanded = self.reports_is_expanded(&def.id);
                let focused = if i == sel && expanded {
                    Some(
                        self.reports_field_sel
                            .min(Self::reports_field_count(def) - 1),
                    )
                } else {
                    None
                };
                Section {
                    id: def.id.clone(),
                    header: SectionHeader {
                        icon: None,
                        title: StyledText::plain(if def.title.is_empty() {
                            def.id.as_str()
                        } else {
                            def.title.as_str()
                        }),
                        badge: self.reports_badge(def),
                        actions: vec![self.reports_export_action(def)],
                        show_chevron: true,
                    },
                    body: SectionBody::Form(self.reports_param_form(def, focused)),
                    aux: None,
                    size: SectionSize::Content,
                    collapsed: !expanded,
                    min_size: None,
                    max_size: None,
                }
            })
            .collect();
        MultiSectionView {
            id: WidgetId::new("reports-sections"),
            sections,
            active_section: Some(sel),
            axis: MsvAxis::Vertical,
            allow_resize: false,
            allow_collapse: true,
            // WholePanel sizes every section to exactly header + body content
            // — deterministic, unlike PerSection's remainder distribution,
            // which matters because this panel hands the *unused* remainder
            // of the main pane to the result table below the stack.
            scroll_mode: ScrollMode::WholePanel,
            has_focus: true,
            panel_scroll: 0.0,
        }
    }

    // ── Sidebar ──────────────────────────────────────────────────────────

    /// #1911: render the section stack (parameters + Run, one collapsible
    /// section per catalogue entry) into the sidebar — moved here from the
    /// main panel so the main panel is free to give the result grid the
    /// space it actually needs.
    ///
    /// Same render-then-cache discipline as the pre-#1911 main-panel version
    /// (`app/msv.rs`'s module docs): build the view, ask the backend for its
    /// layout, paint from that layout, then stash it in `reports_layout` so
    /// `events.rs`'s click routing (now in `mouse_sidebar_click`) hit-tests
    /// the exact geometry that was painted.
    ///
    /// `Last run: N rows` and `Window: <w>` (pre-#1911 sidebar content) are
    /// gone rather than moved: row count is self-evident from the grid the
    /// main panel now has room to show, and the window is visible in the
    /// parameters that produced it. `Running: <id>` survives as the
    /// per-section badge (`reports_badge`) it already was.
    pub(crate) fn render_reports_sidebar(&self, backend: &mut dyn Backend, rect: Rect) {
        let catalogue = self.reports_catalogue();
        if catalogue.is_empty() {
            // #1911: deliberately terser than `render_reports_panel`'s
            // counterpart of this message — the sidebar's default width (35
            // cols) can't fit an arbitrary daemon-supplied failure reason
            // without ugly mid-word truncation, and the main panel has the
            // room and shows the same states with the full reason attached.
            let message = if self.reports_error.is_some() {
                "  No reports available.  (fetch failed)"
            } else if self.reports_no_service {
                "  No reports available.  (no board service)"
            } else if self.reports_catalogue.is_none() {
                "  Loading reports…"
            } else {
                "  No reports available.  (empty catalogue)"
            };
            self.reports_layout.borrow_mut().clear();
            backend.draw_list(rect, &plain_list("reports-empty", message, 0));
            return;
        }

        let view = self.reports_view();
        let layout = backend.msv_layout(rect, &view);
        backend.draw_multi_section_view(rect, &view);
        let forms: Vec<Option<FormLayout>> = view
            .sections
            .iter()
            .enumerate()
            .map(|(i, section)| {
                let body = layout.sections.get(i)?.body_bounds;
                match (&section.body, section.collapsed) {
                    (SectionBody::Form(form), false) => Some(backend.form_layout(body, form)),
                    _ => None,
                }
            })
            .collect();
        self.reports_layout.borrow_mut().set(layout, forms);
    }

    /// `"<start> → <end>"` for a result's window, or `None` when the daemon
    /// sent something other than the documented `[start, end]` pair.
    fn reports_window_label(result: &ReportResult) -> Option<String> {
        if result.window.len() < 2 {
            return None;
        }
        Some(format!(
            "{} → {}",
            format_unix_time(result.window[0]),
            format_unix_time(result.window[1])
        ))
    }

    // ── Result table ─────────────────────────────────────────────────────

    /// This column's `column_meta` entry (#1760), or `None` when the daemon
    /// sent none — looked up **by id**, not by position.
    ///
    /// The wire documents that `column_meta[i]` corresponds to `columns[i]`,
    /// so zipping would work for a well-formed payload. It is looked up by
    /// id anyway for the same reason cells are (`reports_cell_text`): a
    /// daemon that ships a partial, reordered, or over-long `column_meta`
    /// must degrade to "no metadata for this column", never to *another*
    /// column's metadata, which would silently render one column's data
    /// under another column's rules.
    fn reports_column_meta<'a>(
        result: &'a ReportResult,
        column: &str,
    ) -> Option<&'a ReportColumnMeta> {
        result.column_meta.iter().find(|m| m.id == column)
    }

    /// The declared `kind` for a column, or `""` when there is no metadata.
    /// Deliberately a `&str` rather than an enum: `kind` is an **open**
    /// vocabulary, and an unrecognised value must fall through to plain
    /// stringification (the older/newer-daemon guard), not fail to parse.
    fn reports_column_kind<'a>(result: &'a ReportResult, column: &str) -> &'a str {
        Self::reports_column_meta(result, column)
            .map(|m| m.kind.as_str())
            .unwrap_or("")
    }

    /// Today's stringification, unchanged — the fallthrough every unknown
    /// `kind` (and every column with no metadata at all) still lands in.
    fn reports_plain_text(value: &serde_json::Value) -> String {
        match value {
            serde_json::Value::Null => String::new(),
            serde_json::Value::String(s) => s.clone(),
            serde_json::Value::Bool(b) => b.to_string(),
            serde_json::Value::Number(n) => n.to_string(),
            other => other.to_string(),
        }
    }

    /// What an empty `list` cell renders as. `[]` reads like a bug; a blank
    /// cell reads like missing data. An em dash says "nothing, deliberately".
    const REPORTS_EMPTY_LIST: &'static str = "—";

    /// One result cell, looked up **by column name** — the engine documents
    /// that `rows` may carry keys beyond `columns`, so positional indexing
    /// would silently mis-align the moment a report adds a detail field.
    ///
    /// #1762: the raw value is rendered through its column's declared
    /// `kind` (#1760) — a `timestamp` as a readable time rather than
    /// `1785780878.1551082`, a `list` as `dellserver, precision` rather
    /// than `["dellserver","precision"]`.
    ///
    /// The switch is on **`kind`, never on column id**. That is the whole
    /// line between this panel staying generic and it growing a per-report
    /// `match`: #1741's "adding report #2 requires zero `tui/**` changes"
    /// property survives only while nothing here knows that `started_at`
    /// exists. An unrecognised `kind` falls back to `reports_plain_text`,
    /// so a newer daemon declaring a kind this binary predates still
    /// renders its data instead of panicking or blanking the column.
    fn reports_cell_text(row: &serde_json::Value, column: &str, kind: &str) -> String {
        let value = match row.get(column) {
            None | Some(serde_json::Value::Null) => return String::new(),
            Some(v) => v,
        };
        match kind {
            // `null` already returned above, so this is a real instant.
            // A non-numeric "timestamp" (a daemon sending ISO strings, say)
            // falls back rather than rendering "1970-01-01".
            "timestamp" => match value.as_f64() {
                Some(ts) => format_unix_smart(ts),
                None => Self::reports_plain_text(value),
            },
            "list" => match value.as_array() {
                Some(items) if items.is_empty() => Self::REPORTS_EMPTY_LIST.to_string(),
                Some(items) => items
                    .iter()
                    .map(Self::reports_plain_text)
                    .collect::<Vec<_>>()
                    .join(", "),
                None => Self::reports_plain_text(value),
            },
            // #1763: a USD figure. Without this a cost column renders the
            // raw JSON number — `31.500000000000004` next to `1.5`, which
            // is unreadable and misaligned. Still keyed on `kind`, not on
            // column id: any report declaring `money` gets it.
            "money" => match value.as_f64() {
                Some(usd) => format_money(usd),
                None => Self::reports_plain_text(value),
            },
            // #1763: seconds → `1h02m03s`. Declared by no column today, but
            // it is in the #1760 vocabulary and a raw `4523.0` in a column
            // labelled "Time" is worse than useless.
            "duration" => match value.as_f64() {
                Some(secs) => format_duration_compact(secs),
                None => Self::reports_plain_text(value),
            },
            // `int` needs no special text — it is `align: right` that makes
            // a numeric column readable, and that is applied in
            // `reports_result_columns`. `enum`/`text`/unknown: as before.
            _ => Self::reports_plain_text(value),
        }
    }

    /// Clamp on a column's declared `weight`. The floor stops a `weight: 0`
    /// (or a negative, or a NaN) collapsing a column to nothing; the ceiling
    /// stops one runaway weight starving every other column. Both are
    /// generic guards on daemon-supplied numbers, not opinions about any
    /// particular report.
    const REPORTS_MIN_WEIGHT: f32 = 0.5;
    const REPORTS_MAX_WEIGHT: f32 = 8.0;

    fn reports_column_weight(meta: Option<&ReportColumnMeta>) -> f32 {
        match meta.map(|m| m.weight) {
            Some(w) if w.is_finite() && w > 0.0 => {
                w.clamp(Self::REPORTS_MIN_WEIGHT, Self::REPORTS_MAX_WEIGHT)
            }
            // No metadata, or a weight the daemon left at its serde default
            // (0.0) / sent as junk: exactly today's equal-share behaviour.
            _ => 1.0,
        }
    }

    /// Columns for the result `DataTable`, derived entirely from
    /// `ReportResult.columns` plus its optional `column_meta` (#1760).
    ///
    /// Before #1760 the panel had no display metadata at all and every
    /// column flexed equally — not a defect, just the only honest thing to
    /// do with eleven names it could not interpret. The metadata is now on
    /// the wire, so `weight` sizes the column, `align` sets its text
    /// alignment, and `label` titles it. **Every one of those is optional**:
    /// with `column_meta` absent this produces byte-identical `Column`s to
    /// the pre-#1762 code, which is what keeps an older daemon working.
    fn reports_result_columns(result: &ReportResult) -> Vec<Column> {
        result
            .columns
            .iter()
            .map(|c| {
                let meta = Self::reports_column_meta(result, c);
                let title = match meta.map(|m| m.label.as_str()) {
                    Some(label) if !label.is_empty() => label.to_string(),
                    _ => c.clone(),
                };
                Column {
                    title,
                    width: ColumnWidth::Flex(Self::reports_column_weight(meta)),
                    align: match meta.map(|m| m.align.as_str()) {
                        Some("right") => ColumnAlign::Right,
                        Some("center") => ColumnAlign::Center,
                        _ => ColumnAlign::Left,
                    },
                }
            })
            .collect()
    }

    /// Cells per unit of column weight below which the table stops
    /// squeezing and starts scrolling horizontally — the `min_total_width`
    /// floor `audit.rs`'s `AUDIT_TABLE_MIN_WIDTH` (#1094) established for
    /// the Audit table, expressed per-weight because a report's column
    /// count is not known until the result lands.
    ///
    /// Scaling by *total weight* rather than column count is what makes the
    /// floor mean the same thing for every column: at this width the
    /// lightest column (weight 1.0) gets ~9 cells, whatever the heavier
    /// ones are doing.
    const REPORTS_MIN_WIDTH_PER_WEIGHT: f32 = 9.0;

    /// Cap on the derived floor, so a catalogue with an absurd number of
    /// columns produces a wide-but-navigable table rather than a viewport
    /// showing two columns and a very long scrollbar.
    const REPORTS_MAX_MIN_WIDTH: f32 = 240.0;

    /// The `min_total_width` for this result's table, or `None` when there
    /// are no columns to floor.
    ///
    /// `None` (today's value) lets `DataTable` squeeze every column to fit
    /// the viewport, which at 80 columns leaves an eleven-column report a
    /// few cells per column — legible for nothing. Below this floor the
    /// primitive's horizontal scrollbar takes over instead.
    fn reports_table_min_width(result: &ReportResult) -> Option<f32> {
        if result.columns.is_empty() {
            return None;
        }
        let total_weight: f32 = result
            .columns
            .iter()
            .map(|c| Self::reports_column_weight(Self::reports_column_meta(result, c)))
            .sum();
        Some(
            (total_weight * Self::REPORTS_MIN_WIDTH_PER_WEIGHT).min(Self::REPORTS_MAX_MIN_WIDTH),
        )
    }

    /// Row indices in display order under `sort`, or `0..n` when unsorted.
    ///
    /// A `ReportResult` is a **complete bounded set** — unlike `/audit`,
    /// which is server-paginated newest-first and where a client-side sort
    /// would silently reorder only the loaded page and call it the answer.
    /// Sorting here is therefore correct, and is done client-side.
    fn reports_row_order(
        result: &ReportResult,
        sort: Option<(usize, SortDirection)>,
    ) -> Vec<usize> {
        let mut order: Vec<usize> = (0..result.rows.len()).collect();
        let Some((col, dir)) = sort else {
            return order;
        };
        let Some(name) = result.columns.get(col) else {
            // A sort pinned to a column the current result doesn't have
            // (a re-run that changed shape) degrades to unsorted.
            return order;
        };
        let kind = Self::reports_column_kind(result, name);
        // `sort_by` is stable, so rows that tie keep the daemon's own
        // ordering — which for `issue-activity` is most-recently-active
        // first, a meaningful secondary key the panel gets for free.
        order.sort_by(|&a, &b| {
            Self::reports_compare_cells(
                result.rows[a].get(name.as_str()),
                result.rows[b].get(name.as_str()),
                kind,
                dir,
            )
        });
        order
    }

    /// Order two cells of the same column.
    ///
    /// Compares the **raw JSON**, never the formatted string: `13h ago`
    /// versus `2026-07-28 14:03` sorts lexically into nonsense, and
    /// `1785780878.15` versus `900.0` sorts to the wrong answer as text.
    /// Ordering follows the declared `kind` — numeric for `int`/`timestamp`/
    /// `duration`, length-then-text for `list`, string otherwise (including
    /// every unknown kind).
    ///
    /// `null`/missing sorts **last in both directions**: reversing a sort
    /// should surface the other end of the real data, not a block of blanks.
    fn reports_compare_cells(
        a: Option<&serde_json::Value>,
        b: Option<&serde_json::Value>,
        kind: &str,
        dir: SortDirection,
    ) -> std::cmp::Ordering {
        use std::cmp::Ordering;
        let is_null = |v: &Option<&serde_json::Value>| {
            matches!(v, None | Some(serde_json::Value::Null))
        };
        let (a, b) = match (is_null(&a), is_null(&b)) {
            (true, true) => return Ordering::Equal,
            (true, false) => return Ordering::Greater,
            (false, true) => return Ordering::Less,
            // Both non-null, so both unwraps are infallible.
            (false, false) => (a.unwrap(), b.unwrap()),
        };
        let as_text = |v: &serde_json::Value| Self::reports_plain_text(v);
        let joined = |items: &[serde_json::Value]| {
            items
                .iter()
                .map(Self::reports_plain_text)
                .collect::<Vec<_>>()
                .join(", ")
        };
        let base = match kind {
            "int" | "timestamp" | "duration" | "money" => match (a.as_f64(), b.as_f64()) {
                (Some(x), Some(y)) => x.partial_cmp(&y).unwrap_or(Ordering::Equal),
                // A column declared numeric that isn't (mixed types, an
                // ISO string): fall back rather than treating both as 0.
                _ => as_text(a).cmp(&as_text(b)),
            },
            "list" => match (a.as_array(), b.as_array()) {
                // Length first — "which issue needed the most reviews" is
                // the question a list column is actually asked.
                (Some(x), Some(y)) => x.len().cmp(&y.len()).then_with(|| joined(x).cmp(&joined(y))),
                _ => as_text(a).cmp(&as_text(b)),
            },
            _ => as_text(a).cmp(&as_text(b)),
        };
        match dir {
            SortDirection::Ascending => base,
            SortDirection::Descending => base.reverse(),
        }
    }

    fn reports_result_rows(result: &ReportResult, order: &[usize]) -> Vec<DataRow> {
        // Resolve each column's kind once per table rather than once per
        // cell — an 11-column result over a few hundred rows would otherwise
        // do a few thousand redundant linear scans of `column_meta`.
        let kinds: Vec<&str> = result
            .columns
            .iter()
            .map(|c| Self::reports_column_kind(result, c))
            .collect();
        order
            .iter()
            .filter_map(|&i| result.rows.get(i))
            .map(|row| DataRow {
                cells: result
                    .columns
                    .iter()
                    .zip(kinds.iter())
                    .map(|(c, kind)| StyledText {
                        spans: vec![StyledSpan::with_fg(
                            Self::reports_cell_text(row, c, kind),
                            Color::rgb(200, 200, 200),
                        )],
                    })
                    .collect(),
                decoration: Decoration::Normal,
            })
            .collect()
    }

    /// #1763: the pinned grand-total footer row, or `None` when the daemon
    /// sent no `totals` (every report but `usage` today, and every daemon
    /// older than #1763).
    ///
    /// Rendered through exactly the same per-column `kind` dispatch as any
    /// data row — the only addition is a `Σ` marker in the leading column,
    /// which the wire deliberately leaves blank so each client picks its
    /// own. Nothing here knows which columns are identity columns or which
    /// report is being shown.
    fn reports_footer_row(result: &ReportResult) -> Option<DataRow> {
        let totals = result.totals.as_ref()?;
        if !totals.is_object() {
            // A daemon sending a non-object `totals` gets no footer rather
            // than a row of stringified JSON in every cell.
            return None;
        }
        let mut cells: Vec<StyledText> = result
            .columns
            .iter()
            .map(|c| {
                let kind = Self::reports_column_kind(result, c);
                StyledText {
                    spans: vec![StyledSpan::with_fg(
                        Self::reports_cell_text(totals, c, kind),
                        Color::rgb(235, 235, 235),
                    )],
                }
            })
            .collect();
        if let Some(first) = cells.first_mut() {
            if first.spans.iter().all(|s| s.text.trim().is_empty()) {
                *first = StyledText {
                    spans: vec![StyledSpan::with_fg("Σ".to_string(), Color::rgb(235, 235, 235))],
                };
            }
        }
        Some(DataRow { cells, decoration: Decoration::Header })
    }

    // ── Render ───────────────────────────────────────────────────────────

    /// Render the Reports main panel: **only** the last run's result — the
    /// grid, and its notes beneath it.
    ///
    /// #1911: the collapsible section stack (parameters + Run) that used to
    /// sit above this and compete with the grid for height now lives in
    /// `render_reports_sidebar` instead, so this panel gets the whole main
    /// area. There is no "carve the stack off the top" split to do here any
    /// more.
    pub(crate) fn render_reports_panel(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        if self.reports_catalogue().is_empty() {
            // Deliberately NOT the audit panel's "treat unfetched and empty
            // identically" posture: an empty `/report` catalogue means the
            // daemon is older than #1742 (or unreachable), and that has to
            // be diagnosable without attaching a debugger. The full-detail
            // counterpart of `render_reports_sidebar`'s short version of
            // this message — this panel has the width for the daemon's own
            // failure reason.
            let message = if let Some(reason) = &self.reports_error {
                format!("  No reports available.  (catalogue fetch failed: {reason})")
            } else if self.reports_no_service {
                "  No reports available.  (no board service configured)".to_string()
            } else if self.reports_catalogue.is_none() {
                "  Loading reports…".to_string()
            } else {
                "  No reports available.  (the daemon's catalogue is empty)".to_string()
            };
            *self.reports_table_layout.borrow_mut() = None;
            backend.draw_list(rect, &plain_list("reports-empty-main", &message, 0));
            return;
        }
        self.render_reports_result(backend, rect, lh);
    }

    /// The result area: an in-flight line, a failure, an explicit
    /// no-activity body, or the table + notes.
    fn render_reports_result(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        // #1762: drop last frame's table geometry up front. Every path below
        // that actually paints a table re-sets it; every path that doesn't
        // (an error, a run in flight, no result yet, a zero-row result)
        // leaves it `None`, so a header click can never be routed against a
        // table that is no longer on screen. Pre-#1911 this lived in the
        // caller (`render_reports_panel`), which had other reasons to run
        // unconditionally before the empty-catalogue early return; now that
        // this function IS the whole main panel, the clear belongs here.
        *self.reports_table_layout.borrow_mut() = None;

        // A failed run wins over any previously-completed result: leaving
        // the old table on screen under a fresh failure is the exact
        // "stale result" the acceptance criteria rule out.
        if let Some(reason) = &self.reports_error {
            backend.draw_list(
                rect,
                &plain_list("reports-error", &format!("  Report failed: {reason}"), 0),
            );
            return;
        }
        if let Some(id) = &self.reports_running {
            backend.draw_list(
                rect,
                &plain_list("reports-running", &format!("  Running {id}…"), 0),
            );
            return;
        }
        let Some(result) = self.reports_result.as_ref() else {
            backend.draw_list(
                rect,
                &plain_list(
                    "reports-noresult",
                    "  No report run yet — press Enter to run the selected one.",
                    0,
                ),
            );
            return;
        };

        // Notes get the bottom of the area; the table (or the empty-window
        // message) gets the rest.
        //
        // #1765: the export outcome rides in the same block, pinned above
        // the report's own notes. It goes here rather than in a toast
        // because it has to survive being read — a silent write and a
        // silent failure look identical, and the destination path is the
        // whole answer.
        let note_lines: Vec<&str> = self
            .reports_export_status
            .as_deref()
            .into_iter()
            .chain(result.notes.iter().map(|s| s.as_str()))
            .collect();
        let notes_rows = if note_lines.is_empty() {
            0
        } else {
            (note_lines.len() + 2).min(Self::REPORTS_NOTES_MAX_ROWS)
        };
        let notes_h = (notes_rows as f32 * lh).min(rect.height * 0.5);
        let table_h = (rect.height - notes_h).max(0.0);
        let table_rect = Rect::new(rect.x, rect.y, rect.width, table_h);

        if result.rows.is_empty() {
            // An empty table renders as a bare header row, which reads like
            // a broken fetch. Say what actually happened instead.
            backend.draw_list(
                table_rect,
                &plain_list(
                    "reports-no-activity",
                    &match Self::reports_window_label(result) {
                        Some(window) => format!("  No activity in this window ({window})."),
                        None => "  No activity in this window.".to_string(),
                    },
                    0,
                ),
            );
        } else if table_h > 0.0 {
            // A sort left over from a previous run whose column no longer
            // exists is dropped rather than carried: `reports_sort` is
            // cleared on every run (`reports_start_run`), so this only
            // catches a result swapped in by some other path.
            let sort = self
                .reports_sort
                .filter(|(col, _)| *col < result.columns.len());
            let table = DataTable {
                id: WidgetId::new("reports-result"),
                columns: Self::reports_result_columns(result),
                rows: Self::reports_result_rows(result, &Self::reports_row_order(result, sort)),
                selected_idx: None,
                scroll_offset: self.reports_result_scroll,
                // The ▲/▼ header indicator is drawn by the primitive
                // itself — the app only says which column and which way.
                sort,
                has_focus: false,
                show_scrollbar: true,
                min_total_width: Self::reports_table_min_width(result),
                // Horizontal scrolling is *reachable* (the floor above can
                // make the content wider than the viewport, and the
                // primitive then paints its h-scrollbar) but not yet
                // *drivable* — no drag/wheel handler moves this. Held at
                // 0.0 deliberately: `DataTableLayout::hit_test` has no
                // concept of `h_scroll`, so a non-zero value here would
                // shift the painted headers out from under the hit-test
                // and route column-sort clicks to the wrong column.
                //
                // #1853 confirmed that reading is still correct, and that
                // Audit (which *does* drive `audit_h_scroll`) has the same
                // latent mis-routing — unnoticed only because a stray
                // header click there does nothing. Filed separately; not
                // touched here. Column resize does not reopen the question:
                // `drag_divider` moves width *between* a pair and freezes
                // the rest, so total content width is unchanged by a drag
                // and a resize can never newly force horizontal scrolling.
                h_scroll: 0.0,
                // #1853: user-dragged widths for *this* column set, or
                // empty when the last drag belonged to a different report
                // (or a differently-shaped result from the same one).
                column_overrides: self.reports_active_overrides(result),
                // #1763: pinned Σ row (quadraui#432) when the report
                // supplies one — `usage` does, `issue-activity` does not.
                footer: Self::reports_footer_row(result),
            };
            // Cache the painted geometry *with the rect it was painted
            // into* — unlike Audit's table this one does not start at the
            // main panel's origin (the section stack is above it), so a
            // bare `pos - main_b` would mis-hit-test by the stack's height.
            let layout = backend.draw_data_table(table_rect, &table, None);
            *self.reports_table_layout.borrow_mut() = Some((table_rect, layout));
        }

        if notes_h > 0.0 {
            let items: Vec<ListItem> = note_lines
                .iter()
                .map(|n| activity_item(&format!(" {n}"), Color::rgb(210, 200, 160)))
                .collect();
            backend.draw_list(
                Rect::new(rect.x, rect.y + table_h, rect.width, notes_h),
                &ListView {
                    id: WidgetId::new("reports-notes"),
                    title: Some(StyledText::plain(" Notes ")),
                    items,
                    selected_idx: 0,
                    scroll_offset: 0,
                    has_focus: false,
                    bordered: true,
                    h_scroll: 0,
                    max_content_width: None,
                    show_v_scrollbar: true,
                },
            );
        }
    }

    /// Cap on the notes block's height so a chatty report can't push the
    /// table off screen.
    const REPORTS_NOTES_MAX_ROWS: usize = 10;

    // ── Result-table hit-testing and sort ────────────────────────────────

    /// Hit-test a click against the last-painted result `DataTable`, or
    /// `None` when no table is on screen. Same render-then-hit-test pattern
    /// as `audit_table_hit`, except the cached rect carries the table's own
    /// origin (see `render_reports_result`).
    pub(crate) fn reports_table_hit(&self, pos: Point) -> Option<DataTableHit> {
        let n = self.reports_result.as_ref().map(|r| r.rows.len())?;
        let cache = self.reports_table_layout.borrow();
        let (rect, layout) = cache.as_ref()?;
        Some(layout.hit_test(
            pos.x - rect.x,
            pos.y - rect.y,
            self.reports_result_scroll,
            n,
        ))
    }

    /// #1910: number of rows actually visible inside the result table's own
    /// viewport, straight from the last-painted `DataTableLayout`.
    ///
    /// This is deliberately *not* the whole main panel's
    /// `content_visible_rows` (what the wheel handler used before this fix)
    /// — the section stack sits above this table and the notes block can
    /// take a slice below it, so the panel's row count overcounts what the
    /// table itself shows. Using it as `visible` made the wheel-down clamp
    /// (`n.saturating_sub(visible)`) too small — sometimes `0` — so the
    /// table refused to scroll down while scrolling up still appeared to
    /// work (the reported "scrollbar is completely broken" symptom, #1910).
    ///
    /// `None` before anything has been painted (no run started yet).
    pub(crate) fn reports_table_visible_rows(&self) -> Option<usize> {
        self.reports_table_layout
            .borrow()
            .as_ref()
            .map(|(_, layout)| layout.visible_rows)
    }

    /// #1910: hit-test a click/drag position against the result table's
    /// vertical scrollbar track, using the same painted geometry
    /// `reports_table_hit` reads. Mirrors `audit_scrollbar_hit`, minus the
    /// horizontal half — the table's `h_scroll` is pinned at `0.0` (see
    /// `render_reports_result`), so there is never a horizontal scrollbar
    /// track on screen to hit.
    ///
    /// `DataTableLayout::hit_test` has no concept of the scrollbar strip it
    /// reserves space for — same gap #1094 found and fixed for Audit — so a
    /// click there must be caught here, before `reports_table_hit`, or it
    /// mis-resolves to a row/header hit under the thumb.
    pub(crate) fn reports_scrollbar_hit(&self, pos: Point) -> bool {
        let cache = self.reports_table_layout.borrow();
        let Some((rect, layout)) = cache.as_ref() else {
            return false;
        };
        let x = pos.x - rect.x;
        let y = pos.y - rect.y;
        if x < 0.0 || y < 0.0 || x >= layout.viewport_width || y >= layout.viewport_height {
            return false;
        }
        layout.scrollbar_width > 0.0
            && x >= layout.viewport_width - layout.scrollbar_width
            && y >= layout.header_height
    }

    /// #1910: jump `reports_result_scroll` to the row implied by a
    /// click/drag position along the vertical scrollbar's track — same
    /// click/drag-to-position behaviour as `audit_apply_vscroll`. No-op
    /// (returns `false`) when there's nothing to scroll (no result, empty
    /// result, or the cached layout is stale/missing).
    pub(crate) fn reports_apply_vscroll(&mut self, pos: Point) -> bool {
        let n = match self.reports_result.as_ref() {
            Some(r) if !r.rows.is_empty() => r.rows.len(),
            _ => return false,
        };
        let (track_y0, track_h, visible_rows) = {
            let cache = self.reports_table_layout.borrow();
            let Some((rect, layout)) = cache.as_ref() else {
                return false;
            };
            let track_y0 = rect.y + layout.header_height;
            let track_h = (layout.viewport_height
                - layout.header_height
                - layout.h_scrollbar_height)
                .max(1.0);
            (track_y0, track_h, layout.visible_rows.max(1))
        };
        let max_scroll = n.saturating_sub(visible_rows);
        self.reports_result_scroll = if max_scroll == 0 {
            0
        } else {
            let frac = ((pos.y - track_y0) / track_h).clamp(0.0, 1.0);
            (frac * max_scroll as f32).round() as usize
        };
        true
    }

    /// Click a result-table column header: `None → ▲ → ▼ → None` for that
    /// column, switching straight to ▲ when a different column is clicked.
    ///
    /// The third click clearing the sort (rather than cycling back to ▲) is
    /// what makes the daemon's own row order reachable again — for
    /// `issue-activity` that order is "most recently active first", which
    /// is a genuinely different answer from any column sort.
    pub(crate) fn reports_sort_by_column(&mut self, col: usize) -> bool {
        let columns = self
            .reports_result
            .as_ref()
            .map(|r| r.columns.len())
            .unwrap_or(0);
        if col >= columns {
            return false;
        }
        self.reports_sort = match self.reports_sort {
            Some((c, SortDirection::Ascending)) if c == col => {
                Some((col, SortDirection::Descending))
            }
            Some((c, SortDirection::Descending)) if c == col => None,
            _ => Some((col, SortDirection::Ascending)),
        };
        // The row that was under the viewport means something different
        // now, so "stay where you were" is meaningless — go to the top,
        // which is where the answer to "sort by this" actually is.
        self.reports_result_scroll = 0;
        true
    }

    // ── #1853: result-table column resize ────────────────────────────────

    /// The identity of `result`'s column set — see [`ReportsColumnKey`].
    fn reports_columns_key(result: &ReportResult) -> ReportsColumnKey {
        (result.report_id.clone(), result.columns.clone())
    }

    /// The width overrides that legitimately apply to `result`, or an empty
    /// vec meaning "none — lay this table out at its declared `weight`s".
    ///
    /// This is the whole invalidation mechanism, and it is a *read-side*
    /// check on purpose. Clearing the stored overrides at each site that
    /// can swap the result (a run completing, a re-run changing shape, a
    /// future path nobody has written yet) would be a list that has to stay
    /// exhaustive forever; comparing the key at the point of use cannot be
    /// forgotten, because the only way to get overrides out is through
    /// here. A stale override is therefore not "cleared late" — it is never
    /// reachable at all.
    ///
    /// The length guard is belt-and-braces for a malformed key, not the
    /// invalidation itself: two different reports can have the same column
    /// count, so length alone would happily hand `usage`'s dragged widths
    /// to `drive-queue-status`.
    fn reports_active_overrides(&self, result: &ReportResult) -> Vec<Option<f32>> {
        match self.reports_column_overrides.as_ref() {
            Some((key, widths))
                if *key == Self::reports_columns_key(result)
                    && widths.len() == result.columns.len() =>
            {
                widths.clone()
            }
            _ => Vec::new(),
        }
    }

    /// Minimum width (cells) either half of a dragged divider pair may be
    /// squeezed to — same floor as `AUDIT_MIN_COLUMN_WIDTH`, and enforced by
    /// `drag_divider` on *both* sides so a drag can neither collapse the
    /// column being widened's neighbour nor the column itself.
    const REPORTS_MIN_COLUMN_WIDTH: f32 = 4.0;

    /// Continue an in-progress result-table column-resize drag, started by a
    /// `MouseDown` on a `DataTableHit::HeaderDivider` (`reports_resize_col`
    /// set by `mouse_main_click` in `events.rs`) and released on `MouseUp`.
    ///
    /// `pos` is in absolute backend coordinates and is made table-local
    /// against the *cached rect*, not `main_b` — the same origin
    /// `reports_table_hit` uses. Unlike Audit's table this one does not
    /// start at the main panel's origin (the section stack sits above it),
    /// so `audit_update_resize_drag`'s `main_b` signature is not
    /// transferable; taking the origin from the same cache that produced
    /// the hit is what keeps the dragged divider under the cursor.
    ///
    /// The arithmetic is quadraui's `DataTableLayout::drag_divider` rather
    /// than a local `pointer_x - column.x`: it moves width strictly between
    /// the divider's two columns with their combined width held constant,
    /// and freezes every other column at its currently-resolved width so an
    /// untouched `Flex` column can't be reshuffled by pass 2's
    /// redistribution (quadraui#521). Two consequences matter here — an
    /// unrelated column never moves under the user mid-drag, and the table's
    /// total content width is invariant under a resize, which is why this
    /// feature does not reopen the `h_scroll` question.
    ///
    /// Returns `true` (redraw needed) only while a drag is actually in
    /// progress against a table that is still on screen.
    pub(crate) fn reports_update_resize_drag(&mut self, pos: Point) -> bool {
        let Some(col) = self.reports_resize_col else {
            return false;
        };
        // Key and current widths are captured as owned values so the
        // immutable borrow of `reports_result` ends before the store below.
        let (key, current) = match self.reports_result.as_ref() {
            Some(result) => (
                Self::reports_columns_key(result),
                self.reports_active_overrides(result),
            ),
            None => return false,
        };
        let next = {
            let cache = self.reports_table_layout.borrow();
            let Some((rect, layout)) = cache.as_ref() else {
                return false;
            };
            // A divider only exists between two columns; a `col` past the
            // end means the cached layout is from a differently-shaped
            // result and this drag no longer refers to anything.
            if col + 1 >= layout.columns.len() {
                return false;
            }
            layout.drag_divider(
                &current,
                col,
                pos.x - rect.x,
                Self::REPORTS_MIN_COLUMN_WIDTH,
            )
        };
        // Store only a well-formed override set: `drag_divider` sizes its
        // result from the *layout*, and a layout painted from a stale result
        // could disagree with the key we are about to file it under.
        if next.len() != key.1.len() {
            return false;
        }
        self.reports_column_overrides = Some((key, next));
        true
    }

    // ── Actions ──────────────────────────────────────────────────────────

    /// Fire a run of `report_id` with its current parameter values. The
    /// request is built entirely from the catalogue's param list, so a report
    /// that grows a parameter needs no change here.
    pub(crate) fn reports_start_run(&mut self, report_id: &str) {
        let Some(def) = self
            .reports_catalogue()
            .iter()
            .find(|d| d.id == report_id)
            .cloned()
        else {
            return;
        };
        let params: Vec<(String, String)> = def
            .params
            .iter()
            .map(|p| (p.id.clone(), self.reports_param_value(&def.id, p)))
            .collect();
        // Clear the previous run's output up front: a run in flight must not
        // sit under a stale table, and a previous failure must not survive a
        // retry.
        self.reports_error = None;
        self.reports_result = None;
        self.reports_result_scroll = 0;
        // #1765: the previous run's export outcome describes a file that
        // holds the previous run's rows. Drop it with the result it belongs
        // to rather than letting it read as a report on the new one.
        self.reports_export_status = None;
        // Sort is view state over one specific result set, so it resets
        // with that set — same posture as the panel's parameters and the
        // Audit panel's filters, neither of which persist across restarts.
        // Carrying a sort into a differently-shaped result would silently
        // reorder by whatever column happened to land at that index.
        self.reports_sort = None;
        self.reports_running = Some(def.id.clone());
        self.reports_run_rx = Some(spawn_report_run(&def.id, params));
    }

    /// The `(param_id, value)` pairs to send for `def` — the operator's
    /// edits where they exist, the catalogue's defaults elsewhere. Shared by
    /// the run and the export so the two can never disagree about the
    /// window they are asking for.
    fn reports_param_pairs(&self, def: &ReportDef) -> Vec<(String, String)> {
        def.params
            .iter()
            .map(|p| (p.id.clone(), self.reports_param_value(&def.id, p)))
            .collect()
    }

    // ── #1765: export ────────────────────────────────────────────────────

    /// Note the Export click for `section`, to be picked up once a `Backend`
    /// handle is in scope.
    ///
    /// Mouse routing has no backend, and the save dialog needs one, so the
    /// click parks the request and `dispatch_handle` drains it in the same
    /// turn (see `reports_drain_pending_export`). Returns `false` — leaving
    /// the frame unchanged — for a report with no result, which the disabled
    /// action should already have prevented; this is the belt to that
    /// braces, not a second policy.
    pub(crate) fn reports_request_export(&mut self, section: usize) -> bool {
        let Some(id) = self.reports_catalogue().get(section).map(|d| d.id.clone()) else {
            return false;
        };
        if !self.reports_has_result(&id) || self.reports_running.is_some() {
            return false;
        }
        self.reports_sel = section;
        self.reports_pending_export = Some(id);
        true
    }

    /// `issue-activity-20260804-1130.csv` — the save dialog's suggested name.
    ///
    /// Stamped from the *result's* window end, not the wall clock, so it
    /// matches the `Content-Disposition` filename the daemon offers for the
    /// same run and two exports of the same result don't get two names.
    pub(crate) fn reports_suggested_export_name(result: &ReportResult) -> String {
        let stamp = format_unix_stamp(result.window.get(1).copied().unwrap_or(0.0));
        let id: String = result
            .report_id
            .chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
                    c
                } else {
                    '-'
                }
            })
            .collect();
        format!("{id}-{stamp}.csv")
    }

    /// Resolve what the save dialog said into a destination.
    ///
    /// The TUI branch is the awkward one and is deliberately explicit:
    /// quadraui's TUI `PlatformServices::show_file_save_dialog` is a
    /// documented no-op that always returns `None` ("apps should provide an
    /// in-TUI picker instead"), so treating `None` as "cancelled"
    /// everywhere would make Export permanently dead in `coord-tui` — the
    /// one binary this panel actually ships in. Instead the TUI falls back
    /// to the suggested filename in `$HOME`, and the caller reports the full
    /// path, so the operator always learns where the file went. On a backend
    /// that really can ask (GTK, macOS), `None` means the operator cancelled
    /// and nothing is written.
    pub(crate) fn reports_export_destination(
        chosen: Option<std::path::PathBuf>,
        platform: &str,
        suggested: &str,
    ) -> ReportExportDest {
        if let Some(path) = chosen {
            return ReportExportDest::Path(path);
        }
        if platform == "tui" {
            return ReportExportDest::Path(home_dir().join(suggested));
        }
        ReportExportDest::Cancelled
    }

    /// Ask for a destination and start the export. Called from
    /// `dispatch_handle`, the nearest point to the click that holds a
    /// `Backend`. Returns `true` when it did anything (i.e. a redraw is due).
    pub(crate) fn reports_drain_pending_export(&mut self, backend: &mut dyn Backend) -> bool {
        let Some(report_id) = self.reports_pending_export.take() else {
            return false;
        };
        let suggested = match self
            .reports_result
            .as_ref()
            .filter(|r| r.report_id == report_id)
        {
            Some(result) => Self::reports_suggested_export_name(result),
            None => {
                // The result went away between the click and this drain (a
                // re-run, a failure). Say so rather than exporting whatever
                // is on screen now.
                self.reports_export_status =
                    Some(format!("Export: {report_id} has no result to export."));
                return true;
            }
        };
        let chosen = backend.services().show_file_save_dialog(FileDialogOptions {
            title: Some(format!("Export {report_id} as CSV")),
            initial_dir: None,
            initial_filename: Some(suggested.clone()),
            filters: vec![("CSV".to_string(), vec!["csv".to_string()])],
        });
        let platform = backend.services().platform_name();
        match Self::reports_export_destination(chosen, platform, &suggested) {
            ReportExportDest::Cancelled => {
                // Not an error: nothing was written and nothing went wrong.
                self.reports_export_status = Some("Export cancelled.".to_string());
            }
            ReportExportDest::Path(dest) => self.reports_start_export(&report_id, dest),
        }
        true
    }

    /// Fire the background fetch-and-write for `report_id` → `dest`.
    pub(crate) fn reports_start_export(&mut self, report_id: &str, dest: std::path::PathBuf) {
        let Some(def) = self
            .reports_catalogue()
            .iter()
            .find(|d| d.id == report_id)
            .cloned()
        else {
            return;
        };
        let params = self.reports_param_pairs(&def);
        self.reports_export_status = Some(format!("Exporting → {}…", dest.display()));
        self.reports_export_rx = Some(spawn_report_export(&def.id, params, dest));
    }

    /// Re-run the selected report (`r`, and the initial `Enter`).
    pub(crate) fn reports_rerun_selected(&mut self) {
        let Some(id) = self.reports_selected().map(|d| d.id.clone()) else {
            return;
        };
        self.reports_start_run(&id);
    }

    /// Force the next `run_periodic_work` tick to re-fetch the catalogue,
    /// dropping any in-flight request. Mirrors `refresh_audit`.
    pub(crate) fn reports_refresh_catalogue(&mut self) {
        self.reports_catalogue_rx = None;
        self.reports_catalogue_fetched = false;
    }

    /// Clamp `reports_field_sel` into the selected section's field list —
    /// called after any move of `reports_sel`, since reports have different
    /// parameter counts.
    pub(crate) fn reports_clamp_field_sel(&mut self) {
        let max = self
            .reports_selected()
            .map(|d| Self::reports_field_count(d).saturating_sub(1))
            .unwrap_or(0);
        self.reports_field_sel = self.reports_field_sel.min(max);
        self.reports_text_editing = false;
    }

    /// The kind of the currently-focused field in the selected section —
    /// what `Tab`/`←`/`→`/`Enter`/typing all branch on. `None` when the Run
    /// button is focused (or there is no selection).
    pub(crate) fn reports_focused_param(&self) -> Option<(String, ReportParamDef)> {
        let def = self.reports_selected()?;
        let param = def.params.get(self.reports_field_sel)?;
        Some((def.id.clone(), param.clone()))
    }

    /// Step a focused `choice` param to the next/previous option (`→`/`←`).
    /// A no-op for text params and for the Run button.
    pub(crate) fn reports_step_choice(&mut self, forward: bool) -> bool {
        let Some((report_id, param)) = self.reports_focused_param() else {
            return false;
        };
        if param.kind != "choice" || param.choices.is_empty() {
            return false;
        }
        let current = self.reports_param_value(&report_id, &param);
        let idx = param
            .choices
            .iter()
            .position(|c| *c == current)
            .unwrap_or(0);
        let n = param.choices.len();
        let next = if forward {
            (idx + 1) % n
        } else {
            (idx + n - 1) % n
        };
        let value = param.choices[next].clone();
        self.reports_set_param(&report_id, &param.id, value);
        true
    }

    /// Apply a click that landed on a form field, having already been routed
    /// through `MsvLayoutCache`. Returns `true` when something changed.
    ///
    /// Note the segmented-control case: the form layout reports a synthetic
    /// per-option id, which is what makes "click the option you want"
    /// work without the panel knowing what the options mean.
    pub(crate) fn reports_apply_field_click(&mut self, section: usize, field: &WidgetId) -> bool {
        let (base, seg_idx) = split_segment_id(field.as_str());
        let Some((report_id, param_id)) = Self::reports_parse_field_id(base) else {
            return false;
        };
        // Keep keyboard focus where the mouse just clicked (same
        // click-syncs-focus behaviour the Settings panel has).
        self.reports_sel = section;
        let Some(def) = self
            .reports_catalogue()
            .iter()
            .find(|d| d.id == report_id)
            .cloned()
        else {
            return false;
        };
        let Some(param_id) = param_id else {
            // The Run button.
            self.reports_field_sel = def.params.len();
            self.reports_text_editing = false;
            if self.reports_running.is_none() {
                self.reports_start_run(&report_id);
            }
            return true;
        };
        let Some(idx) = def.params.iter().position(|p| p.id == param_id) else {
            return false;
        };
        self.reports_field_sel = idx;
        let param = &def.params[idx];
        match seg_idx.and_then(|i| param.choices.get(i)) {
            Some(choice) => {
                let choice = choice.clone();
                self.reports_text_editing = false;
                self.reports_set_param(&report_id, &param_id, choice);
            }
            None if param.kind == "choice" && !param.choices.is_empty() => {
                // A dropdown (> REPORTS_SEGMENTED_MAX options) has no
                // per-option hit region in the TUI rasteriser — clicking it
                // advances to the next option, same as `→`.
                self.reports_text_editing = false;
                self.reports_step_choice(true);
            }
            None => {
                // A text param: clicking focuses it for editing.
                self.reports_text_editing = true;
            }
        }
        true
    }

    /// Insert a typed character into the focused text param.
    pub(crate) fn reports_text_insert(&mut self, ch: char) -> bool {
        let Some((report_id, param)) = self.reports_focused_param() else {
            return false;
        };
        let mut value = self.reports_param_value(&report_id, &param);
        value.push(ch);
        self.reports_set_param(&report_id, &param.id, value);
        true
    }

    /// Delete the last character of the focused text param.
    pub(crate) fn reports_text_backspace(&mut self) -> bool {
        let Some((report_id, param)) = self.reports_focused_param() else {
            return false;
        };
        let mut value = self.reports_param_value(&report_id, &param);
        if value.pop().is_none() {
            return false;
        }
        self.reports_set_param(&report_id, &param.id, value);
        true
    }

    /// `true` when the focused field is a free-text param — the only state
    /// in which typing edits a value instead of hitting the panel's own
    /// single-key bindings (`r`, `j`/`k`, …).
    pub(crate) fn reports_focus_is_text(&self) -> bool {
        self.reports_focused_param()
            .map(|(_, p)| p.kind != "choice" || p.choices.is_empty())
            .unwrap_or(false)
    }
}

/// #1765: where an Export should be written, once the save dialog has had
/// its say. `Cancelled` is a *successful* no-op — nothing written, nothing
/// reported as an error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ReportExportDest {
    Path(std::path::PathBuf),
    Cancelled,
}
