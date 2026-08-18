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

    /// #2374: fleet-level escalations — sentinel rows `coord drive`/`coord
    /// release propagate` write with a `"("`-prefixed pseudo-repo
    /// (`coord.drive_queue.QUEUE_ALERT_REPO` = `"(drive-queue)"`,
    /// `coord.commands.release.DRAIN_ALERT_REPO` = `"(release-cordon)"`) and
    /// `issue_number=0` instead of a real GitHub issue — there is no
    /// `PipelineIssue` row these can ever match (`escalation_badge_span`
    /// above is keyed strictly to `(repo, issue_number)` against a real
    /// Pipeline row), so before this they rendered only if an operator
    /// happened to open Reports → `decisions`. Filtered straight out of
    /// `self.data.escalations` — the same wire feed `escalation_for` reads —
    /// rather than a second fetch, since `coord/reports.py`'s
    /// `fold_decisions` folds these exact rows from the same
    /// `list_drive_escalations` table with no additional derivation for the
    /// `source: "escalation"` branch (options are always
    /// Recommended/Inspect — see that function).
    pub(crate) fn fleet_escalations(&self) -> Vec<&EscalationEntry> {
        self.data
            .escalations
            .iter()
            .filter(|e| e.repo_name.starts_with('('))
            .collect()
    }

    /// The always-visible status-bar strip for fleet-level escalations
    /// (#2374) — same tier of always-on treatment `fleet_health.rs`'s
    /// indicator gets, so a cordon/drive-queue alert is visible from the
    /// TUI's default Pipeline view without a Reports-panel detour. Unlike
    /// the fleet-health segment, this one is `None` (not "0 escalations")
    /// when the fleet is clean — mirrors the `plans_attn`/`audit_recent`
    /// badges in `mod.rs`'s `status_bar`, which only appear when there's
    /// something to say.
    pub(crate) fn fleet_escalation_status_bar_segment(&self) -> Option<StatusBarSegment> {
        let entries = self.fleet_escalations();
        let first = entries.first()?;
        let headline = first.reason.lines().next().unwrap_or(first.reason.as_str());
        let text = if entries.len() == 1 {
            format!(" ⚠ FLEET ESCALATION: {} ", trunc(headline, 90))
        } else {
            format!(
                " ⚠ FLEET ESCALATIONS ({}): {} ",
                entries.len(),
                trunc(headline, 70)
            )
        };
        // Same white-on-crit-red pair `FleetSeverity::Crit::colors()` uses —
        // a fleet escalation is exactly that severity, and the two
        // indicators should read as the same "this needs you now" language.
        Some(StatusBarSegment {
            text,
            fg: Color::rgb(255, 255, 255),
            bg: Color::rgb(150, 30, 30),
            bold: true,
            action_id: None,
        })
    }

    /// `"<repo>#<issue>"` — the payload riding in `decide-fleet-escalation:`/
    /// `dismiss-fleet-escalation:` action ids (`context_menu_items_for_
    /// fleet_escalations` below). Needed because the status bar is a single
    /// `ContextMenuTarget::FleetHealth` for the whole bar (no per-segment
    /// click dispatch anywhere in this codebase — see `fleet_health.rs`'s
    /// module doc comment), so an action reached from it can't recover
    /// which fleet escalation was meant from `target` the way a Pipeline
    /// row's menu recovers `(repo, issue)` from `ContextMenuTarget::
    /// PipelineRow` — same `drive-queue-add-on:<machine>` precedent
    /// `decide-escalation:<index>`'s own doc comment cites.
    fn fleet_escalation_key(repo: &str, issue: i64) -> String {
        format!("{repo}#{issue}")
    }

    /// Parses a `fleet_escalation_key` back into `(repo, issue)`. Splits on
    /// the LAST `#` — a coordinator.yml repo name can't itself contain `#`,
    /// so this is unambiguous even though the sentinel repo names
    /// (`"(drive-queue)"`) contain parens.
    fn parse_fleet_escalation_key(key: &str) -> Option<(String, u64)> {
        let (repo, issue) = key.rsplit_once('#')?;
        Some((repo.to_string(), issue.parse().ok()?))
    }

    /// Right-click-the-status-bar menu items for every open fleet
    /// escalation (#2374) — appended to `context_menu_items_for_fleet_
    /// health`'s union in `dialogs.rs`. Mirrors the Pipeline row's
    /// escalation block (`context_menu_items_for_pipeline_row` in
    /// `dialogs.rs`) item-for-item — informational reason header, "Run
    /// proposed fix" pull-right submenu (Recommended/Inspect, same two
    /// `decisions`-report options `fold_decisions` always folds an
    /// escalation-table row into), "Dismiss escalation" — just keyed by an
    /// action-id-encoded `(repo, issue)` instead of the click target, since
    /// there's no per-row target here.
    pub(crate) fn context_menu_items_for_fleet_escalations(&self) -> Vec<ContextMenuItem> {
        let mut items = Vec::new();
        for esc in self.fleet_escalations() {
            items.push(ContextMenuItem::separator());
            let mut reason_item = ContextMenuItem::action(
                "fleet-escalation-reason",
                &format!("{}: {}", esc.repo_name, trunc(&esc.reason, 50)),
            );
            reason_item.disabled = true;
            items.push(reason_item);
            let key = Self::fleet_escalation_key(&esc.repo_name, esc.issue_number);
            items.push(ContextMenuItem::parent(
                &format!("Run proposed fix: {}", trunc(&esc.proposed_command, 56)),
                vec![
                    ContextMenuItem::action(
                        &format!("decide-fleet-escalation:{key}:0"),
                        &format!("Recommended: {}", trunc(&esc.proposed_command, 40)),
                    ),
                    ContextMenuItem::action(
                        &format!("decide-fleet-escalation:{key}:1"),
                        "Inspect (view the full escalation record)",
                    ),
                ],
            ));
            items.push(ContextMenuItem::action(
                &format!("dismiss-fleet-escalation:{key}"),
                "Dismiss escalation",
            ));
        }
        items
    }

    /// Shared tail of `dispatch_context_menu_action`'s `decide-fleet-
    /// escalation:`/`dismiss-fleet-escalation:` arms (`dialogs.rs`) —
    /// recovers `(repo, issue)` from the action id and dispatches through
    /// the SAME `dispatch_decide_escalation`/`dispatch_dismiss_escalation`
    /// the Pipeline row's menu already uses (#2374 explicitly: "`coord
    /// decide` remains the single execution primitive either surface calls
    /// — no new execution code path").
    pub(crate) fn dispatch_decide_fleet_escalation(&mut self, key: &str, option_index: usize) {
        if let Some((repo, issue)) = Self::parse_fleet_escalation_key(key) {
            self.dispatch_decide_escalation(&repo, issue, option_index);
        }
    }

    pub(crate) fn dispatch_dismiss_fleet_escalation(&mut self, key: &str) {
        if let Some((repo, issue)) = Self::parse_fleet_escalation_key(key) {
            self.dispatch_dismiss_escalation(&repo, issue);
        }
    }

    /// The "Run proposed fix: `<cmd>`" pull-right submenu for an open
    /// escalation record — an escalation-table card always folds to TWO
    /// `decisions`-report options (`coord/reports.py`'s `fold_decisions`:
    /// "Recommended" = this row's `proposed_command`, "Inspect" = `coord
    /// escalate list --repo <repo>`), so this is always a submenu, never a
    /// flat action, and each child calls `coord decide <repo> <issue>
    /// <index>` (via `dispatch_decide_escalation`, dispatched through the
    /// `decide-escalation:<index>` action-id convention) instead of `coord
    /// escalate run` directly.
    ///
    /// #2375: pulled out of `dialogs.rs`'s Pipeline-row menu builder so the
    /// drive-queue Queue-panel row menu (`drive_queue.rs`) can offer the
    /// IDENTICAL submenu for the same (repo, issue) — same recommended
    /// command, same child ordering — without a second hand-rolled copy of
    /// this `ContextMenuItem::parent(...)` construction to drift out of
    /// sync with the original.
    pub(crate) fn run_proposed_fix_menu_item(esc: &EscalationEntry) -> ContextMenuItem {
        ContextMenuItem::parent(
            &format!("Run proposed fix: {}", trunc(&esc.proposed_command, 56)),
            vec![
                ContextMenuItem::action(
                    "decide-escalation:0",
                    &format!("Recommended: {}", trunc(&esc.proposed_command, 40)),
                ),
                ContextMenuItem::action(
                    "decide-escalation:1",
                    "Inspect (view the full escalation record)",
                ),
            ],
        )
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

    // ── fleet_escalations / fleet_escalation_status_bar_segment (#2374) ─────

    #[test]
    fn fleet_escalations_filters_out_issue_scoped_rows() {
        let app = make_test_app(BoardData {
            escalations: vec![escalation("(drive-queue)", 0), escalation("myrepo", 42)],
            ..BoardData::default()
        });
        let fleet = app.fleet_escalations();
        assert_eq!(fleet.len(), 1);
        assert_eq!(fleet[0].repo_name, "(drive-queue)");
    }

    #[test]
    fn fleet_escalations_empty_when_only_issue_scoped() {
        let app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        assert!(app.fleet_escalations().is_empty());
    }

    #[test]
    fn fleet_escalation_status_bar_segment_none_when_no_fleet_rows() {
        // Regression guard for the existing per-issue badge path: an
        // issue-scoped escalation alone must never light up the fleet strip.
        let app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        assert!(app.fleet_escalation_status_bar_segment().is_none());
    }

    #[test]
    fn fleet_escalation_status_bar_segment_none_when_no_escalations_at_all() {
        let app = make_test_app(BoardData::default());
        assert!(app.fleet_escalation_status_bar_segment().is_none());
    }

    #[test]
    fn fleet_escalation_status_bar_segment_some_when_fleet_row_present() {
        let app = make_test_app(BoardData {
            escalations: vec![escalation("(drive-queue)", 0)],
            ..BoardData::default()
        });
        let seg = app
            .fleet_escalation_status_bar_segment()
            .expect("a fleet-scoped escalation must produce a status-bar segment");
        assert!(
            seg.text.contains("FLEET ESCALATION"),
            "segment text must name itself a fleet escalation: {:?}",
            seg.text
        );
        assert!(
            seg.text.contains("merge_status=NEEDS_ATTENTION"),
            "segment text must surface the escalation's own reason: {:?}",
            seg.text
        );
    }

    #[test]
    fn fleet_escalation_status_bar_segment_counts_multiple_fleet_rows() {
        let app = make_test_app(BoardData {
            escalations: vec![
                escalation("(drive-queue)", 0),
                escalation("(release-cordon)", 0),
            ],
            ..BoardData::default()
        });
        let seg = app.fleet_escalation_status_bar_segment().unwrap();
        assert!(
            seg.text.contains("(2)"),
            "two open fleet escalations must be counted in the segment: {:?}",
            seg.text
        );
    }

    // ── context_menu_items_for_fleet_escalations (#2374) ────────────────────

    #[test]
    fn context_menu_items_for_fleet_escalations_empty_when_none_open() {
        let app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        assert!(app.context_menu_items_for_fleet_escalations().is_empty());
    }

    #[test]
    fn context_menu_items_for_fleet_escalations_offers_run_and_dismiss() {
        let app = make_test_app(BoardData {
            escalations: vec![escalation("(drive-queue)", 0)],
            ..BoardData::default()
        });
        let items = app.context_menu_items_for_fleet_escalations();
        let parent = items
            .iter()
            .find(|i| i.label.starts_with("Run proposed fix"))
            .expect("'Run proposed fix' item not found");
        let children = parent.submenu.as_ref().expect("must be a pull-right submenu");
        assert_eq!(
            children.iter().map(|c| c.action_id.as_deref()).collect::<Vec<_>>(),
            vec![
                Some("decide-fleet-escalation:(drive-queue)#0:0"),
                Some("decide-fleet-escalation:(drive-queue)#0:1"),
            ],
            "each submenu entry must carry the (repo, issue) key since there's \
             no per-row click target for the status bar"
        );
        assert!(
            items
                .iter()
                .any(|i| i.action_id.as_deref() == Some("dismiss-fleet-escalation:(drive-queue)#0")),
            "must offer to dismiss the fleet escalation"
        );
    }

    // ── dispatch_decide_fleet_escalation / dispatch_dismiss_fleet_escalation ─

    #[test]
    fn dispatch_decide_fleet_escalation_spawns_coord_decide_with_the_sentinel_repo() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("(drive-queue)", 0)],
            ..BoardData::default()
        });
        app.dispatch_decide_fleet_escalation("(drive-queue)#0", 0);
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "decide".to_string(),
                "(drive-queue)".to_string(),
                "0".to_string(),
                "0".to_string(),
            ]],
            "must dispatch `coord decide` through the exact same primitive the \
             Pipeline row's menu uses — no new execution path"
        );
    }

    #[test]
    fn dispatch_dismiss_fleet_escalation_spawns_and_removes_optimistically() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("(release-cordon)", 0)],
            ..BoardData::default()
        });
        app.dispatch_dismiss_fleet_escalation("(release-cordon)#0");
        assert_eq!(
            app.command_runner.spawned_calls,
            vec![vec![
                "escalate".to_string(),
                "dismiss".to_string(),
                "(release-cordon)".to_string(),
                "0".to_string(),
            ]],
        );
        assert!(app.fleet_escalations().is_empty());
    }

    #[test]
    fn dispatch_decide_fleet_escalation_ignores_a_malformed_key() {
        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("(drive-queue)", 0)],
            ..BoardData::default()
        });
        app.dispatch_decide_fleet_escalation("no-hash-in-this-key", 0);
        assert!(
            app.command_runner.spawned_calls.is_empty(),
            "a key with no '#' can't be parsed into (repo, issue) — must be a no-op, not a panic"
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

    // ── TuiDriver black-box: fleet-level escalation strip (#2374) ───────────

    /// The issue's own acceptance bar: "A fleet-scoped `decisions` row ...
    /// renders somewhere visible in the TUI's default/Pipeline view without
    /// the operator navigating to Reports." Seeds ONE fleet-scoped
    /// escalation (a `"(drive-queue)"` sentinel row, `issue_number=0`) and
    /// ZERO issue-scoped ones — there is no `PipelineIssue` at all here, so
    /// the only way this can render is the new always-visible status-bar
    /// strip, not `escalation_badge_span`.
    #[test]
    fn tuidriver_fleet_scoped_escalation_shows_strip_with_no_issue_scoped_rows() {
        use quadraui::tui::testing::driver_with_shell;

        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("(drive-queue)", 0)],
            ..BoardData::default()
        });
        app.active_view = SidebarView::Pipeline;

        let driver = driver_with_shell(app, CoordApp::shell_config(), 200, 40);
        let screen = driver.screen();
        assert!(
            screen.contains("FLEET ESCALATION"),
            "a fleet-scoped escalation with no matching Pipeline row must still \
             surface via the always-visible status-bar strip:\n{screen}"
        );
    }

    /// The acceptance bar's other half: "a normal per-issue Pipeline test
    /// asserts it does NOT [render the strip]" — no regression to the
    /// existing per-issue badge path. Mirrors `tuidriver_escalated_row_
    /// shows_badge_and_menu_offers_run_and_dismiss`'s seed exactly, but
    /// checks the ABSENCE of the fleet strip instead of the row badge.
    #[test]
    fn tuidriver_per_issue_escalation_does_not_show_fleet_strip() {
        use quadraui::tui::testing::driver_with_shell;

        let mut app = make_test_app(BoardData {
            escalations: vec![escalation("myrepo", 42)],
            ..BoardData::default()
        });
        app.pipeline_issues = vec![pipeline_issue(42, Some("myrepo"))];
        app.pipeline_sel = Some(0);
        app.active_view = SidebarView::Pipeline;
        app.rebuild_pipeline_sidebar(None);

        let driver = driver_with_shell(app, CoordApp::shell_config(), 200, 40);
        let screen = driver.screen();
        assert!(
            !screen.contains("FLEET ESCALATION"),
            "a per-issue escalation must not light up the fleet-level strip:\n{screen}"
        );
    }
}
