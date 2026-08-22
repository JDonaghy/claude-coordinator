//! Approved work items ActivityBar panel (#2532, ms-67 contract §3).
//!
//! A plain aligned-column list of portal submissions ready for
//! decomposition into coordinator work — today `signoff.approved`
//! submissions (once `coord-portal`#132's operator "start work" override
//! lands, that widens too, but that is a wire-shape amendment, not a client
//! change). Modeled on the Audit panel's own list, not the bordered
//! `DataTable` grid Queue uses — no sortable numeric columns and no
//! drag-reorder verb here (contract §3c).
//!
//! **No fetch of its own.** Like Queue, rows ride the existing `/board`
//! poll (`BoardData::approved_submissions`, server-computed by
//! `coord/serve_app.py` — `repos` is already resolved via #2531's
//! project↔repo mapping, so this module never reads `coordinator.yml`
//! directly). See `SidebarView::Approved`'s doc comment for why the #2336
//! daemon-host guard that gates `coord portal outbox/events` does not apply
//! here: this panel only ever talks to `/board` over HTTP, same as every
//! other panel in this app.
//!
//! **Detail pane (contract §3e).** Enter opens an inline detail region
//! below the list, headed by a manually-authored `"── Submission Detail
//! ──"` divider line (NOT `ListView::bordered`'s rounded-box title overlay
//! — quadraui's `draw_list` renders that as `"╭─ Title ─────╮"`, a single
//! dash before the title rather than the two the contract's convention
//! pins verbatim, so reusing it would silently drift from the contract).
//! Long field values (outcome/audience/done/constraints) word-wrap with
//! continuation lines indented to the value column (13 chars, contract
//! §3e / §6.2) — the wrap width is derived from the actual painted detail
//! rect so a value's tail is never clipped by the renderer regardless of
//! terminal width.
#[allow(unused_imports)]
use super::*;
use super::doc_tabs::truncate_with_ellipsis;

/// Contract §3c's pinned column widths (display columns, not derived from
/// any existing constant — same posture ms-65 §2b/§9 took for its own
/// truncation widths).
const APPROVED_COL_SUBMISSION: usize = 12;
const APPROVED_COL_CLIENT: usize = 22;
const APPROVED_COL_REPOS: usize = 16;
const APPROVED_COL_OUTCOME: usize = 32;

/// Contract §3c: the literal placeholder for a row whose `repos_for_project`
/// resolved empty — never a blank cell, so "no mapping" and "not yet
/// loaded" are never visually indistinguishable.
const APPROVED_NO_MAPPING: &str = "— no mapping —";

/// Contract §3e: the value column continuation lines indent to (13 chars —
/// the width of e.g. `"constraints: "`).
const APPROVED_DETAIL_LABEL_WIDTH: usize = 13;

/// Floor (in text rows) for the inline detail region when it is open — the
/// `"── Submission Detail ──"` divider plus the nine identity/briefing
/// fields, plus one row of headroom for a wrapped value. Without a floor a
/// list long enough to fill the panel leaves the detail rect zero-height and
/// `Enter` visibly does nothing; `render_audit_panel` reserves its own
/// `lh * 7.0` floor for exactly this reason.
const APPROVED_DETAIL_MIN_ROWS: f32 = 11.0;

impl CoordApp {
    /// The approved-submissions list, server-order (contract §3c:
    /// oldest-first — mirrors `list_submissions()`'s own `ORDER BY
    /// first_seen_at ASC`; harness note 5 in the sealed slice: the client
    /// preserves payload order rather than re-deriving it).
    pub(crate) fn approved_submissions(&self) -> &[ApprovedSubmission] {
        &self.data.approved_submissions
    }

    /// Selected row index, clamped against the current row count. `0` on an
    /// empty list (never read in that case — callers check emptiness
    /// first), same pattern as `audit_selected_idx`.
    pub(crate) fn approved_selected_idx(&self) -> usize {
        let n = self.approved_submissions().len();
        if n == 0 {
            0
        } else {
            self.approved_sel.min(n - 1)
        }
    }

    /// The currently-selected submission, or `None` on an empty list.
    pub(crate) fn approved_selected(&self) -> Option<&ApprovedSubmission> {
        self.approved_submissions().get(self.approved_selected_idx())
    }

    /// Count of rows whose `repos` resolved empty — drives both the
    /// sidebar's conditional "missing a repo mapping" line (contract §3b)
    /// and (in a follow-up, #2533) the disabled state of the "Pull into
    /// decomposition session" context-menu item.
    fn approved_missing_mapping_count(&self) -> usize {
        self.approved_submissions()
            .iter()
            .filter(|e| e.repos.is_empty())
            .count()
    }

    /// Sidebar content (contract §3b): the panel title (" APPROVED WORK
    /// ITEMS ", rendered by `ShellConfig` from `shell_config()`'s
    /// `PanelDefinition.title`, mirrored here on the `ListView` itself same
    /// as Audit/Queue) plus a "N ready to pull" aggregate line and an
    /// optional "⚠ M missing a repo mapping" line, only rendered when M > 0
    /// — mirrors `queue_sidebar`'s own `if s.blocked > 0`.
    pub(crate) fn approved_sidebar(&self) -> ListView {
        let n = self.approved_submissions().len();
        let mut items = vec![activity_item(
            &format!("  {n} ready to pull"),
            Color::rgb(160, 160, 160),
        )];
        let missing = self.approved_missing_mapping_count();
        if missing > 0 {
            items.push(activity_item(
                &format!("  ⚠ {missing} missing a repo mapping"),
                Color::rgb(255, 210, 100),
            ));
        }
        ListView {
            id: WidgetId::new("approved-sidebar"),
            title: Some(StyledText::plain(" APPROVED WORK ITEMS ")),
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

    /// The column-header row (contract §3c), painted into its own one-row
    /// rect above the scrolling row list so it stays pinned no matter how
    /// far `approved_scroll` has advanced. (It used to be item 0 of the row
    /// list itself, which both scrolled the header away and forced every
    /// selection index to carry a +1 offset.)
    fn approved_header_view() -> ListView {
        ListView {
            id: WidgetId::new("approved-header"),
            title: None,
            items: vec![approved_plain_item(
                format!(
                    "{}{}{}{}",
                    approved_pad("Submission", APPROVED_COL_SUBMISSION),
                    approved_pad("Client / Project", APPROVED_COL_CLIENT),
                    approved_pad("Repo(s)", APPROVED_COL_REPOS),
                    "Outcome",
                ),
                Color::rgb(150, 180, 220),
            )],
            // No selection highlight on a header: `has_focus: false` keeps
            // `draw_list` from painting the `▶` prefix / selected background.
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// #1094-style scroll follow: keep `approved_sel` inside the visible
    /// window. Must be called after every keyboard nav that moves
    /// `approved_sel` (`j`/`k`/`Down`/`Up`/`Home`/`End` in `events.rs`) —
    /// `ListView` has no "scroll to keep the selection visible" behaviour of
    /// its own, so without this the selection walks off the bottom of the
    /// first screenful and the selected row is never painted. Mirrors
    /// `fix_audit_scroll` / `fix_queue_scroll`.
    ///
    /// `visible` is the number of *submission* rows the panel can paint —
    /// pass `content_visible_rows(main_bounds, lh)`, whose one-row title
    /// deduction matches the pinned header row this panel reserves.
    pub(crate) fn fix_approved_scroll(&mut self, visible: usize) {
        if visible == 0 {
            return;
        }
        let sel = self.approved_selected_idx();
        if sel < self.approved_scroll {
            self.approved_scroll = sel;
        } else if sel >= self.approved_scroll + visible {
            self.approved_scroll = sel + 1 - visible;
        }
    }

    /// Build the main-panel row list (contract §3c) — one row per
    /// submission (the header is painted separately by
    /// `approved_header_view`). `list_view` (not a `DataTable`): no sortable
    /// numeric columns, no drag-reorder verb.
    fn approved_list_view(&self, sel: usize) -> ListView {
        let mut items = Vec::with_capacity(self.approved_submissions().len());
        for e in self.approved_submissions() {
            let client_project = format!("{} / {}", e.client, e.project_label);
            let repos_text = if e.repos.is_empty() {
                APPROVED_NO_MAPPING.to_string()
            } else {
                e.repos.join(", ")
            };
            items.push(approved_plain_item(
                format!(
                    "{}{}{}{}",
                    approved_pad(&e.submission_id, APPROVED_COL_SUBMISSION),
                    approved_pad(&client_project, APPROVED_COL_CLIENT),
                    approved_pad(&repos_text, APPROVED_COL_REPOS),
                    truncate_with_ellipsis(&e.outcome, APPROVED_COL_OUTCOME),
                ),
                Color::rgb(200, 200, 200),
            ));
        }
        ListView {
            id: WidgetId::new("approved-list"),
            title: None,
            items,
            selected_idx: sel,
            // Was hardcoded to `0`, which made every row past the first
            // screenful unreachable however far `j`/`End` moved the
            // selection. Kept in step with `approved_sel` by
            // `fix_approved_scroll` at the events.rs nav sites; `.min(sel)`
            // is the defensive half — a `/board` poll that shrinks the list
            // can strand `approved_scroll` below a re-clamped selection, and
            // the render path must never paint a window the selected row
            // sits above. (The overshoot half is clamped by quadraui's own
            // `clamp_scroll_offset`.)
            scroll_offset: self.approved_scroll.min(sel),
            has_focus: true,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Build the inline submission-detail pane (contract §3e): the
    /// `"── Submission Detail ──"` divider, identity/routing context
    /// (submission/client/project/repos/received), then the four briefing
    /// fields (#2533 consumes the same four) with long values word-wrapped
    /// to `wrap_width` and continuation lines indented to the value column.
    fn approved_detail_items(entry: &ApprovedSubmission, wrap_width: usize, rect_width: usize) -> Vec<ListItem> {
        let mut divider = "── Submission Detail ──".to_string();
        while divider.chars().count() < rect_width {
            divider.push('─');
        }
        let mut items = vec![approved_plain_item(divider, Color::rgb(230, 230, 255))];

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let received = crate::app::data::parse_iso8601_to_epoch(&entry.received_at)
            .map(|ts| format_age(Some(ts), now))
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "unknown".to_string());
        let repos_text = if entry.repos.is_empty() {
            APPROVED_NO_MAPPING.to_string()
        } else {
            entry.repos.join(", ")
        };

        let mut push_field = |label: &str, value: &str| {
            let lines = wrap_words(value, wrap_width);
            let mut it = lines.into_iter();
            let first = it.next().unwrap_or_default();
            items.push(approved_plain_item(
                format!("{:<width$}{}", label, first, width = APPROVED_DETAIL_LABEL_WIDTH),
                Color::rgb(210, 210, 210),
            ));
            for cont in it {
                items.push(approved_plain_item(
                    format!("{:width$}{}", "", cont, width = APPROVED_DETAIL_LABEL_WIDTH),
                    Color::rgb(210, 210, 210),
                ));
            }
        };

        push_field("submission:", &entry.submission_id);
        push_field("client:", &entry.client);
        push_field(
            "project:",
            &format!("{} ({})", entry.project_id, entry.project_label),
        );
        push_field("outcome:", &entry.outcome);
        push_field("audience:", &entry.audience);
        push_field("done:", &entry.done_definition);
        push_field("constraints:", &entry.constraints);
        push_field("repos:", &repos_text);
        push_field("received:", &received);
        items
    }

    /// Render the Approved work items main panel (contract §3d/§3e): empty
    /// state, populated list, or list-plus-inline-detail split when
    /// `approved_detail_open`.
    pub(crate) fn render_approved_panel(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        let entries = self.approved_submissions();
        if entries.is_empty() {
            // Contract §3d: the empty-state message deliberately never
            // contains the substring "Submission" (the header-only word) —
            // see the module doc comment on why that phrasing was picked.
            backend.draw_list(
                rect,
                &plain_list(
                    "approved-empty",
                    "  No approved work items yet — check back after a customer signs off.",
                    0,
                ),
            );
            return;
        }

        let sel = self.approved_selected_idx();
        // Header row + one row per submission, capped at the panel.
        let list_natural_h = ((entries.len() + 1) as f32 * lh).min(rect.height);
        let (list_rect, detail_rect) = if self.approved_detail_open {
            // The list only ever takes what it actually needs, so a short
            // list still hands the whole remainder to the detail region —
            // but the detail region never shrinks below
            // `APPROVED_DETAIL_MIN_ROWS`, so a list long enough to fill the
            // panel can no longer squeeze it to zero height (which made
            // `Enter` look like a no-op). The floor is itself capped at half
            // the panel so the list above never collapses on a short
            // terminal either.
            let min_detail_h = (lh * APPROVED_DETAIL_MIN_ROWS).min(rect.height * 0.5);
            let detail_h = (rect.height - list_natural_h)
                .max(min_detail_h)
                .min(rect.height);
            let list_h = (rect.height - detail_h).max(0.0);
            let list_rect = Rect::new(rect.x, rect.y, rect.width, list_h);
            let detail_rect = Rect::new(rect.x, rect.y + list_h, rect.width, detail_h);
            (list_rect, Some(detail_rect))
        } else {
            (rect, None)
        };

        // The header is painted into its own one-row rect so it stays pinned
        // while the rows below it scroll.
        let header_h = lh.min(list_rect.height);
        backend.draw_list(
            Rect::new(list_rect.x, list_rect.y, list_rect.width, header_h),
            &Self::approved_header_view(),
        );
        backend.draw_list(
            Rect::new(
                list_rect.x,
                list_rect.y + header_h,
                list_rect.width,
                (list_rect.height - header_h).max(0.0),
            ),
            &self.approved_list_view(sel),
        );

        if let Some(detail_rect) = detail_rect {
            if let Some(entry) = self.approved_selected() {
                let rect_width = detail_rect.width.round().max(0.0) as usize;
                let wrap_width = rect_width.saturating_sub(APPROVED_DETAIL_LABEL_WIDTH).max(20);
                backend.draw_list(
                    detail_rect,
                    &ListView {
                        id: WidgetId::new("approved-detail"),
                        title: None,
                        items: Self::approved_detail_items(entry, wrap_width, rect_width),
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
    }
}

/// Left-align `text` within `width` display columns, truncating with an
/// ellipsis (via `doc_tabs::truncate_with_ellipsis`) when it overflows.
fn approved_pad(text: &str, width: usize) -> String {
    let n = text.chars().count();
    if n >= width {
        return truncate_with_ellipsis(text, width);
    }
    format!("{}{}", text, " ".repeat(width - n))
}

/// A plain, unstyled-key `ListItem` — this panel's rows/detail lines are
/// pre-formatted whole strings (column-padded or label-indented), not
/// `kv_item`'s `" {key:12} "` key/value split (which reserves a different,
/// wider prefix than this panel's contract-pinned 13-column value column).
fn approved_plain_item(text: String, color: Color) -> ListItem {
    ListItem {
        text: StyledText {
            spans: vec![StyledSpan::with_fg(text, color)],
        },
        icon: None,
        detail: None,
        decoration: Decoration::Normal,
    }
}

/// Small ASCII/whitespace word-wrapper (contract §3e / §6.2 — a new
/// convention no prior detail pane in this crate needed): breaks `text` on
/// whitespace into lines no longer than `width` characters. A single word
/// longer than `width` is kept whole on its own line rather than
/// hard-split, mirroring `trunc`'s existing "no more clever than it needs
/// to be" posture elsewhere in this crate.
fn wrap_words(text: &str, width: usize) -> Vec<String> {
    if width == 0 {
        return vec![text.to_string()];
    }
    let mut lines = Vec::new();
    let mut current = String::new();
    for word in text.split_whitespace() {
        if current.is_empty() {
            current.push_str(word);
        } else if current.chars().count() + 1 + word.chars().count() <= width {
            current.push(' ');
            current.push_str(word);
        } else {
            lines.push(std::mem::take(&mut current));
            current.push_str(word);
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::fixtures::make_test_app;
    use quadraui::tui::testing::driver_with_shell;

    /// `n` synthetic approved submissions, ids `sub_0000`..`sub_{n-1}` —
    /// enough of them to overflow any terminal the driver builds, which is
    /// the precondition both regressions below need.
    fn approved_rows(n: usize) -> Vec<ApprovedSubmission> {
        (0..n)
            .map(|i| ApprovedSubmission {
                submission_id: format!("sub_{i:04}"),
                client: format!("Client {i}"),
                project_id: format!("proj-{i}"),
                project_label: format!("Project {i}"),
                outcome: format!("Outcome number {i} for the acceptance fixture"),
                audience: "Internal ops".to_string(),
                done_definition: "Ships and is observable".to_string(),
                constraints: "No new dependencies".to_string(),
                repos: vec!["claude-coordinator".to_string()],
                received_at: "2026-08-01T00:00:00Z".to_string(),
            })
            .collect()
    }

    fn approved_driver(n: usize) -> quadraui::tui::testing::TuiDriver<impl quadraui::AppLogic> {
        let mut app = make_test_app(BoardData {
            approved_submissions: approved_rows(n),
            ..BoardData::default()
        });
        app.active_view = SidebarView::Approved;
        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 120, 40);
        driver.render();
        driver
    }

    /// Regression: `approved_list_view` used to hardcode `scroll_offset: 0`,
    /// so `End` (and a long run of `j`) moved `approved_sel` past the first
    /// screenful while the list never scrolled — the selected row was simply
    /// never painted. Same defect class as #1094's `fix_audit_scroll`.
    #[test]
    fn end_scrolls_the_last_approved_row_into_view() {
        let mut driver = approved_driver(60);

        // Precondition: the tail row is genuinely off-screen to begin with,
        // otherwise this test would pass against the buggy code.
        assert!(
            !driver.screen_contains("sub_0059"),
            "fixture must overflow the panel for this regression to bite:\n{}",
            driver.screen(),
        );

        driver.press_named(quadraui::NamedKey::End);
        driver.render();

        assert!(
            driver.screen_contains("sub_0059"),
            "End must scroll the last approved row into view:\n{}",
            driver.screen(),
        );
    }

    /// Same regression from the other direction: repeated `j` past the
    /// bottom of the window must drag the viewport along with it, and `k`
    /// back to the head must rewind it.
    #[test]
    fn j_and_k_keep_the_selected_approved_row_on_screen() {
        let mut driver = approved_driver(60);

        for _ in 0..50 {
            driver.type_char('j');
        }
        driver.render();
        assert!(
            driver.screen_contains("sub_0050"),
            "j past the first screenful must scroll the selection into view:\n{}",
            driver.screen(),
        );

        for _ in 0..50 {
            driver.type_char('k');
        }
        driver.render();
        assert!(
            driver.screen_contains("sub_0000"),
            "k back to the head must rewind the scroll window:\n{}",
            driver.screen(),
        );
    }

    /// The column header is painted into its own pinned rect, so it survives
    /// scrolling instead of being item 0 of the scrolled list.
    #[test]
    fn column_header_stays_pinned_while_the_list_scrolls() {
        let mut driver = approved_driver(60);
        driver.press_named(quadraui::NamedKey::End);
        driver.render();

        assert!(
            driver.screen_contains("Submission"),
            "the column header must stay pinned once the list has scrolled:\n{}",
            driver.screen(),
        );
    }

    /// Regression: `render_approved_panel` handed the detail region whatever
    /// height the list did not use, with no floor — so on a list long enough
    /// to fill the panel `detail_rect` was zero-height and `Enter` looked
    /// like a no-op. `render_audit_panel` has reserved a floor for exactly
    /// this since it shipped.
    #[test]
    fn detail_pane_still_renders_when_the_list_fills_the_panel() {
        let mut driver = approved_driver(60);
        assert!(
            !driver.screen_contains("Submission Detail"),
            "detail must start closed:\n{}",
            driver.screen(),
        );

        driver.press_named(quadraui::NamedKey::Enter);
        driver.render();

        assert!(
            driver.screen_contains("Submission Detail"),
            "Enter must open a non-degenerate detail region even when the \
             row list alone would fill the panel:\n{}",
            driver.screen(),
        );
        for label in ["outcome:", "audience:", "done:", "constraints:"] {
            assert!(
                driver.screen_contains(label),
                "the detail floor must leave room for the `{label}` field:\n{}",
                driver.screen(),
            );
        }
    }

    /// The short-list case must be unchanged: with only a couple of rows the
    /// detail region still gets all the leftover height, not just the floor.
    #[test]
    fn short_list_detail_pane_keeps_the_whole_remainder() {
        let mut driver = approved_driver(2);
        driver.press_named(quadraui::NamedKey::Enter);
        driver.render();

        assert!(
            driver.screen_contains("Submission Detail"),
            "Enter must open the detail region on a short list too:\n{}",
            driver.screen(),
        );
        assert!(
            driver.screen_contains("received:"),
            "the last detail field must be visible on a short list:\n{}",
            driver.screen(),
        );
    }
}
