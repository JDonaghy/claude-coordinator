//! Test-support fixtures: build a [`CoordApp`] from in-memory `BoardData`,
//! no live daemon required.
//!
//! Always compiled for in-crate `#[cfg(test)]` unit/TuiDriver tests (the
//! in-crate tests in `app/tests.rs` pull these in via `use super::fixtures::*;`).
//! Also compiled — and made reachable outside the crate — when the
//! `test-support` feature is enabled, so an external integration-test crate
//! (`tui/tests/acceptance.rs`) can build a `CoordApp` exactly like the
//! in-crate tests do (#1042, oracle-loop Gate-A prerequisite: docs/ORACLE_LOOP.md).
//!
//! The feature stays off for a normal `cargo build`/`cargo test`, so none of
//! this is part of the crate's default public surface.

use super::*;

// Re-exported so `use coord_tui::fixtures::{make_test_app, BoardData};` is
// enough — callers don't also need `coord_tui::app::types::BoardData`
// (which isn't reachable outside the crate; `app` and `app::types` stay
// private/`pub(crate)`, only these specific names are re-exported).
pub use super::types::{Assignment, BoardData};

/// Build a bare [`CoordApp`] from the given [`BoardData`] — no real `coord`
/// subprocess is ever spawned (`CommandRunner::new_for_test()`), no live
/// daemon, no I/O. This is the seam every other fixture in this module (and
/// every in-crate `#[cfg(test)]` test) builds on.
pub fn make_test_app(data: BoardData) -> CoordApp {
    let mut sidebar = SidebarSystem::new(Vec::new());
    sidebar.set_navigation_mode(NavigationMode::Selection);
    sidebar.set_allow_collapse(true);
    let mut pipeline_sidebar = SidebarSystem::new(Vec::new());
    pipeline_sidebar.set_navigation_mode(NavigationMode::Selection);
    pipeline_sidebar.set_allow_collapse(true);
    let (inject_fallback_tx, inject_fallback_rx) = std::sync::mpsc::channel();
    CoordApp {
        data,
        active_view: SidebarView::default(),
        board_sidebar: sidebar,
        board_repo_names: Vec::new(),
        board_issues_cache: Vec::new(),
        has_proposals_section: false,
        machine_sel: 0,
        machine_scroll: 0,
        refreshed_at: Instant::now(),
        detail_scroll: 0,
        machine_detail_scroll: 0,
        // Use new_for_test() so no real `coord` subprocess is ever spawned
        // when tests exercise dispatch paths (merge-queue key-bindings,
        // etc.).  Calls are recorded in spawned_calls; a zero-exit success
        // is resolved on the channel immediately so poll() returns quickly.
        command_runner: crate::commands::CommandRunner::new_for_test(),
        last_notify: Instant::now(),
        issue_sync_last: None,
        board_search: SidebarFilter::default(),
        board_milestone_expanded: std::collections::HashMap::new(),
        pipeline_sidebar,
        pipeline_repo_names: Vec::new(),
        pipeline_state_section_names: Vec::new(),
        pipeline_search: SidebarFilter::default(),
        pipeline_lifecycle_expanded: std::collections::HashMap::new(),
        pipeline_milestone_expanded: std::collections::HashMap::new(),
        pipeline_issues: Vec::new(),
        pipeline_sel: None,
        pipeline_status: None,
        toasts: Vec::new(),
        next_toast_id: 0,
        watch_pool: std::collections::HashMap::new(),
        watch_focused: None,
        inject_opened_from_log_tab: false,
        inject_chat: None,
        inject_spinner_frame: 0,
        pipeline_detail_tab: PipelineDetailTab::default(),
        board_detail_tab: BoardDetailTab::default(),
        pipeline_detail_scroll: 0,
        pipeline_log_hscroll: 0,
        remote_log_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
        pending_data: None,
        fetch_error: None,
        pending_log_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
        pending_issue_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
        fetched_issues_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
        // #876: pending_comments_fetches and fetched_comments_cache removed.
        pending_purge: None,
        pending_test_fail: None,
        pending_report_fix: None,
        pending_plan_capture: None,
        pending_milestone_row_input: None,
        pending_close_plan: None,
        pending_new_milestone_chat: None,
        pending_refinement: None,
        pending_test_chat: None,
        pending_chat_resume: None,
        inject_fallback_tx,
        inject_fallback_rx,
        pending_refine_ready: None,
        refine_then_dispatch: false,
        chat_transcript_cache_key: None,
        chat_last_activity: None,
        chat_spinner_throttle: 0,
        paused_machines: read_paused_machines(),
        pending_force_merge: None,
        pending_merge_all_ready: None,
        pending_context_menu: None,
        context_menu_layout: std::cell::RefCell::new(Vec::new()),
        dialog_layout: std::cell::RefCell::new(None),
        pending_restart: None,
        machine_last_contact: std::collections::HashMap::new(),
        local_coord_version: None,
        last_main_visible_rows: std::cell::Cell::new(40),
        last_log_panel_cols: std::cell::Cell::new(120),
        last_issue_panel_cols: std::cell::Cell::new(120),
        purge_days: 7,
        sidebar_action_bar_hover: ToolbarHoverTracker::new(),
        panel_toolbar_hover: ToolbarHoverTracker::new(),
        pipeline_action_bar_hover: ToolbarHoverTracker::new(),
        pipeline_focused_stage: None,
        pipeline_stage_content_scroll: 0,
        settings: TuiSettings::default(),
        parsed_keybindings: parse_keybindings(&TuiSettings::default()),
        settings_form: std::cell::RefCell::new(FormController::new("settings".to_string())),
        settings_field_sel: 0,
        audio_prev_running: std::collections::HashSet::new(),
        test_build_jobs: std::collections::HashMap::new(),
        last_test_builds: std::collections::HashMap::new(),
        pending_pr_fetches: std::cell::RefCell::new(std::collections::HashMap::new()),
        fetched_prs_cache: std::cell::RefCell::new(std::collections::HashMap::new()),
        pipeline_ci_checks: std::collections::HashMap::new(),
        pipeline_ci_loader: std::collections::HashMap::new(),
        pipeline_dismissed: std::collections::HashSet::new(),
        pipeline_inflight_merges: std::collections::HashSet::new(),
        pending_refinement_notes_synth: None,
        refinement_notes_modal: None,
        refinement_notes_post_rx: None,
        pending_refinement_close_prompt: None,
        finalise_after_notes_post: false,
        pending_board_chat: None,
        pending_milestone_chat: None,
        pending_repo_picker: None,
        pending_machine_picker: None,
        pending_new_terminal_picker: None,
        pending_new_terminal: None,
        pending_diagnose_dialog: None,
        pending_diagnose_legacy_retry: None,
        pending_quit_confirm: false,
        quit_requested: false,
        file_issue_modal: None,
        file_issue_post_rx: None,
        artifact_cache: std::collections::HashMap::new(),
        artifact_fetch_rx: None,
        pending_artifact_pull: None,
        last_artifact_pulls: std::collections::HashMap::new(),
        artifact_pull_dialog: None,
        log_items_cache: std::cell::RefCell::new(None),
        redraw_pending: false,
        last_redraw_at: Instant::now(),
        test_plan_pending: std::collections::HashSet::new(),
        test_plan_staleness_checked_for: None,
        test_step_jobs: std::collections::HashMap::new(),
        test_step_results: std::collections::HashMap::new(),
        test_step_output: std::collections::HashMap::new(),
        // #424
        terminal_session: None,
        terminal_focused: false,
        terminal_pending_dims: std::cell::Cell::new(None),
        terminal_spawn_error: None,
        // #1029: no queued programmatic panel switch / Terminal
        // return-view bookmark on startup.
        pending_panel_switch: None,
        pending_switch_is_programmatic: false,
        terminal_return_view: None,
        // #440
        detail_terminal_sessions: std::collections::HashMap::new(),
        detail_terminal_spawn_errors: std::collections::HashMap::new(),
        detail_terminal_focused: false,
        ctrl_w_pending: false,
        focused_region: FocusedRegion::default(),
        detail_terminal_pending_dims: std::cell::Cell::new(None),
        // #454
        pty_pressed_buttons: 0,
        // #464
        terminal_host_sel_dragging: false,
        // #790
        terminal_copy_mode: false,
        // #207
        machine_metrics: std::collections::HashMap::new(),
        pending_metrics: Vec::new(),
        metrics_last_polled: Instant::now(),
        // #487
        live_tmux_sessions: Vec::new(),
        pending_remote_sessions: None,
        // #953
        fleet_terminals: Vec::new(),
        pending_remote_terminals: None,
        terminal_tree_expanded: std::collections::HashMap::new(),
        terminal_tree_selected: None,
        terminal_tree_scroll: 0,
        // #955
        fleet_terminal_sessions: std::collections::HashMap::new(),
        fleet_terminal_spawn_errors: std::collections::HashMap::new(),
        pending_kill_terminal: None,
        // #1032
        sessions_tree_expanded: std::collections::HashMap::new(),
        sessions_tree_selected: None,
        sessions_tree_scroll: 0,
        // #1033
        pending_kill_session: None,
        fix_briefing_preview: None,
        fix_briefing_rx: None,
        // Leg 2 (#517)
        armed_for_auto_review: std::collections::HashMap::new(),
        pending_auto_review: None,
        pending_stage_launch: None,
        // #685: per-issue test-mode policy choice dialog.
        pending_test_mode_choice: None,
        offered_smoke_for_headless_work: std::collections::HashSet::new(),
        // Leg 3 (#517)
        armed_for_verdict: std::collections::HashMap::new(),
        pending_rework: None,
        rework_bypass: false,
        // #541
        issue_finder: None,
        // Leg 3c / A3 (#517, #581)
        armed_for_test_verdict: std::collections::HashMap::new(),
        pending_test_fix: None,
        pending_merge: None,
        // #863
        pending_fix_cap_preflight: None,
        pending_fix_force_confirm: None,
        // #638
        kanban_model: BoardModel {
            id: WidgetId::new("kanban:coord"),
            columns: Vec::new(),
            selected_card_id: None,
            col_scroll_offset: 0,
        },
        kanban_layout: std::cell::RefCell::new(None),
        // #737
        merge_queue_sel: 0,
        merge_queue_scroll: 0,
        // #771
        milestone_dag_sel: 0,
        // #975
        plans_sel: 0,
        // #1001
        plans_expanded_repos: std::collections::HashSet::new(),
        // #1039: Audit panel — nothing seeded by default; use
        // `make_app_with_audit_json` to pre-populate `audit_page`.
        audit_page: None,
        audit_fetch_rx: None,
        audit_last_fetched: None,
        audit_sel: 0,
        audit_detail_open: false,
        audit_fetch_error: None,
        audit_no_service: false,
        // #1040: no filter applied by default in test helpers; individual
        // tests override these fields directly to exercise the filters.
        audit_time_range: AuditTimeRange::All,
        audit_category: AuditCategory::All,
        audit_type_filter: SidebarFilter::default(),
        // #217: use the default dark palette for test helpers.
        active_theme: crate::settings::Theme::Dark.to_quadraui_theme(),
        // #728: default 2h window for tests (can be overridden per test).
        done_window: DoneWindow::H2,
        // #816: no pending PTY-panic dialog in test helpers.
        pty_panic_dialog: None,
        // #1059: no pending Gate A dispatch-failure dialog in test helpers.
        gate_a_error_dialog: None,
    }
}

/// Build a [`CoordApp`] seeded with `assignments` and the board sidebar
/// already rebuilt from them (so selection/navigation tests can drive it
/// immediately).
pub fn make_app_with_assignments(assignments: Vec<Assignment>) -> CoordApp {
    let mut app = make_test_app(BoardData {
        assignments,
        ..BoardData::default()
    });
    app.rebuild_board_sidebar();
    app
}

/// #1039 data-model seam: build a [`CoordApp`] with the Audit panel's cache
/// (`audit_page`) pre-seeded from a raw JSON string shaped exactly like the
/// `GET /audit` response body (contract §6, `tests/acceptance/ms-33/
/// contract.md`) — no live daemon, no background fetch thread.
///
/// This is the seam a later JIT extension of the sealed acceptance suite
/// needs for the populated-list / entry-detail / count+badge assertions
/// that `tests/acceptance/ms-33/audit_1039.rs` deliberately deferred (its
/// TODO block names this exact helper shape). Malformed JSON is a silent
/// no-op (`audit_page` stays whatever `data` implied, i.e. `None`) rather
/// than a panic — callers that care should assert on the resulting screen,
/// not on this function's return.
pub fn make_app_with_audit_json(data: BoardData, audit_json: &str) -> CoordApp {
    let mut app = make_test_app(data);
    if let Ok(page) = serde_json::from_str::<super::types::AuditPage>(audit_json) {
        app.audit_page = Some(page);
    }
    app
}

/// #265 helper: build an app where issue #10 is closed (on GitHub) and has a
/// done assignment, so it lands in the Completed group.
pub fn make_app_with_one_completed_issue() -> CoordApp {
    let mut app = make_app_with_assignments(vec![make_assignment_typed(
        "done",
        10,
        "repo-a",
        Some("work"),
    )]);
    app.data.open_issues.push(OpenIssue {
        repo_name: "repo-a".to_string(),
        number: 10,
        title: "closed one".to_string(),
        body: String::new(),
        state: "closed".to_string(),
        labels: Vec::new(),
        milestone_number: None,
        milestone_title: None,
    });
    app.rebuild_board_sidebar();
    app
}

/// Build an [`Assignment`] with the handful of fields most tests care about
/// (`status`, `issue_number`, `repo`, `assignment_type`) and sensible
/// defaults for everything else.
pub fn make_assignment_typed(
    status: &str,
    issue: u64,
    repo: &str,
    atype: Option<&str>,
) -> Assignment {
    Assignment {
        id: format!("id-{}-{}", issue, status),
        repo: repo.to_string(),
        issue_number: issue,
        issue_title: format!("Issue {}", issue),
        machine: "testmachine".to_string(),
        status: status.to_string(),
        branch: None,
        model: None,
        dispatched_at: Some(1_000_000.0 + issue as f64),
        finished_at: None,
        exit_code: None,
        assignment_type: atype.map(|s| s.to_string()),
        test_state: None,
        review_verdict: None,
        review_of_assignment_id: None,
        cost_usd: None,
        smoke_tests: None,
        review_findings: None,
        test_plan: None,
        test_plan_branch_head: None,
        input_tokens: 0,
        output_tokens: 0,
        cache_creation_tokens: 0,
        cache_read_tokens: 0,
        is_interactive: false,
        failure_reason: None,
        review_iteration: 0,
        acceptance_state: None,
        acceptance_reason: None,
        acceptance_sha: None,
        acceptance_total: None,
        acceptance_passed: None,
        test_reason: None,
        review_state: None,
        pr_url: None,
        audit_goals_json: None,
        audit_bottom_line: None,
        audit_run_number: None,
    }
}
