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
//! #2370: the right-click menu's "Run proposed fix" item is a pull-right
//! submenu whose children call `coord decide <repo> <issue> <index>`
//! (`dispatch_decide_escalation`) rather than only ever offering the single
//! recommended fix via `coord escalate run` — an escalation-table card
//! always folds to two `decisions`-report options (`coord/reports.py`'s
//! `fold_decisions`: "Recommended" = this row's `proposed_command`,
//! "Inspect" = `coord escalate list --repo <repo>`), so the operator can
//! now pick either, not just the recommended one. `escalate run`/`dismiss`
//! keep working unchanged — `dispatch_run_escalation` below is still the
//! direct-CLI-parity path, just no longer the menu's only option.
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

    /// "Decide" — generalizes `dispatch_run_escalation` to any option on
    /// the card's `decisions`-report shape, not just the recommended one
    /// (#2370). Shells out to `coord decide <repo> <issue> <option_index>`,
    /// which re-reads the card fresh from `coord.reports.find_decision` and
    /// runs `options[option_index].command_or_action` — the same
    /// `subprocess.run(command, shell=True)` primitive `coord escalate run`
    /// uses, so this is fire-and-forget the same way
    /// `dispatch_run_escalation` is; there is nothing here worth watching
    /// live.
    ///
    /// Post-run bookkeeping (dismiss-on-success vs. echo-and-stop) is the
    /// CLI's job, not this dispatch call's — `coord decide` already knows
    /// whether `option_index` is the card's recommended escalation option
    /// (see that command's docstring), and this menu never re-derives or
    /// caches that classification.
    pub(crate) fn dispatch_decide_escalation(
        &mut self,
        repo: &str,
        issue: u64,
        option_index: usize,
    ) {
        let issue_str = issue.to_string();
        let index_str = option_index.to_string();
        self.command_runner
            .spawn_queued(&["decide", repo, &issue_str, &index_str]);
        self.push_toast(
            "Escalation",
            &format!("running option {option_index} for {repo} #{issue}…"),
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

    // ── dispatch_decide_escalation (#2370) ───────────────────────────────────

    #[test]
    fn dispatch_decide_escalation_spawns_the_cli_call_with_the_chosen_index() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        app.dispatch_decide_escalation("myrepo", 42, 1);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "decide".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
                "1".to_string(),
            ]],
            "must dispatch `coord decide myrepo 42 1`; got {:?}",
            app.command_runner.spawned_calls,
        );
    }

    #[test]
    fn dispatch_decide_escalation_default_index_is_the_recommended_option() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        app.dispatch_decide_escalation("myrepo", 42, 0);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "decide".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
                "0".to_string(),
            ]],
            "index 0 is the escalation card's recommended option — same slot \
             `coord escalate run` executes, generalized via `coord decide`",
        );
    }

    // ── context menu: "Run proposed fix" submenu (#2370) ────────────────────

    #[test]
    fn escalated_row_menu_offers_run_proposed_fix_as_a_submenu() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        let items = app.context_menu_items_for_pipeline_row(
            Some(42),
            &crate::app::types::PipelineRowLifecycle::New,
            Some("myrepo"),
        );
        let parent = items
            .iter()
            .find(|i| i.label.starts_with("Run proposed fix"))
            .expect("'Run proposed fix' item not found in escalated row's menu");
        let children = parent
            .submenu
            .as_ref()
            .expect("'Run proposed fix' must be a pull-right submenu, not a flat action");
        assert_eq!(
            children.iter().map(|c| c.action_id.as_deref()).collect::<Vec<_>>(),
            vec![Some("decide-escalation:0"), Some("decide-escalation:1")],
            "each submenu entry must call `coord decide` with its own option index"
        );
    }

    #[test]
    fn dispatch_context_menu_action_decide_escalation_zero_calls_decide_with_index_zero() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        let target = crate::app::types::ContextMenuTarget::PipelineRow {
            issue_number: Some(42),
            repo_name: Some("myrepo".to_string()),
            lifecycle: crate::app::types::PipelineRowLifecycle::New,
        };
        app.dispatch_context_menu_action("decide-escalation:0", &target);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "decide".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
                "0".to_string(),
            ]],
        );
    }

    #[test]
    fn dispatch_context_menu_action_decide_escalation_one_calls_decide_with_index_one() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        let target = crate::app::types::ContextMenuTarget::PipelineRow {
            issue_number: Some(42),
            repo_name: Some("myrepo".to_string()),
            lifecycle: crate::app::types::PipelineRowLifecycle::New,
        };
        app.dispatch_context_menu_action("decide-escalation:1", &target);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "decide".to_string(),
                "myrepo".to_string(),
                "42".to_string(),
                "1".to_string(),
            ]],
            "picking the non-default 'Inspect' option must still run `coord decide` \
             with its own index, not silently fall back to index 0"
        );
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

        // #2370: "Run proposed fix" is now a pull-right submenu (a card
        // with more than one `decisions`-report option, per the design's
        // point 4) rather than a flat action — this click opens it, it
        // doesn't run anything yet.
        let submenu = driver.screen();
        assert!(
            submenu.contains("Recommended:"),
            "the submenu must offer the recommended option:\n{submenu}"
        );
        assert!(
            submenu.contains("Inspect"),
            "the submenu must offer to inspect the record as a second option:\n{submenu}"
        );

        let (rx, ry) = driver
            .find("Recommended:")
            .unwrap_or_else(|| panic!("'Recommended:' submenu item not found:\n{submenu}"));
        driver.click(rx, ry - 0.1);

        let toast_screen = driver.screen();
        assert!(
            toast_screen.contains("running option 0 for myrepo #42"),
            "clicking the submenu's 'Recommended' entry must dispatch `coord decide \
             myrepo 42 0` and toast that it's running:\n{toast_screen}"
        );
    }
}
