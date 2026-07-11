//! Audit ActivityBar panel (#1039).
//!
//! Newest-first list of the audit trail (`/audit`, #1037) with an inline
//! entry-detail view — modeled on the Plans panel (`plans.rs`): a plain
//! `ListView`/`draw_list` main panel (no `SidebarSystem`/tree), a simple
//! `usize` selection index clamped on navigation, and a background fetch
//! gated to only run while this panel is the active view (mirrors
//! `spawn_artifact_fetch`'s Pipeline-only gating in `data.rs`).
//!
//! **Read-only in this slice.** Filters (time-range `t`, category `Tab`)
//! are #1040 — out of scope here per the #1039 issue body.
//!
//! **Contract:** `tests/acceptance/ms-33/contract.md` (Gate A, mock-authored
//! before this issue dispatched) pins the exact panel registration, screen
//! strings, hint text, and `/audit` wire shape this module implements
//! against. The sealed acceptance slice (`tests/acceptance/ms-33/
//! audit_1039.rs`) currently covers only the no-seeding-required behaviours
//! (panel registration, sidebar header, empty state, list-mode hints); the
//! populated-list/detail-pane/badge assertions are deferred there pending a
//! seeding seam — see `app::fixtures::make_app_with_audit_json`.
#[allow(unused_imports)]
use super::*;

impl CoordApp {
    /// The cached audit entries, or an empty slice when nothing has been
    /// fetched yet (or the fetch found a genuinely empty log). Both cases
    /// render identically — contract §4b deliberately treats "not fetched
    /// yet" the same as "fetched, zero rows" so a slow first fetch never
    /// reads as broken.
    pub(crate) fn audit_entries(&self) -> &[AuditEntry] {
        self.audit_page
            .as_ref()
            .map(|p| p.entries.as_slice())
            .unwrap_or(&[])
    }

    /// Selected row index, clamped against the current entry count. `0` on
    /// an empty list (never read in that case — callers check emptiness
    /// first).
    pub(crate) fn audit_selected_idx(&self) -> usize {
        let n = self.audit_entries().len();
        if n == 0 {
            0
        } else {
            self.audit_sel.min(n - 1)
        }
    }

    /// The currently-selected audit entry, or `None` on an empty list.
    pub(crate) fn audit_selected(&self) -> Option<&AuditEntry> {
        self.audit_entries().get(self.audit_selected_idx())
    }

    /// Sidebar content (#1039 contract §3): the panel title (" AUDIT ",
    /// rendered by `ShellConfig.with_status_bar()` from `shell_config()`'s
    /// `PanelDefinition.title`, mirrored here on the `ListView` itself same
    /// as `plans_sidebar()`) plus an entry-count line and an optional
    /// "N recent" badge sourced from `/board`'s `audit_recent_count` (kept
    /// separate from the cached page's own entry count per contract §3:
    /// "The count comes from the cached AuditPage ... not the /board
    /// audit_recent_count field").
    pub(crate) fn audit_sidebar(&self) -> ListView {
        let n = self.audit_entries().len();
        let count_line = format!("  {n} entr{}", if n == 1 { "y" } else { "ies" });
        let mut items = vec![activity_item(&count_line, Color::rgb(160, 160, 160))];
        let recent = self.audit_recent_count();
        if recent > 0 {
            items.push(activity_item(
                &format!("  {recent} recent"),
                Color::rgb(120, 210, 120),
            ));
        }
        ListView {
            id: WidgetId::new("audit-sidebar"),
            title: Some(StyledText::plain(" AUDIT ")),
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

    /// Count of audit rows in the last 15 minutes (`/board`'s
    /// `audit_recent_count`, #1037) — the basis for a future activity-bar
    /// attention dot (issue deliverable #7). Mirrors
    /// `plans_needing_attention_count`'s role as the single source both the
    /// sidebar badge above and any global status-bar/ActivityBar indicator
    /// read from, so the two can never drift out of sync.
    pub(crate) fn audit_recent_count(&self) -> u64 {
        self.data.audit_recent_count
    }

    /// One main-panel row's display text (contract §4a): relative time,
    /// category, actor, `repo#issue`, summary — verbatim except the
    /// summary, which is truncated to keep rows readable.
    fn audit_row_text(entry: &AuditEntry) -> String {
        let time_ago = format_unix_time(entry.ts);
        let repo_issue = match (&entry.repo, entry.issue) {
            (Some(repo), Some(n)) => format!("{repo}#{n}"),
            (Some(repo), None) => repo.clone(),
            (None, Some(n)) => format!("#{n}"),
            (None, None) => String::new(),
        };
        format!(
            "  {}  {}  {}  {}  {}",
            time_ago,
            entry.category,
            entry.actor,
            repo_issue,
            trunc(&entry.summary, 60),
        )
    }

    /// Build the flat `ListItem` row list for the populated main panel.
    fn audit_row_items(entries: &[AuditEntry]) -> Vec<ListItem> {
        entries
            .iter()
            .map(|entry| ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(
                        Self::audit_row_text(entry),
                        Color::rgb(200, 200, 200),
                    )],
                },
                icon: None,
                detail: None,
                decoration: Decoration::Normal,
            })
            .collect()
    }

    /// Inline entry-detail pane content (contract §4c): a header line plus
    /// one key/value row per required field. `details` renders as its raw
    /// JSON (`"{}"` when absent/null).
    fn audit_detail_items(entry: &AuditEntry) -> Vec<ListItem> {
        let details_str = entry
            .details
            .as_ref()
            .map(|v| v.to_string())
            .unwrap_or_else(|| "{}".to_string());
        let mut items = vec![
            kv_item("", " Entry Detail", Some(Color::rgb(230, 230, 255))),
            kv_item("id:", &entry.id.to_string(), None),
            kv_item("ts:", &entry.ts.to_string(), None),
            kv_item("category:", &entry.category, None),
            kv_item("event_type:", &entry.event_type, None),
            kv_item("actor:", &entry.actor, None),
        ];
        // The remaining wire fields (contract §6) are optional in practice
        // (e.g. a `plan`/`error` category entry may have no repo/issue) —
        // shown only when present, rather than as an always-blank row.
        if let Some(tier) = &entry.tier {
            items.push(kv_item("tier:", tier, None));
        }
        if let Some(assignment_id) = &entry.assignment_id {
            items.push(kv_item("assignment:", assignment_id, None));
        }
        if let Some(machine) = &entry.machine {
            items.push(kv_item("machine:", machine, None));
        }
        items.push(kv_item("summary:", &entry.summary, None));
        items.push(kv_item("details:", &details_str, None));
        items
    }

    /// Render the Audit main panel (#1039): empty state, populated list, or
    /// list-plus-inline-detail split when `audit_detail_open`.
    pub(crate) fn render_audit_panel(&self, backend: &mut dyn Backend, rect: Rect, lh: f32) {
        let entries = self.audit_entries();
        if entries.is_empty() {
            // Contract §4b — treated identically whether the fetch hasn't
            // completed yet or genuinely returned zero rows. When the most
            // recent fetch actually failed (vs. "nothing fetched yet" or
            // "no board service configured"), append the reason — the
            // message still starts with the exact contract-required string.
            let message = match &self.audit_fetch_error {
                Some(reason) => format!("  No audit events yet.  (last fetch failed: {reason})"),
                None => "  No audit events yet.".to_string(),
            };
            backend.draw_list(rect, &plain_list("audit-empty", &message, 0));
            return;
        }

        let sel = self.audit_selected_idx();
        let (list_rect, detail_rect) = if self.audit_detail_open {
            // Reserve roughly the bottom 40% (at least 7 rows: header + 6
            // fields) for the detail pane, the rest for the list above it.
            let min_detail_h = (lh * 7.0).min(rect.height);
            let detail_h = (rect.height * 0.4).max(min_detail_h).min(rect.height);
            let list_h = (rect.height - detail_h).max(0.0);
            let list_rect = Rect::new(rect.x, rect.y, rect.width, list_h);
            let detail_rect = Rect::new(rect.x, rect.y + list_h, rect.width, rect.height - list_h);
            (list_rect, Some(detail_rect))
        } else {
            (rect, None)
        };

        backend.draw_list(
            list_rect,
            &ListView {
                id: WidgetId::new("audit-list"),
                title: None,
                items: Self::audit_row_items(entries),
                selected_idx: sel,
                scroll_offset: 0,
                has_focus: true,
                bordered: false,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: true,
            },
        );

        if let Some(detail_rect) = detail_rect {
            if let Some(entry) = self.audit_selected() {
                backend.draw_list(
                    detail_rect,
                    &ListView {
                        id: WidgetId::new("audit-detail"),
                        title: None,
                        items: Self::audit_detail_items(entry),
                        selected_idx: 0,
                        scroll_offset: 0,
                        has_focus: false,
                        bordered: true,
                        h_scroll: 0,
                        max_content_width: None,
                        show_v_scrollbar: false,
                    },
                );
            }
        }
    }

    /// Map a click position in the main panel to an entry index (flat list,
    /// no header rows — unlike `plans_row_at`, which must skip repo-header
    /// and "+N without a work order" rows). Returns `None` outside the list
    /// rows, when the detail pane is open (the list is out of hit-test
    /// scope while it's collapsed above the detail pane — the caller only
    /// invokes this in list-only mode), or on an empty list.
    pub(crate) fn audit_row_at(&self, pos: Point, main_b: Rect, lh: f32) -> Option<usize> {
        let n = self.audit_entries().len();
        if n == 0 || lh <= 0.0 {
            return None;
        }
        if pos.y < main_b.y {
            return None;
        }
        let row = ((pos.y - main_b.y) / lh).floor() as usize;
        if row < n {
            Some(row)
        } else {
            None
        }
    }

    /// Force the next `run_periodic_work` tick to re-fetch `/audit`
    /// immediately, dropping any in-flight request (`r` — contract §7).
    pub(crate) fn refresh_audit(&mut self) {
        self.audit_fetch_rx = None;
        self.audit_last_fetched = None;
    }
}
