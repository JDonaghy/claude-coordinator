//! Reports ActivityBar panel (#1741) — Phase B of the Reports feature.
//!
//! A stack of collapsible sections, one per entry in the report catalogue
//! (`GET /report`, #1742). Each section's body is that report's parameter
//! form plus a `Run` button; running it renders the resulting `ReportResult`
//! as a `DataTable` with the notes block beneath.
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
                        actions: Vec::new(),
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

    /// Main-axis rows one section occupies: its header, plus its form's
    /// fields when expanded. Mirrors `tui_msv_layout`'s own `measure`
    /// closure (`SectionBody::Form(f) => f.fields.len()`), which is what
    /// makes the carved `msv_rect` below exactly fit the stack.
    fn reports_section_rows(&self, def: &ReportDef) -> usize {
        1 + if self.reports_is_expanded(&def.id) {
            Self::reports_field_count(def)
        } else {
            0
        }
    }

    // ── Sidebar ──────────────────────────────────────────────────────────

    /// Sidebar content: the panel title, the catalogue size, and the last
    /// run's window + row count. Follows `audit_sidebar` — a bare `ListView`,
    /// not a `SidebarSystem` tree.
    pub(crate) fn reports_sidebar(&self) -> ListView {
        let n = self.reports_catalogue().len();
        let mut items = vec![activity_item(
            &format!("  {n} report{}", if n == 1 { "" } else { "s" }),
            Color::rgb(160, 160, 160),
        )];
        if let Some(def) = self.reports_selected() {
            items.push(activity_item(
                &format!("  Selected: {}", def.title),
                Color::rgb(150, 180, 220),
            ));
            // The catalogue's own one-liner — the only place it fits, since
            // the section header is a single row of title + badge.
            if !def.description.is_empty() {
                items.push(activity_item(
                    &format!("  {}", trunc(&def.description, 30)),
                    Color::rgb(140, 140, 140),
                ));
            }
            // The values the next run will actually use. A segmented
            // control shows every option and marks the selected one with
            // colour alone, which is invisible to anything reading the
            // rendered grid (the operator squinting at a dim palette, and
            // the screen-level tests) — this states it in text.
            for param in &def.params {
                let value = self.reports_param_value(&def.id, param);
                let label = if param.label.is_empty() {
                    param.id.as_str()
                } else {
                    param.label.as_str()
                };
                items.push(activity_item(
                    &format!("  {label}: {value}"),
                    Color::rgb(150, 180, 220),
                ));
            }
        }
        match (&self.reports_running, &self.reports_result) {
            (Some(id), _) => items.push(activity_item(
                &format!("  Running: {id}"),
                Color::rgb(230, 200, 120),
            )),
            (None, Some(result)) => {
                items.push(activity_item(
                    &format!(
                        "  Last run: {} row{}",
                        result.rows.len(),
                        if result.rows.len() == 1 { "" } else { "s" }
                    ),
                    Color::rgb(140, 200, 140),
                ));
                if let Some(window) = Self::reports_window_label(result) {
                    items.push(activity_item(
                        &format!("  Window: {window}"),
                        Color::rgb(150, 180, 220),
                    ));
                }
            }
            (None, None) => items.push(activity_item(
                "  No run yet (Enter to run)",
                Color::rgb(140, 140, 140),
            )),
        }
        if let Some(err) = &self.reports_error {
            items.push(activity_item(
                &format!("  Error: {}", trunc(err, 28)),
                Color::rgb(230, 130, 130),
            ));
        }
        ListView {
            id: WidgetId::new("reports-sidebar"),
            title: Some(StyledText::plain(" REPORTS ")),
            items,
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
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

    /// One result cell, looked up **by column name** — the engine documents
    /// that `rows` may carry keys beyond `columns`, so positional indexing
    /// would silently mis-align the moment a report adds a detail field.
    fn reports_cell_text(row: &serde_json::Value, column: &str) -> String {
        match row.get(column) {
            None | Some(serde_json::Value::Null) => String::new(),
            Some(serde_json::Value::String(s)) => s.clone(),
            Some(serde_json::Value::Bool(b)) => b.to_string(),
            Some(serde_json::Value::Number(n)) => n.to_string(),
            Some(other) => other.to_string(),
        }
    }

    /// Columns for the result `DataTable`, derived entirely from
    /// `ReportResult.columns`. Every column flexes equally: the panel has no
    /// idea what any of them mean, so it cannot pick better weights — that
    /// is the price of not hardcoding reports, and it is the right trade.
    fn reports_result_columns(result: &ReportResult) -> Vec<Column> {
        result
            .columns
            .iter()
            .map(|c| Column {
                title: c.clone(),
                width: ColumnWidth::Flex(1.0),
                align: ColumnAlign::Left,
            })
            .collect()
    }

    fn reports_result_rows(result: &ReportResult) -> Vec<DataRow> {
        result
            .rows
            .iter()
            .map(|row| DataRow {
                cells: result
                    .columns
                    .iter()
                    .map(|c| StyledText {
                        spans: vec![StyledSpan::with_fg(
                            Self::reports_cell_text(row, c),
                            Color::rgb(200, 200, 200),
                        )],
                    })
                    .collect(),
                decoration: Decoration::Normal,
            })
            .collect()
    }

    // ── Render ───────────────────────────────────────────────────────────

    /// Render the Reports main panel: the collapsible section stack on top,
    /// the last run's result (table + notes) beneath it.
    pub(crate) fn render_reports_panel(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        let catalogue = self.reports_catalogue();
        if catalogue.is_empty() {
            // Deliberately NOT the audit panel's "treat unfetched and empty
            // identically" posture: an empty `/report` catalogue means the
            // daemon is older than #1742 (or unreachable), and that has to
            // be diagnosable without attaching a debugger.
            let message = if let Some(reason) = &self.reports_error {
                format!("  No reports available.  (catalogue fetch failed: {reason})")
            } else if self.reports_no_service {
                "  No reports available.  (no board service configured)".to_string()
            } else if self.reports_catalogue.is_none() {
                "  Loading reports…".to_string()
            } else {
                "  No reports available.  (the daemon's catalogue is empty)".to_string()
            };
            self.reports_layout.borrow_mut().clear();
            backend.draw_list(rect, &plain_list("reports-empty", &message, 0));
            return;
        }

        // Carve the section stack off the top at exactly the height it
        // needs, so the remainder goes to the result. `lh` converts rows to
        // backend units (1.0 per row in the TUI), same as `audit.rs`'s
        // detail-pane split.
        let stack_rows: usize = catalogue.iter().map(|d| self.reports_section_rows(d)).sum();
        let max_rows = ((rect.height / lh).floor() as usize).max(1);
        // Always leave at least a few rows for the result area once there is
        // something to show there, so a long section stack can't hide it.
        let reserve = if self.reports_has_result_area() {
            Self::REPORTS_RESULT_MIN_ROWS
        } else {
            0
        };
        let msv_rows = stack_rows.min(max_rows.saturating_sub(reserve).max(1));
        let msv_h = (msv_rows as f32 * lh).min(rect.height);
        let msv_rect = Rect::new(rect.x, rect.y, rect.width, msv_h);

        // Layout ONCE, paint from that layout, cache it for hit-testing —
        // the contract `MultiSectionView` exists to enforce (see
        // `app/msv.rs`'s module docs). `Backend::msv_layout` and
        // `Backend::draw_multi_section_view` both delegate to the
        // rasteriser's `tui_msv_layout`, so both consume identical metrics.
        let view = self.reports_view();
        let layout = backend.msv_layout(msv_rect, &view);
        backend.draw_multi_section_view(msv_rect, &view);
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

        let result_rect = Rect::new(
            rect.x,
            rect.y + msv_h,
            rect.width,
            (rect.height - msv_h).max(0.0),
        );
        if result_rect.height >= lh {
            self.render_reports_result(backend, result_rect, lh);
        }
    }

    /// Rows reserved for the result area whenever there is anything to show
    /// there (a run, an error, or a completed result): enough for a table
    /// header plus a couple of rows, so it can never be squeezed to nothing.
    const REPORTS_RESULT_MIN_ROWS: usize = 6;

    /// Whether the area below the section stack has anything to render.
    fn reports_has_result_area(&self) -> bool {
        self.reports_running.is_some()
            || self.reports_result.is_some()
            || self.reports_error.is_some()
    }

    /// The result area beneath the section stack: an in-flight line, a
    /// failure, an explicit no-activity body, or the table + notes.
    fn render_reports_result(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
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
        let notes_rows = if result.notes.is_empty() {
            0
        } else {
            (result.notes.len() + 2).min(Self::REPORTS_NOTES_MAX_ROWS)
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
            let table = DataTable {
                id: WidgetId::new("reports-result"),
                columns: Self::reports_result_columns(result),
                rows: Self::reports_result_rows(result),
                selected_idx: None,
                scroll_offset: self.reports_result_scroll,
                sort: None,
                has_focus: false,
                show_scrollbar: true,
                min_total_width: None,
                h_scroll: 0.0,
                column_overrides: Vec::new(),
                footer: None,
            };
            backend.draw_data_table(table_rect, &table, None);
        }

        if notes_h > 0.0 {
            let items: Vec<ListItem> = result
                .notes
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
        self.reports_running = Some(def.id.clone());
        self.reports_run_rx = Some(spawn_report_run(&def.id, params));
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
