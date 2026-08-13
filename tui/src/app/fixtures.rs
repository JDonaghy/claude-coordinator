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

// #1087: re-exported so `coord_tui::fixtures::set_test_board_service` pairs
// with `MockBoardService` below for an external integration-test crate,
// exactly like the other names in this module. Goes through `super::data`
// (the defining module) rather than `super::set_test_board_service` — the
// latter only resolves via `app/mod.rs`'s private `use self::data::*;`,
// which itself isn't re-exportable (same reason `Assignment`/`BoardData`
// above are re-exported via `super::types::` and not bare `super::`).
pub use super::data::{set_test_board_service, TestBoardServiceGuard};

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
    // #1326: derive straight from the fixture's `data` — never touch the
    // real `~/.coord/workspace.json` from a test fixture (mirrors
    // `command_runner: CommandRunner::new_for_test()` below: no real I/O).
    let workspace = Workspace::derive_from_repos(&known_repos(&data));
    CoordApp {
        data,
        workspace,
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
        board_epic_expanded: std::collections::HashMap::new(),
        board_epic_row_keys: std::collections::HashMap::new(),
        pipeline_sidebar,
        pipeline_repo_names: Vec::new(),
        pipeline_state_section_names: Vec::new(),
        pipeline_search: SidebarFilter::default(),
        pipeline_lifecycle_expanded: std::collections::HashMap::new(),
        pipeline_milestone_expanded: std::collections::HashMap::new(),
        pipeline_epic_expanded: std::collections::HashMap::new(),
        pipeline_epic_row_keys: std::collections::HashMap::new(),
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
        quiet_paused_machines: std::collections::HashSet::new(),
        cordoned_machines: std::collections::HashSet::new(),
        quiet_hours_windows: std::collections::HashMap::new(),
        pending_paused_machines: None,
        pending_force_merge: None,
        pending_merge_all_ready: None,
        pending_context_menu: None,
        pending_clipboard_copy: None,
        context_menu_layout: std::cell::RefCell::new(Vec::new()),
        fleet_health_overlay_open: false,
        pending_drive_queue_after: None,
        queue_sel: 0,
        queue_scroll: 0,
        queue_sort: None,
        queue_table_layout: std::cell::RefCell::new(None),
        queue_detail_scroll: 0,
        last_queue_detail_cols: std::cell::Cell::new(120),
        last_queue_detail_visible_rows: std::cell::Cell::new(10),
        last_queue_detail_item_count: std::cell::Cell::new(0),
        queue_split_frac: 0.4,
        queue_split_drag: false,
        queue_separator_rect: std::cell::Cell::new(None),
        queue_vscroll_drag: false,
        queue_h_scroll: std::cell::Cell::new(0.0),
        queue_hscroll_drag: false,
        queue_detail_scrollbar: std::cell::RefCell::new(None),
        queue_detail_vscroll_drag: false,
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
        pending_quiet_hours: None,
        pending_diagnose_dialog: None,
        pending_diagnose_legacy_retry: None,
        pending_quit_confirm: false,
        quit_requested: false,
        file_issue_modal: None,
        file_issue_post_rx: None,
        artifact_cache: std::collections::HashMap::new(),
        artifact_fetch_rx: None,
        findings_detail_cache: std::collections::HashMap::new(),
        findings_fetch_rx: None,
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
        // #1398
        drive_sessions: Vec::new(),
        pending_drive_sessions: None,
        pending_kill_drive: None,
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
        // #1124: same registration `CoordApp::new()` does — fixtures must
        // not skip it, or a test driving the Plans `?`/`/` surfaces would
        // find an empty registry regardless of what it exercises.
        help_registry: {
            let mut registry = HelpRegistry::new();
            registry.register("panel:plans", CoordApp::plans_view_help());
            registry
        },
        help_overlay: HelpOverlayController::new(),
        command_palette: None,
        // #975
        plans_sel: 0,
        // #1001
        plans_expanded_repos: std::collections::HashSet::new(),
        // #1122
        plans_detail_open: false,
        // #1122 fix-iteration-1
        plans_detail_scroll: 0,
        plans_detail_sel: 0,
        plans_detail_target: None,
        // #1121
        plans_tree_expanded: std::collections::HashMap::new(),
        plans_tree_selected: None,
        plans_tree_scroll: 0,
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
        // #1094: no column-width overrides / active resize drag / cached
        // layout in test helpers by default.
        audit_column_overrides: vec![None; 5],
        audit_table_layout: std::cell::RefCell::new(None),
        audit_resize_col: None,
        audit_scroll: 0,
        audit_h_scroll: 0.0,
        audit_scrollbar_drag: None,
        // #1741: Reports panel — nothing seeded by default; use
        // `make_app_with_reports` to pre-populate the catalogue (and
        // optionally a completed run) with no daemon and no fetch thread.
        reports_catalogue: None,
        reports_catalogue_rx: None,
        reports_catalogue_fetched: false,
        reports_params: std::collections::HashMap::new(),
        reports_expanded: std::collections::HashSet::new(),
        reports_touched: std::collections::HashSet::new(),
        reports_running: None,
        reports_run_rx: None,
        reports_result: None,
        reports_error: None,
        reports_no_service: false,
        reports_sel: 0,
        reports_field_sel: 0,
        reports_text_editing: false,
        reports_result_scroll: 0,
        reports_layout: std::cell::RefCell::new(MsvLayoutCache::default()),
        reports_sort: None,
        reports_table_layout: std::cell::RefCell::new(None),
        // #1853: no dragged column widths and no active resize drag by
        // default. `None` rather than `vec![None; N]` (Audit's shape) —
        // there is no N until a result lands, which is the whole reason
        // these are keyed.
        reports_column_overrides: None,
        reports_resize_col: None,
        reports_vscroll_drag: false,
        reports_pending_export: None,
        reports_export_status: None,
        reports_export_rx: None,
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

/// #1866 (Q-1) data-model seam: build a [`CoordApp`] whose `/board` payload
/// carries a drive queue, from a raw JSON array shaped exactly like
/// `/board`'s own `drive_queue` key (OpenAPI `BoardDriveQueueEntry`, i.e. a
/// raw `drive_queue` table dump — note `after_json`, not `after`).
///
/// Deliberately takes **JSON rather than a `Vec<BoardDriveQueueEntry>`**:
/// that type is `pub(crate)`, so a `pub fn` accepting it would not compile
/// (E0446), and going through the wire shape means a fixture that drifts
/// from the daemon's payload fails here instead of rendering a plausible
/// lie. Same posture as [`make_app_with_audit_json`].
///
/// The Queue panel carries no fetch of its own — `/board` already ships this
/// data and the existing poll refreshes it — so unlike
/// [`make_app_with_reports`] there is nothing here to mark as "already
/// fetched".
///
/// Malformed JSON is a silent no-op (the panel then renders its own empty
/// state) rather than a panic — assert on the resulting screen, not on this
/// function's return.
pub fn make_app_with_drive_queue(data: BoardData, drive_queue_json: &str) -> CoordApp {
    let mut app = make_test_app(data);
    if let Ok(entries) =
        serde_json::from_str::<Vec<super::types::BoardDriveQueueEntry>>(drive_queue_json)
    {
        app.data.drive_queue = entries;
    }
    app
}

/// #1741 data-model seam: build a [`CoordApp`] with the Reports panel's
/// catalogue (and optionally one completed run) pre-seeded from raw JSON
/// strings shaped exactly like `GET /report` / `GET /report/{id}` (#1742) —
/// no live daemon, no background fetch thread.
///
/// `catalogue_json` is the `{"reports": [...]}` envelope; `result_json` is a
/// `ReportResult` body, or `None` for "no run yet". Sections are seeded
/// expanded, matching what the real catalogue-fetch drain does
/// (`reports_seed_expansion` in `settings_ui.rs`), so a test renders the
/// same first frame the operator sees. `reports_catalogue_fetched` is set so
/// a driver test that ticks `run_periodic_work` never arms a real fetch on
/// top of the seeded data.
///
/// Malformed JSON is a silent no-op (the panel then renders its own
/// empty/loading state) rather than a panic — assert on the resulting
/// screen, not on this function's return.
pub fn make_app_with_reports(
    data: BoardData,
    catalogue_json: &str,
    result_json: Option<&str>,
) -> CoordApp {
    let mut app = make_test_app(data);
    if let Ok(cat) = serde_json::from_str::<super::types::ReportCatalogue>(catalogue_json) {
        app.reports_catalogue = Some(cat.reports);
        app.reports_catalogue_fetched = true;
        app.reports_seed_expansion();
    }
    if let Some(json) = result_json {
        if let Ok(result) = serde_json::from_str::<super::types::ReportResult>(json) {
            app.reports_result = Some(result);
        }
    }
    app
}

/// #1853 test seam: the sending half of a fake in-flight report run, handed
/// back by [`make_app_with_reports_awaiting_run`] so a test can decide *when*
/// the next result lands.
///
/// Exists because `TuiDriver::app_mut` yields the opaque shell adapter, not
/// the `CoordApp` inside it — a driver test therefore has no way to swap
/// `reports_result` mid-session by poking state. Delivering through the
/// app's own run channel is better than poking anyway: it is the exact path
/// a real completed run takes (`run_periodic_work` drains it on the next
/// event), so what the test exercises is the real report switch, not a
/// hand-assembled imitation of one.
pub struct PendingReportRun {
    tx: std::sync::mpsc::Sender<super::data::ReportRunOutcome>,
}

impl PendingReportRun {
    /// Deliver `result_json` as the run's outcome. It is picked up by the
    /// next `run_periodic_work` tick — i.e. by the next event the driver
    /// dispatches while the Reports panel is the active view.
    ///
    /// Returns `false` if `result_json` doesn't parse or the app has been
    /// dropped, so a typo in a test fixture fails at the `assert!` rather
    /// than as a mystifying "the table never changed".
    pub fn deliver(&self, result_json: &str) -> bool {
        match serde_json::from_str::<super::types::ReportResult>(result_json) {
            Ok(result) => self
                .tx
                .send(super::data::ReportRunOutcome::Result(Box::new(result)))
                .is_ok(),
            Err(_) => false,
        }
    }
}

/// #1853 data-model seam: [`make_app_with_reports`] plus an *open run
/// channel*, so a driver test can replace the report on screen part-way
/// through a session — the event a column-width override must not survive.
///
/// The app looks exactly as it does with a run in flight, minus the thread:
/// `reports_run_rx` is live and empty, and stays that way until the returned
/// [`PendingReportRun::deliver`] is called.
pub fn make_app_with_reports_awaiting_run(
    data: BoardData,
    catalogue_json: &str,
    result_json: &str,
) -> (CoordApp, PendingReportRun) {
    let mut app = make_app_with_reports(data, catalogue_json, Some(result_json));
    let (tx, rx) = std::sync::mpsc::channel();
    app.reports_run_rx = Some(rx);
    (app, PendingReportRun { tx })
}

/// #1087: a minimal, in-process, `std`-only mock HTTP server for exercising
/// `spawn_audit_fetch` / `resolve_board_service`'s real network path
/// end-to-end (paired with [`super::set_test_board_service`]).
///
/// Binds to an OS-assigned free port on `127.0.0.1` and, for the lifetime of
/// the returned value, answers *every* accepted connection with the same
/// canned response — `200 OK` / `application/json` / the `body` passed to
/// [`MockBoardService::start`] — regardless of method or path. It loops
/// rather than serving a single request so an incidental extra connection
/// (e.g. from an unrelated Audit-panel test on another thread that also
/// nudges `spawn_audit_fetch`, harmless because of the thread-local scoping
/// described on `set_test_board_service`, but still a live TCP client that
/// might dial in) can never "steal" the response meant for the caller's own
/// request.
///
/// No request parsing beyond draining the socket — `spawn_audit_fetch`'s
/// `GET /audit[?since=...&category=...&type=...]` carries no body, so a
/// full HTTP parser isn't needed to answer it correctly.
pub struct MockBoardService {
    addr: std::net::SocketAddr,
    shutdown: std::sync::Arc<std::sync::atomic::AtomicBool>,
    handle: Option<std::thread::JoinHandle<()>>,
    /// Request line (e.g. `"GET /audit?since=123 HTTP/1.1"`) of every
    /// request handled so far, oldest first — lets a test assert the real
    /// path/query `spawn_audit_fetch` sent, not just the parsed response.
    requests: std::sync::Arc<std::sync::Mutex<Vec<String>>>,
}

impl MockBoardService {
    /// Start the server. `body` is served verbatim as the JSON response
    /// body for every request received while this value is alive.
    pub fn start(body: impl Into<String>) -> Self {
        let listener = std::net::TcpListener::bind("127.0.0.1:0")
            .expect("MockBoardService: failed to bind 127.0.0.1:0");
        let addr = listener
            .local_addr()
            .expect("MockBoardService: failed to read local_addr");
        // Non-blocking + a short poll interval so the accept loop notices
        // `shutdown` promptly instead of blocking in `accept()` forever.
        listener
            .set_nonblocking(true)
            .expect("MockBoardService: failed to set_nonblocking");
        let shutdown = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let requests = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let body = body.into();
        let handle = {
            let shutdown = shutdown.clone();
            let requests = requests.clone();
            std::thread::spawn(move || {
                while !shutdown.load(std::sync::atomic::Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((stream, _)) => Self::respond(stream, &body, &requests),
                        Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                            std::thread::sleep(std::time::Duration::from_millis(5));
                        }
                        // Any other accept error (e.g. the listener got torn
                        // down) — stop looping rather than spin.
                        Err(_) => break,
                    }
                }
            })
        };
        Self {
            addr,
            shutdown,
            handle: Some(handle),
            requests,
        }
    }

    /// `http://127.0.0.1:<port>` — pass to [`super::set_test_board_service`]
    /// so `resolve_board_service()` (and thus `spawn_audit_fetch`) points
    /// here for the duration of the returned guard.
    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    /// Request lines received so far, oldest first (e.g.
    /// `"GET /audit?since=123 HTTP/1.1"`). Poisoned-lock recovery falls back
    /// to an empty list rather than propagating a second panic over a
    /// first — a test asserting on an empty `requests()` after some other
    /// assertion already panicked would only obscure the real failure.
    pub fn requests(&self) -> Vec<String> {
        self.requests
            .lock()
            .map(|g| g.clone())
            .unwrap_or_default()
    }

    fn respond(
        mut stream: std::net::TcpStream,
        body: &str,
        requests: &std::sync::Mutex<Vec<String>>,
    ) {
        use std::io::{BufRead, BufReader, Write};
        let _ = stream.set_read_timeout(Some(std::time::Duration::from_millis(500)));
        // Only the request line matters for the mock's purposes (method +
        // path + query); headers/body (if any) are left undrained on the
        // socket, which is fine since we respond and close immediately.
        let mut reader = BufReader::new(stream.try_clone().expect("clone stream for read"));
        let mut request_line = String::new();
        if reader.read_line(&mut request_line).is_ok() {
            let trimmed = request_line.trim_end().to_string();
            if !trimmed.is_empty() {
                if let Ok(mut log) = requests.lock() {
                    log.push(trimmed);
                }
            }
        }
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body,
        );
        let _ = stream.write_all(response.as_bytes());
        let _ = stream.flush();
    }
}

impl Drop for MockBoardService {
    fn drop(&mut self) {
        self.shutdown.store(true, std::sync::atomic::Ordering::Relaxed);
        // Wake the accept loop immediately (rather than waiting up to the
        // 5ms poll interval) by dialing in once ourselves — keeps teardown
        // prompt and deterministic instead of racing the sleep.
        let _ = std::net::TcpStream::connect(self.addr);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
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
        review_findings_truncated: false,
        review_findings_len: None,
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
        for_issue_number: None,
        driven_by: None,
    }
}
