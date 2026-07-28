//! Board-visible driver-escalation records (#1505).
//!
//! `coord drive`'s merge stage writes one of these the moment it hits a
//! status no amount of retrying can fix (NEEDS_ATTENTION / an unrecognised
//! status) instead of burning the merge-attempt budget on a retry that
//! cannot possibly land — see `coord/drive.py`'s `_escalate_merge`. This
//! module is the TUI's read + one-key-response half: badge the Pipeline row
//! (`escalation_badge_span`, same splice point as `epic_badge_span`/
//! `drive_badge_span`), offer Run/Dismiss on its right-click menu (built in
//! `dialogs.rs`'s `context_menu_items_for_pipeline_row`, dispatched from
//! `dispatch_context_menu_action`), and shell out to `coord escalate
//! run|dismiss` (registered in `tui/src/commands.rs`'s `BARE_GROUPS` since
//! `escalate` is a bare Click group like `context`/`milestone`).
//!
//! Reading is entirely server-derived (`self.data.escalations`, populated
//! from `/board`'s `escalations` field) — unlike `drive.rs`'s
//! `DriveSession`, there is no client-side liveness state to track here.
//!
//! **Import pattern:** `use super::*` is intentional — see `drive.rs` /
//! `sessions.rs` / `terminal.rs` / `fleet_terminals.rs` for the same
//! rationale.
#[allow(unused_imports)]
use super::*;

impl CoordApp {
    /// The open escalation record for (repo, issue), if any.
    ///
    /// `repo_name` on the wire is the coordinator.yml repo key — the same
    /// value `PipelineIssue::coord_repo` carries — so this matches
    /// directly, no github-slug translation needed.
    pub(crate) fn escalation_for(
        &self,
        repo: &str,
        issue_number: u64,
    ) -> Option<&EscalationEntry> {
        self.data
            .escalations
            .iter()
            .find(|e| e.repo_name == repo && e.issue_number == issue_number as i64)
    }

    /// Splice-point badge for the Pipeline row — same position
    /// `epic_badge_span`/`drive_badge_span` use (between `#N` and the
    /// title), so it can't be clipped by a long title or overwritten by the
    /// right-aligned stage badge.
    pub(crate) fn escalation_badge_span(&self, issue: &PipelineIssue) -> Option<StyledSpan> {
        let repo = issue.coord_repo.as_deref()?;
        self.escalation_for(repo, issue.number).map(|_| {
            StyledSpan::with_fg(" [stuck]".to_string(), Color::rgb(230, 90, 90))
        })
    }

    /// "Run it" — the human's explicit one-key response. #1505: "Do not
    /// auto-run the proposed command… the point is a one-key human
    /// decision, not autonomous force-merge" — this dispatch call, reached
    /// only by an operator clicking the menu item, IS that key. Shells out
    /// to `coord escalate run <repo> <issue>`, which re-reads the record
    /// from the board and runs its `proposed_command` (fire-and-forget via
    /// `CommandRunner`, the same mechanism `confirm_kill_drive`'s
    /// `stop-drive` uses — there is nothing here worth watching live the
    /// way a drive session is).
    pub(crate) fn dispatch_run_escalation(&mut self, repo: &str, issue: u64) {
        let issue_str = issue.to_string();
        self.command_runner
            .spawn_queued(&["escalate", "run", repo, &issue_str]);
        self.push_toast(
            "Escalation",
            &format!("running the proposed fix for {repo} #{issue}…"),
            ToastSeverity::Info,
        );
    }

    /// "Dismiss" — clears the record without acting on it. Optimistically
    /// removed from `self.data.escalations` too (mirrors
    /// `confirm_kill_drive`'s optimistic `drive_sessions` removal) so the
    /// row badge and menu update immediately rather than waiting on the
    /// next board poll.
    pub(crate) fn dispatch_dismiss_escalation(&mut self, repo: &str, issue: u64) {
        let issue_str = issue.to_string();
        self.command_runner
            .spawn_queued(&["escalate", "dismiss", repo, &issue_str]);
        self.data
            .escalations
            .retain(|e| !(e.repo_name == repo && e.issue_number == issue as i64));
        self.rebuild_pipeline_sidebar(None);
        self.push_toast(
            "Escalation",
            &format!("dismissed for {repo} #{issue}"),
            ToastSeverity::Info,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app::fixtures::make_test_app;

    fn pipeline_issue(number: u64, coord_repo: Option<&str>) -> PipelineIssue {
        PipelineIssue {
            number,
            title: format!("issue {number}"),
            body: String::new(),
            repo_slug: format!("acme/{}", coord_repo.unwrap_or("unmapped")),
            coord_repo: coord_repo.map(str::to_string),
            matched_labels: vec!["coord".to_string()],
            all_labels: vec!["coord".to_string()],
            is_closed: false,
        }
    }

    fn escalation(repo: &str, issue_number: u64) -> EscalationEntry {
        EscalationEntry {
            id: 1,
            repo_name: repo.to_string(),
            issue_number: issue_number as i64,
            stage: "merge".to_string(),
            assignment_id: Some("w1".to_string()),
            reason: "merge_status=NEEDS_ATTENTION — no number of retries changes this"
                .to_string(),
            gate_readings: "merge_status=NEEDS_ATTENTION | pr_url=https://github.com/acme/myrepo/pull/9"
                .to_string(),
            proposed_command: "gh pr merge 9 --rebase && coord reconcile-merges".to_string(),
            created_at: Some(1.0),
        }
    }

    // ── escalation_for ───────────────────────────────────────────────────────

    #[test]
    fn escalation_for_matches_repo_and_issue() {
        let app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        assert!(app.escalation_for("myrepo", 42).is_some());
        assert!(app.escalation_for("other-repo", 42).is_none(), "must not cross repos");
        assert!(app.escalation_for("myrepo", 7).is_none(), "must not cross issues");
    }

    #[test]
    fn escalation_for_none_when_no_escalations() {
        let app = make_test_app(BoardData::default());
        assert!(app.escalation_for("myrepo", 42).is_none());
    }

    // ── escalation_badge_span ────────────────────────────────────────────────

    #[test]
    fn escalation_badge_span_some_when_stuck() {
        let app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        let issue = pipeline_issue(42, Some("myrepo"));
        assert!(app.escalation_badge_span(&issue).is_some());
    }

    #[test]
    fn escalation_badge_span_none_when_not_escalated() {
        let app = make_test_app(BoardData::default());
        let issue = pipeline_issue(42, Some("myrepo"));
        assert!(app.escalation_badge_span(&issue).is_none());
    }

    #[test]
    fn escalation_badge_span_none_when_repo_unmapped() {
        let app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        let issue = pipeline_issue(42, None);
        assert!(
            app.escalation_badge_span(&issue).is_none(),
            "an issue with no coord_repo can never match a repo-scoped escalation"
        );
    }

    // ── dispatch_run_escalation / dispatch_dismiss_escalation ───────────────

    #[test]
    fn dispatch_run_escalation_spawns_the_cli_call() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        app.dispatch_run_escalation("myrepo", 42);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "escalate".to_string(),
                "run".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
            ]],
            "must dispatch `coord escalate run myrepo 42`; got {:?}",
            app.command_runner.spawned_calls,
        );
    }

    #[test]
    fn dispatch_dismiss_escalation_spawns_and_removes_optimistically() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        app.dispatch_dismiss_escalation("myrepo", 42);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "escalate".to_string(),
                "dismiss".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
            ]],
        );
        assert!(
            app.data.escalations.is_empty(),
            "dismissed escalation must be removed from data.escalations optimistically"
        );
    }

    #[test]
    fn dispatch_dismiss_escalation_only_removes_the_matching_issue() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42), escalation("myrepo", 7)],
            ..BoardData::default()
        });
        app.dispatch_dismiss_escalation("myrepo", 42);
        assert_eq!(app.data.escalations.len(), 1);
        assert_eq!(app.data.escalations[0].issue_number, 7);
    }

    // ── TuiDriver black-box: row badge + menu (#1505 acceptance) ────────────

    /// The acceptance bar this issue names: "a `TuiDriver` test over a
    /// seeded `BoardData` with an escalation record asserts the row renders
    /// the proposal and the menu entry appears." Drives a real right-click
    /// (mirrors `drive.rs`'s `tuidriver_drive_automated_menu_item_switches_
    /// to_terminal_and_shows_badge`) rather than calling
    /// `context_menu_items_for_pipeline_row` directly.
    #[test]
    fn tuidriver_escalated_row_shows_badge_and_menu_offers_run_and_dismiss() {
        use quadraui::tui::testing::driver_with_shell;

        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.rebuild_pipeline_sidebar(None);

        let mut driver = driver_with_shell(app, CoordApp::shell_config(), 140, 40);

        // Same "No milestone" bucket expansion drive.rs's TuiDriver test
        // needs — the lone issue has no milestone, so its row starts
        // collapsed under that header.
        let (label_x, label_y) = driver
            .find("No milestone")
            .unwrap_or_else(|| panic!("'No milestone' bucket header not found:\n{}", driver.screen()));
        driver.click((label_x - 2.0).max(0.0), label_y);

        let screen = driver.screen();
        assert!(
            screen.contains("[stuck]"),
            "an escalated Pipeline row must render the [stuck] badge on the \
             row itself, not just in the menu:\n{screen}"
        );

        let (x, y) = driver
            .find("#42")
            .unwrap_or_else(|| panic!("could not find Pipeline row '#42':\n{}", driver.screen()));
        driver.dispatch(UiEvent::MouseDown {
            widget: None,
            button: MouseButton::Right,
            position: Point::new(x, y),
            modifiers: Modifiers::default(),
        });

        let menu = driver.screen();
        assert!(
            menu.contains("Run proposed fix"),
            "right-click on a stuck row must offer to run the proposed fix:\n{menu}"
        );
        assert!(
            menu.contains("Dismiss escalation"),
            "right-click on a stuck row must offer to dismiss the escalation:\n{menu}"
        );

        let (dx, dy) = driver
            .find("Run proposed fix")
            .unwrap_or_else(|| panic!("'Run proposed fix' menu item not found:\n{menu}"));
        // Same fractional-anchor nudge drive.rs's TuiDriver test documents —
        // `find` returns the row centre, but the menu hit-tests one item-
        // height below where it visibly renders.
        driver.click(dx, dy - 0.1);

        let toast_screen = driver.screen();
        assert!(
            toast_screen.contains("running the proposed fix"),
            "clicking 'Run proposed fix' must toast that the fix is running:\n{toast_screen}"
        );
    }
}
