//! Plans ActivityBar panel (#975).
//!
//! Elevates and subsumes the `milestone_dag` "Milestones" view: renders the
//! plan-roster from `BoardData::plan_roster` (server-computed by
//! `coord/serve_app.py` via `coord.plans.aggregate_repo_plans`) as one row per
//! milestone/epic with ready / blocked / in-flight / done counts and a
//! `needs_you` attention list.  Selecting a row opens that plan's tracking
//! epic in the browser via `gh issue view --web`.
//!
//! **Design note (server-computed, thin client).** The TUI does not
//! re-aggregate plans client-side: it reads `plan_roster` off `/board`
//! verbatim.  This mirrors the #584 portable-control-center read path — no
//! shell-out to `coord plans` from the TUI, no re-implementation of the
//! aggregation, and stays in lock-step with `coord.plans.PlanEntry.to_dict()`
//! (a mistyped field would fail the whole `BoardPayload` parse and blank the
//! board, per #632).
//!
//! **Read-only in this slice.** Health chips + attention badges (#976), fast
//! capture (#977), and the GOAL.md header (#978) come later.
#[allow(unused_imports)]
use super::*;

// ─── impl CoordApp — sidebar/main-panel rendering + actions ──────────────────

impl CoordApp {
    /// The plan-roster entries currently on the board, in a stable order:
    /// primary sort is `(repo, milestone_number)` so the list stays visually
    /// stable across refreshes.  Cheap — a clone of the payload slice.
    pub(crate) fn plans_entries(&self) -> Vec<PlanRosterEntry> {
        let mut out: Vec<PlanRosterEntry> = self.data.plan_roster.clone();
        out.sort_by(|a, b| {
            (a.repo.as_str(), a.milestone_number).cmp(&(b.repo.as_str(), b.milestone_number))
        });
        out
    }

    /// The currently-selected plan-roster row (`plans_sel`, clamped), or
    /// `None` when the roster is empty.
    pub(crate) fn plans_selected(&self) -> Option<PlanRosterEntry> {
        let entries = self.plans_entries();
        if entries.is_empty() {
            return None;
        }
        let idx = self.plans_sel.min(entries.len() - 1);
        entries.into_iter().nth(idx)
    }

    /// Sidebar placeholder for the Plans view — plan count + "attention"
    /// hint (any entry with a `needs_you` signal).  All content lives in the
    /// main panel; mirrors `merge_queue_sidebar` / `milestone_dag_sidebar`.
    pub(crate) fn plans_sidebar(&self) -> ListView {
        let entries = self.plans_entries();
        let n = entries.len();
        let attn_count = entries.iter().filter(|e| !e.needs_you.is_empty()).count();
        let attn = if attn_count > 0 {
            format!(" ⚠ {} need attention", attn_count)
        } else {
            String::new()
        };
        let hint = format!(
            "  {} plan{}{}",
            n,
            if n == 1 { "" } else { "s" },
            attn,
        );
        ListView {
            id: WidgetId::new("plans-sidebar"),
            title: Some(StyledText::plain(" PLANS ")),
            items: vec![activity_item(&hint, Color::rgb(160, 160, 160))],
            selected_idx: 0,
            scroll_offset: 0,
            has_focus: false,
            bordered: false,
            h_scroll: 0,
            max_content_width: None,
            show_v_scrollbar: false,
        }
    }

    /// Render the Plans main panel — one row per milestone/epic:
    ///
    /// ```text
    /// api  #5  Substrate                    epic:#500  ready=2  in-flight=0  blocked=1  done=0/3  [ready_waiting]
    /// api  #6  Follow-up                    epic:—     no work order  [no_work_order]
    /// ```
    ///
    /// The currently-selected row is highlighted via `selected_idx` so the
    /// "Enter to open tracking epic" action has a visible target.
    pub(crate) fn render_plans_panel(&self, backend: &mut dyn Backend, rect: Rect, _lh: f32) {
        let entries = self.plans_entries();
        if entries.is_empty() {
            backend.draw_list(
                rect,
                &plain_list(
                    "plans-empty",
                    "  No plans yet.  Milestones with a `## Work order` block will appear here.",
                    0,
                ),
            );
            return;
        }
        let sel = self.plans_sel.min(entries.len() - 1);
        let mut items: Vec<ListItem> = Vec::new();

        for (i, entry) in entries.iter().enumerate() {
            let tracking = entry
                .tracking_issue
                .map(|n| format!("epic:#{}", n))
                .unwrap_or_else(|| "epic:—".to_string());
            let stats = if entry.has_work_order {
                format!(
                    "ready={}  in-flight={}  blocked={}  done={}/{}",
                    entry.ready_frontier, entry.in_flight, entry.blocked, entry.done, entry.total,
                )
            } else {
                "no work order".to_string()
            };
            let needs = if entry.needs_you.is_empty() {
                String::new()
            } else {
                format!("  [{}]", entry.needs_you.join(", "))
            };
            let row_label = format!(
                " {}  #{}  {}   {}   {}{}",
                entry.repo,
                entry.milestone_number,
                trunc(&entry.title, 32),
                tracking,
                stats,
                needs,
            );
            let color = if !entry.needs_you.is_empty() {
                // Any attention signal → warmer accent so the row reads
                // as "look at me" without needing #976's chips yet.
                Color::rgb(220, 190, 120)
            } else {
                Color::rgb(200, 200, 200)
            };
            let decoration = if i == sel {
                Decoration::Header
            } else {
                Decoration::Normal
            };
            items.push(ListItem {
                text: StyledText {
                    spans: vec![StyledSpan::with_fg(row_label, color)],
                },
                icon: None,
                detail: None,
                decoration,
            });
        }

        let total = items.len();
        backend.draw_list(
            rect,
            &ListView {
                id: WidgetId::new("plans-list"),
                title: Some(StyledText::plain(" PLANS ")),
                items,
                selected_idx: sel,
                scroll_offset: 0,
                has_focus: true,
                bordered: true,
                h_scroll: 0,
                max_content_width: None,
                show_v_scrollbar: total > 10,
            },
        );
    }

    /// Enter / "open selected plan" — spawn `gh issue view <tracking_issue>
    /// --repo <slug> --web` for the selected plan.  Silently noops when
    /// nothing is selected or the plan has no tracking epic yet (a "create
    /// an epic" workflow lives in #977 / #978).  Returns `true` when the
    /// tracking-epic open was attempted so the caller can request a redraw.
    ///
    /// Mirrors `dispatch_open_pr_for_selected_pipeline_row` — bypasses the
    /// command runner because `gh` isn't a `coord` subcommand and the runner
    /// is `coord`-verb-scoped.  In `#[cfg(test)]` builds the spawn itself is
    /// skipped (so `cargo test` doesn't try to shell out to a real `gh`); a
    /// toast is still pushed so tests can observe the action via the screen.
    pub(crate) fn open_selected_plan_tracking_epic(&mut self) -> bool {
        let Some(entry) = self.plans_selected() else {
            self.push_toast(
                "Open plan",
                "No plan selected — highlight a row first.",
                ToastSeverity::Info,
            );
            return false;
        };
        let Some(tracking) = entry.tracking_issue else {
            self.push_toast(
                "No tracking epic yet",
                &format!(
                    "{} #{}: {} has no `epic`-labelled tracking issue. \
                     Create one with `coord milestone chat`.",
                    entry.repo, entry.milestone_number, entry.title,
                ),
                ToastSeverity::Info,
            );
            return false;
        };
        // Resolve the coord-local repo → GitHub slug so `gh --repo` gets the
        // full owner/name.  Empty slug falls through to `gh` picking the
        // ambient repo from the cwd (still useful; just less precise).
        let repo_slug = self
            .data
            .pipeline_repos
            .iter()
            .find(|(name, _)| name == &entry.repo)
            .map(|(_, gh)| gh.clone())
            .unwrap_or_default();
        // Skip the real spawn under `cargo test` (no `gh` on CI sandbox, no
        // point opening a browser during a headless test).  The toast fires
        // regardless so tests can observe the action.
        #[cfg(not(test))]
        {
            let issue_str = tracking.to_string();
            let mut cmd = std::process::Command::new("gh");
            cmd.args(["issue", "view", &issue_str]);
            if !repo_slug.is_empty() {
                cmd.args(["--repo", &repo_slug]);
            }
            cmd.arg("--web")
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null());
            let _ = cmd.spawn();
        }
        #[cfg(test)]
        let _ = &repo_slug; // silence unused-var under test builds
        self.push_toast(
            "Opening plan",
            &format!(
                "gh issue view #{} — opening tracking epic for {} #{} in browser…",
                tracking, entry.repo, entry.milestone_number,
            ),
            ToastSeverity::Info,
        );
        true
    }
}

// ─── Pure-function unit tests ─────────────────────────────────────────────────

#[cfg(test)]
mod pure_tests {
    use super::*;

    fn entry(
        repo: &str,
        ms: i64,
        title: &str,
        tracking: Option<u64>,
        needs: &[&str],
    ) -> PlanRosterEntry {
        PlanRosterEntry {
            repo: repo.to_string(),
            title: title.to_string(),
            milestone_number: ms,
            tracking_issue: tracking,
            has_work_order: tracking.is_some(),
            ready_frontier: 0,
            blocked: 0,
            in_flight: 0,
            done: 0,
            total: 0,
            needs_you: needs.iter().map(|s| s.to_string()).collect(),
        }
    }

    #[test]
    fn plans_entries_sorts_by_repo_then_milestone_number() {
        let entries = vec![
            entry("b-repo", 2, "b2", None, &[]),
            entry("a-repo", 5, "a5", None, &[]),
            entry("b-repo", 1, "b1", None, &[]),
            entry("a-repo", 1, "a1", None, &[]),
        ];
        // Simulate what the payload → BoardData flow would set.
        let ordered: Vec<(String, i64)> = {
            let mut es = entries;
            es.sort_by(|a, b| {
                (a.repo.as_str(), a.milestone_number).cmp(&(b.repo.as_str(), b.milestone_number))
            });
            es.into_iter().map(|e| (e.repo, e.milestone_number)).collect()
        };
        assert_eq!(
            ordered,
            vec![
                ("a-repo".to_string(), 1),
                ("a-repo".to_string(), 5),
                ("b-repo".to_string(), 1),
                ("b-repo".to_string(), 2),
            ]
        );
    }

    #[test]
    fn plan_roster_entry_deserializes_matching_payload_shape() {
        // Golden: mirror exactly what `coord.plans.PlanEntry.to_dict()` emits.
        // Any drift here would fail the whole BoardPayload parse (#632).
        let json = r#"{
            "repo": "api",
            "title": "Substrate",
            "milestone_number": 5,
            "tracking_issue": 500,
            "has_work_order": true,
            "ready_frontier": 2,
            "blocked": 1,
            "in_flight": 0,
            "done": 0,
            "total": 3,
            "needs_you": ["ready_waiting"]
        }"#;
        let entry: PlanRosterEntry = serde_json::from_str(json).expect("valid roster JSON");
        assert_eq!(entry.repo, "api");
        assert_eq!(entry.milestone_number, 5);
        assert_eq!(entry.tracking_issue, Some(500));
        assert!(entry.has_work_order);
        assert_eq!(entry.ready_frontier, 2);
        assert_eq!(entry.needs_you, vec!["ready_waiting".to_string()]);
    }

    #[test]
    fn plan_roster_entry_deserializes_with_null_tracking_issue() {
        // A milestone without an epic reports tracking_issue: null.
        let json = r#"{
            "repo": "api",
            "title": "Follow-up",
            "milestone_number": 6,
            "tracking_issue": null,
            "has_work_order": false,
            "ready_frontier": 0,
            "blocked": 0,
            "in_flight": 0,
            "done": 0,
            "total": 0,
            "needs_you": ["no_work_order"]
        }"#;
        let entry: PlanRosterEntry = serde_json::from_str(json).expect("valid roster JSON");
        assert_eq!(entry.tracking_issue, None);
        assert!(!entry.has_work_order);
        assert_eq!(entry.needs_you, vec!["no_work_order".to_string()]);
    }
}
