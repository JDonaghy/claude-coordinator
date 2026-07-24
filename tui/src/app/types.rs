//! App data-model types extracted from `app/mod.rs` (#743).
//!
//! DTO/enum structs and their pure impls — no I/O, no quadraui rendering.
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use quadraui::{Color, WidgetId};
use super::format::fmt_dur;


/// #349: One step in a generated smoke-test plan.  Parsed from the JSON stored
/// in `assignments.test_plan` when the DB row is loaded.
#[derive(Clone, Debug, serde::Deserialize)]
pub(crate) struct TestPlanStep {
    /// Step type: "pull", "run", or "verify".
    pub(crate) kind: String,
    /// Shell command for "pull" and "run" steps; absent for "verify".
    pub(crate) cmd: Option<String>,
    /// Human-readable label (mainly for "pull" steps).
    pub(crate) label: Option<String>,
    /// Observable assertion for "verify" steps.
    pub(crate) check: Option<String>,
}

/// #349: In-flight execution of a single test-plan step.  Background thread
/// runs the shell command and signals completion via the mpsc channel.
pub(crate) struct TestStepJob {
    /// (work_id, step_index) key — stored here for future diagnostics /
    /// logging; not read in the current implementation.
    #[allow(dead_code)]
    pub(crate) work_id: String,
    #[allow(dead_code)]
    pub(crate) step_idx: usize,
    /// Channel for receiving `(exit_code, captured_output)` once the subprocess
    /// finishes.  `captured_output` is the combined stdout + stderr of the
    /// command (newline-separated, bounded to 64 KiB to avoid unbounded growth).
    pub(crate) rx: std::sync::mpsc::Receiver<(i32, String)>,
}

/// The tabs shown in the Pipeline view detail panel.
///
/// #818: Redesigned tab set — Stages and Refinement removed; Pipeline renamed
/// to Overview.  A universal read-only stage strip is pinned at the top of
/// every non-Overview tab when a pipeline entry exists.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub(crate) enum PipelineDetailTab {
    /// Overview: horizontal stage boxes (click a box to expand that stage's
    /// detail inline), repo/labels/gates meta, and focused-stage content.
    /// Actions (Go / dispatch) are only available here.
    #[default]
    Overview,
    /// Full issue body text (scrollable with j/k).
    Issue,
    /// Live worker log — same content as the watch overlay but inline.
    Log,
    /// #558: session-history summary for the selected issue.  Lists every
    /// work/review/fix session (oldest → newest) with type, machine,
    /// status/verdict, and summary text.  Sourced from the in-memory board.
    Summary,
    /// #440: per-issue interactive shell for the detail view.  Each active
    /// issue gets its own `TerminalSession`; only the selected issue's
    /// terminal is visible while others keep running in the background.
    Terminal,
}

/// The tabs shown in the Board view detail panel.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub(crate) enum BoardDetailTab {
    /// Default: assignment summary, status, machine, etc.
    #[default]
    Board,
    /// Full issue body text + labels (scrollable with j/k or scrollwheel).
    /// Reuses `issue_body_list` so the rendering matches the Pipeline view.
    Issue,
    /// #316 Phase A: board-level chat (new-issue or refine-board).
    /// Shows an empty state with Refine / New Issue CTAs when no chat is
    /// open; shows the ChatController when a board chat is live.
    Chat,
    /// #675: per-issue interactive shell for the Board detail view.
    /// Mirrors `PipelineDetailTab::Terminal` — spawned by "Chat about issue"
    /// from the Board context menu.  The same `detail_terminal_sessions` map
    /// is used; the session key is `(repo_slug, issue_number)`.
    Terminal,
}

/// #782: which pane currently has keyboard focus.
///
/// `Sidebar` is the default (activity-bar + list panel).
/// `Main` is the primary content area (main panel / detail panel).
/// `Detail` is the secondary pane visible when a detail sub-pane or PTY
/// occupies the right/bottom portion of the content area.
///
/// Used by the Ctrl-W focus cycler; the visible indicator is rendered in the
/// status bar.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub(crate) enum FocusedRegion {
    #[default]
    Sidebar,
    Main,
    Detail,
}

impl FocusedRegion {
    /// Short label shown in the status-bar focus indicator.
    pub(crate) fn label(self) -> &'static str {
        match self {
            FocusedRegion::Sidebar => "Sidebar",
            FocusedRegion::Main => "Main",
            FocusedRegion::Detail => "Detail",
        }
    }
}

/// The selectable top-level views shown in the left sidebar.
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub(crate) enum SidebarView {
    #[default]
    Board,
    Machines,
    /// Pipeline panel: tracked-issue list + horizontal stage view per issue.
    Pipeline,
    /// Settings panel: category nav on the left, form controls on the right.
    Settings,
    /// #424: embedded terminal pane hosting a live PTY-backed shell session
    /// via `quadraui::terminal_engine::TerminalSession`.  Focus routing is
    /// controlled by `CoordApp::terminal_focused`: when true, keystrokes
    /// pass through to the PTY; when false, normal TUI chrome handles
    /// them. F12 toggles focus.
    Terminal,
    /// #638: Kanban view — three-column (Backlog / In Flight / Completed)
    /// rendered via `quadraui::Board`.
    Kanban,
    /// #737: Merge Queue panel — global view of all in-flight PR merges,
    /// grouped by milestone.  Key `7` switches to this view.
    MergeQueue,
    /// #771 (Phase 3 of #767): milestone work-order DAG/lane view — renders
    /// a milestone's `## Work order` block (parsed client-side from the
    /// already-synced tracking-issue body, see `app/milestone_dag.rs`) as
    /// cohort rows with done/in-flight/blocked/ready state per node, plus a
    /// "Dispatch milestone" action on the milestone header. Key `8`.
    MilestoneDag,
    /// #975: Plans panel — the first-class ActivityBar view onto the plan
    /// roster.  Elevates/subsumes the older `MilestoneDag` "Milestones" view:
    /// one row per milestone/epic with ready / blocked / in-flight / done
    /// counts, sourced from `BoardData::plan_roster` (server-computed by
    /// `coord/serve_app.py` via `coord.plans.aggregate_repo_plans`).  Read-only
    /// in this slice — health chips (#976), fast capture (#977), and the
    /// GOAL.md header (#978) come later.  Selecting a row opens that plan's
    /// tracking epic in the browser via `gh issue view --web`.
    Plans,
    /// #1032: Sessions panel — fleet-wide, machine → repo grouped tree of
    /// live `coord-<aid>` claude work sessions (`self.live_tmux_sessions`).
    /// Built on the same `TreeView` + flat-pixel-row click pattern as the
    /// #953 Terminal-view tree (`fleet_sessions.rs`), extended one level
    /// deeper (machine → repo → session leaf). Read-only nav/select in this
    /// slice — attach/kill/stop are a follow-up.
    Sessions,
    /// #1039: Audit panel — a scrollable, newest-first list of the audit
    /// trail (`/audit`, #1037) with an inline entry-detail view, modeled on
    /// the Plans panel. Filters (#1040) are a follow-up.
    Audit,
    /// #1116: Usage panel — per-issue (or per-repo) cost/token grid sourced
    /// from the already-loaded board assignments (no new daemon
    /// round-trip), with a scope selector (today/week/month/custom range),
    /// group-by, and a click-to-expand per-stage drill. See `app/usage.rs`.
    Usage,
}

impl SidebarView {
    pub(crate) fn label(self) -> &'static str {
        match self {
            SidebarView::Board => "Board",
            SidebarView::Machines => "Machines",
            SidebarView::Pipeline => "Pipeline",
            SidebarView::Settings => "Settings",
            SidebarView::Terminal => "Terminal",
            SidebarView::Kanban => "Kanban",
            SidebarView::MergeQueue => "Merge Queue",
            SidebarView::MilestoneDag => "Milestones",
            SidebarView::Plans => "Plans",
            SidebarView::Sessions => "Sessions",
            SidebarView::Audit => "Audit",
            SidebarView::Usage => "Usage",
        }
    }

    /// The ActivityBar `WidgetId` (see `CoordApp::shell_config`) this view
    /// corresponds to, or `None` when the view has no top-level ActivityBar
    /// entry of its own (`MilestoneDag` — reached only as a drill-down from
    /// Plans; see the `#975` note on the `Plans` variant above).
    ///
    /// This is the inverse of the `panel_id_str` match in
    /// `render.rs::on_shell_event`. `CoordApp::switch_active_view` (#1029
    /// bug A) uses it to keep quadraui's ActivityBar highlight + sidebar
    /// header in sync with a *programmatic* view switch — one not already
    /// driven by an ActivityBar click, which `on_shell_event` already
    /// handles correctly.
    pub(crate) fn panel_widget_id(self) -> Option<WidgetId> {
        match self {
            SidebarView::Board => Some(WidgetId::new("panel:board")),
            SidebarView::Machines => Some(WidgetId::new("panel:machines")),
            SidebarView::Pipeline => Some(WidgetId::new("panel:pipeline")),
            SidebarView::Settings => Some(WidgetId::new("panel:settings")),
            SidebarView::Terminal => Some(WidgetId::new("panel:terminal")),
            SidebarView::Kanban => Some(WidgetId::new("panel:kanban")),
            SidebarView::MergeQueue => Some(WidgetId::new("panel:mergequeue")),
            SidebarView::Plans => Some(WidgetId::new("panel:plans")),
            SidebarView::Sessions => Some(WidgetId::new("panel:sessions")),
            SidebarView::Audit => Some(WidgetId::new("panel:audit")),
            SidebarView::Usage => Some(WidgetId::new("panel:usage")),
            SidebarView::MilestoneDag => None,
        }
    }
}

#[derive(Clone, serde::Deserialize)]
// #1042: `pub`, not `pub(crate)` — it's a parameter/return type of the
// `test-support`-feature-gated fixtures in `app::fixtures` (e.g.
// `make_app_with_assignments(Vec<Assignment>)`), which must be at least as
// visible as those `pub fn`s (E0446) to be reachable from an external
// integration-test crate. Fields stay `pub(crate)`: nothing outside the
// crate constructs one field-by-field, only via `app::fixtures` helpers or
// `serde::Deserialize`.
pub struct Assignment {
    #[serde(rename = "assignment_id")]
    pub(crate) id: String,
    #[serde(rename = "repo_name")]
    pub(crate) repo: String,
    pub(crate) issue_number: u64,
    pub(crate) issue_title: String,
    #[serde(rename = "machine_name")]
    pub(crate) machine: String,
    pub(crate) status: String,
    #[serde(default)]
    pub(crate) branch: Option<String>,
    #[serde(default)]
    pub(crate) model: Option<String>,
    #[serde(default)]
    pub(crate) dispatched_at: Option<f64>,
    #[serde(default)]
    pub(crate) finished_at: Option<f64>,
    #[serde(default)]
    pub(crate) exit_code: Option<i32>,
    #[serde(rename = "type", default)]
    pub(crate) assignment_type: Option<String>,
    /// #200: human-driven Test gate verdict for type="work" assignments.
    /// None | "passed" | "failed" | "skipped".
    #[serde(default)]
    pub(crate) test_state: Option<String>,
    /// #253: parsed adversarial-review verdict for type="review" assignments.
    /// None | "approve" | "request-changes".  Drives the merge-gate hint
    /// swap so the user sees the block before pressing m.
    #[serde(default)]
    pub(crate) review_verdict: Option<String>,
    /// #253: links a review assignment back to the work assignment it
    /// reviews — needed to pair review_verdict with the merge entry.
    #[serde(default)]
    pub(crate) review_of_assignment_id: Option<String>,
    /// #208: worker cost captured from the final stream-json result event.
    /// `None` for in-flight workers and for pre-#208 rows.
    #[serde(default)]
    pub(crate) cost_usd: Option<f64>,
    /// #252: worker-emitted smoke-test list, parsed from the SMOKE_TESTS
    /// block in the worker's log.
    ///
    /// * `None`     — no block found (graceful degradation: TUI shows
    ///                "inspect the diff" placeholder).
    /// * `Some([])` — explicit "(none — change is internal)" form.
    /// * `Some(vec)` — bullets to render under the Test stage.
    ///
    /// #584: on the /board wire this is a real JSON array (already decoded),
    /// so plain serde handles it.
    #[serde(default)]
    pub(crate) smoke_tests: Option<Vec<String>>,
    /// #bounce: cached review findings (verdict + body), JSON-encoded
    /// in the DB column.  `None` for non-review assignments and for
    /// reviews completed before the cache landed.
    ///
    /// #584: intentionally kept as a raw JSON STRING on the /board wire, so
    /// plain serde deserialization works.
    #[serde(default)]
    pub(crate) review_findings: Option<String>,
    /// #1337: true when the daemon bounded `review_findings` on the /board
    /// wire (the collection carries a preview; the full body lives on
    /// `GET /assignment/{id}`).  Absent (→ false) on pre-#1337 daemons and
    /// on the local-SQLite path, both of which carry the full text.
    #[serde(default)]
    pub(crate) review_findings_truncated: bool,
    /// #1337: full stored length of `review_findings` when truncated —
    /// combined with the assignment id as the detail-fetch cache key, so a
    /// force-overwritten review (different length) re-fetches.
    #[serde(default)]
    pub(crate) review_findings_len: Option<i64>,
    /// #349 Phase B: AI-generated smoke-test plan for type="work" assignments.
    /// Parsed from the JSON blob in `assignments.test_plan`.  `None` = not
    /// yet generated (TUI will spawn `coord test-plan` to fill it in).
    ///
    /// #584: on the /board wire this is a decoded OBJECT `{"steps":[...]}`,
    /// not an array — see [`deserialize_test_plan`].
    #[serde(default, deserialize_with = "deserialize_test_plan")]
    pub(crate) test_plan: Option<Vec<TestPlanStep>>,
    /// #349 Phase B: git branch HEAD SHA at the time the cached test_plan was
    /// generated.  `None` when no plan exists or when it was generated without
    /// branch tracking.  Used to detect staleness (branch advanced →
    /// auto-refresh via `coord test-plan --refresh`).
    #[serde(default)]
    pub(crate) test_plan_branch_head: Option<String>,
    /// #546: token counts for automated (claude -p) assignments, parsed from
    /// the final stream-json result event alongside `cost_usd`.
    /// 0 for interactive (Claude Max / OAuth) sessions — those have no
    /// per-token billing and the TUI shows "Max" instead of a $ figure.
    #[serde(default)]
    pub(crate) input_tokens: i64,
    #[serde(default)]
    pub(crate) output_tokens: i64,
    #[serde(default)]
    #[allow(dead_code)] // populated from DB / SSE; not yet read in TUI render (#818 removed the Stages tab that displayed them)
    pub(crate) cache_creation_tokens: i64,
    #[serde(default)]
    #[allow(dead_code)] // see above
    pub(crate) cache_read_tokens: i64,
    /// #546: true when the assignment ran as a human-attended interactive session
    /// (Max / Pro subscription).  Set by `finalize_interactive_exit`; prevents
    /// misidentifying old automated rows (which also have cost_usd=NULL and zero
    /// token counts) as Max sessions.
    //
    // #628 hotfix: the daemon serializes this SQLite boolean as an int (0/1), so
    // a strict `bool` here fails the ENTIRE /board parse on `is_interactive:0`,
    // returning BoardData::default() and blanking every panel. Accept int-or-bool.
    #[serde(default, deserialize_with = "de_bool_from_int_or_bool")]
    pub(crate) is_interactive: bool,
    /// #618: short human-readable reason written immediately when an interactive
    /// session fails to launch (e.g. "branch already checked out at <path>").
    /// `None` for assignments that launched successfully.  Shown in the
    /// assignment detail panel so the TUI explains the red box without a log file.
    #[serde(default)]
    pub(crate) failure_reason: Option<String>,
    /// #803: fix-round counter — 0 on the original work assignment, N on the
    /// N-th fix.  Used to compute the next iteration's escalated model via
    /// [`fix_model_for_iteration`]: `next_iteration = review_iteration + 1`.
    #[serde(default)]
    pub(crate) review_iteration: i64,
    /// #932/#944: the Acceptance-gate verdict (oracle loop,
    /// docs/ORACLE_LOOP.md) for type="work" assignments, stamped by `coord
    /// acceptance record --issue N --sha <sha>` — the coordinator's
    /// external re-run of the sealed suite against the pushed SHA.
    /// None | "passed" | "failed". Reported and gated SEPARATELY from
    /// `test_state` — its own box, its own verdict.
    #[serde(default)]
    pub(crate) acceptance_state: Option<String>,
    /// Short failing-test summary when `acceptance_state == "failed"`.
    #[serde(default)]
    pub(crate) acceptance_reason: Option<String>,
    /// SHA the last `acceptance record` verdict was recorded against.
    #[serde(default)]
    pub(crate) acceptance_sha: Option<String>,
    /// #932: per-test counts from the same verdict, so the Acceptance box
    /// can read as partial progress ("3/7 acceptance green") rather than a
    /// bare pass/fail — a growing suite is expected to be sub-100% until
    /// the feature completes. `None` for rows predating this column.
    #[serde(default)]
    pub(crate) acceptance_total: Option<i64>,
    #[serde(default)]
    pub(crate) acceptance_passed: Option<i64>,
    /// #876: test failure reason entered by the operator via `coord test --fail`
    /// (written by `record_test_verdict`).  `None` for assignments without a
    /// failed test or for pre-#876 rows.
    #[serde(default)]
    pub(crate) test_reason: Option<String>,
    /// Internal review-state machine value: "pending" | "done" etc.
    /// `None` for non-review assignments and for pre-#876 rows.
    /// Deserialized from the board for future display; not yet read in
    /// production rendering paths.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) review_state: Option<String>,
    /// URL to the pull request for this work assignment (populated when the
    /// worker pushes a branch and the coordinator records it).
    /// `None` when no PR URL is known.
    #[serde(default)]
    pub(crate) pr_url: Option<String>,
    /// #886 Phase 2: Milestone Outcome Audit structured verdict, set only on
    /// `type="audit"` assignments (see #885's `--audit-of`). Kept as a raw
    /// JSON STRING on the wire, same convention as `review_findings` above.
    /// The Plans panel renders the *aggregated* latest-run-per-milestone view
    /// via `PlanRosterEntry`'s `outcome_*` fields, not this raw column — these
    /// are present for a possible future per-assignment audit detail view.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) audit_goals_json: Option<String>,
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) audit_bottom_line: Option<String>,
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) audit_run_number: Option<i64>,
    /// #1084: for `type="test-author"` JIT-mode assignments, the specific
    /// work-order member issue this dispatch is extending the acceptance
    /// suite for (`coord.test_author.dispatch_test_author`'s `issue_number`
    /// argument) — NOT the same as `issue_number` above, which test-author
    /// always sets to the milestone's *tracking* issue (every JIT dispatch
    /// for a milestone shares one branch/PR). `None` for milestone-mode
    /// (Gate A) authoring, every other assignment type, and rows predating
    /// this column. Used by the per-issue Acceptance-Authoring mini-
    /// pipeline to attribute a shared-branch assignment row back to the
    /// right member issue's row.
    #[serde(default)]
    pub(crate) for_issue_number: Option<u64>,
}

/// Deserialize a boolean the daemon may send as a SQLite-style integer (0/1)
/// instead of a JSON bool. Accepts bool, int, or null (→ false). One mistyped
/// boolean would otherwise fail the whole `BoardPayload` parse and blank the
/// board (the #546 `is_interactive` regression).
pub(crate) fn de_bool_from_int_or_bool<'de, D>(d: D) -> Result<bool, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(serde::Deserialize)]
    #[serde(untagged)]
    enum BoolOrInt {
        Bool(bool),
        Int(i64),
    }
    Ok(match <Option<BoolOrInt> as serde::Deserialize>::deserialize(d)? {
        Some(BoolOrInt::Bool(b)) => b,
        Some(BoolOrInt::Int(n)) => n != 0,
        None => false,
    })
}

impl Assignment {
    /// Return the colour for this assignment's status badge, drawn from the
    /// active theme palette.
    ///
    /// Mapping (semantic → `quadraui::Theme` field):
    /// - `"running"` → `theme.badge_running`  (active worker — green on dark)
    /// - `"done"`    → `theme.muted_fg`        (completed, no longer active)
    /// - `"failed"`  → `theme.badge_blocked`   (hard failure — red)
    /// - other       → `theme.warning_fg`      (pending / unknown — yellow)
    pub(crate) fn status_color(&self, theme: &quadraui::Theme) -> Color {
        match self.status.as_str() {
            "running" => theme.badge_running,
            "done" => theme.muted_fg,
            "failed" => theme.badge_blocked,
            _ => theme.warning_fg,
        }
    }

    pub(crate) fn status_label(&self) -> &str {
        match self.status.as_str() {
            "running" => "RUN ",
            "done" => "DONE",
            "failed" => "FAIL",
            _ => "PEND",
        }
    }

    pub(crate) fn age_str(&self) -> String {
        match self.dispatched_at {
            None => "-".to_string(),
            Some(ts) => {
                let now = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs_f64();
                fmt_dur((now - ts).max(0.0) as u64)
            }
        }
    }
}

#[derive(Clone)]
pub(crate) struct Machine {
    pub(crate) name: String,
    /// Tailscale FQDN (the `host` column in the machines table).
    pub(crate) host: String,
    pub(crate) reachable: bool,
    pub(crate) active_count: usize,
    pub(crate) repos: Vec<String>,
    /// Agent version string from `/health.version`; `None` when unreachable.
    pub(crate) version: Option<String>,
    /// Total git-worktree disk usage in bytes, from `/health.worktree_bytes`.
    pub(crate) worktree_bytes: u64,
}

/// #584: a machine row as it arrives on the `coord serve` /board wire.
///
/// `Machine` itself carries probe-only fields (reachable / active_count /
/// version / worktree_bytes) that never appear in the payload, so we
/// deserialize into this minimal shape and let `assemble_board_data` run the
/// reachability + health probes exactly like the SQLite path does.
#[derive(serde::Deserialize)]
pub(crate) struct RawMachine {
    pub(crate) name: String,
    pub(crate) host: String,
    #[serde(default)]
    pub(crate) repos: Vec<String>,
}

/// #778: one entry in the "approved but not yet queued" staging section.
///
/// Mirrors `coord.merge_queue.StagingItem` on the Python side.  Populated
/// by `staging_items()` server-side; the TUI receives it via the `/board`
/// JSON payload and also computes it locally in `load_data`.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct StagingEntry {
    /// Wire field — present for future right-click / force-enqueue actions.
    #[allow(dead_code)]
    pub(crate) assignment_id: String,
    /// Wire field — present for future per-repo grouping.
    #[allow(dead_code)]
    pub(crate) repo_name: String,
    pub(crate) issue_number: i64,
    pub(crate) issue_title: String,
    /// Wire field — present for future branch-detail display.
    #[allow(dead_code)]
    pub(crate) branch: String,
    /// `"ready"` — all gates pass, will enqueue on next daemon tick.
    /// `"blocked"` — at least one non-review gate is failing.
    #[serde(default)]
    pub(crate) status: String,
    /// Human-readable gate failure when `status == "blocked"`, e.g.
    /// `"test verdict missing"`.  `None` when ready.
    #[serde(default)]
    pub(crate) reason: Option<String>,
}

/// #584: the top-level `coord serve` /board payload.
///
/// serde ignores unknown JSON keys by default, so the many extra columns the
/// daemon emits (schema_version, notifications, per-row provider/files fields,
/// etc.) are silently dropped.  JSON columns are decoded to native objects on
/// the wire EXCEPT `assignments.review_findings` (a raw JSON string) — handled
/// by the per-field serde attributes on `Assignment`.
#[derive(serde::Deserialize, Default)]
pub(crate) struct BoardPayload {
    /// Round counter from the daemon.  Not rendered today (the SQLite path
    /// never read it either) but kept on the wire shape for parity + tests.
    #[serde(default)]
    #[allow(dead_code)]
    pub(crate) round_number: i64,
    #[serde(default)]
    pub(crate) assignments: Vec<Assignment>,
    #[serde(default)]
    pub(crate) machines: Vec<RawMachine>,
    #[serde(default)]
    pub(crate) merge_queue: Vec<MergeQueueEntry>,
    /// Server-side merge plan (#776) — ranked, annotated list of planned merges.
    /// Computed by `coord.merge_queue.plan()` and injected by `serve_app.py`.
    /// Empty when the daemon is older than v0.4.53 (pre-#776).
    #[serde(default)]
    pub(crate) merge_plan: Vec<PlannedMergeEntry>,
    #[serde(default)]
    pub(crate) proposals: Vec<Proposal>,
    #[serde(default)]
    pub(crate) issues: Vec<OpenIssue>,
    /// assignment_id → decoded plan object (parsed via [`parse_plan_data`]).
    #[serde(default)]
    pub(crate) plans: std::collections::HashMap<String, serde_json::Value>,
    /// board_meta key → STRING value (JSON-encoded for the pipeline_* keys).
    #[serde(default)]
    pub(crate) board_meta: std::collections::HashMap<String, String>,
    /// #778: approved/done work not yet in the merge queue.  Computed
    /// server-side by `staging_items()`; empty when the board daemon is not
    /// running (the local SQLite path computes its own in `load_data`).
    #[serde(default)]
    pub(crate) merge_staging: Vec<StagingEntry>,
    /// #550: server-computed per-issue stage/gate projection.  Empty when
    /// the daemon predates #550 — the client-local `pipeline.rs` functions
    /// this mirrors remain the fallback in that case (and always for the
    /// local-SQLite-mode read path, which has no daemon to ask).
    #[serde(default)]
    pub(crate) issue_stage_projection: Vec<IssueStageProjection>,
    /// #795 Phase 3b: per-milestone work-order rank + ready frontier.  Empty
    /// when the daemon predates #795.  The TUI renders rank, next-up, and
    /// blocked-on badges on Pipeline milestone cards using this projection.
    #[serde(default)]
    pub(crate) milestone_work_orders: Vec<MilestoneWorkOrder>,
    /// #1195/#1197: per-epic child-issue lists, published under the wire key
    /// `children`. Empty on the local-SQLite-mode read path (no daemon to
    /// compute it) and on daemons older than #1195. The Pipeline view nests
    /// each epic's children beneath its row using this projection.
    #[serde(default, rename = "children")]
    pub(crate) epic_children: Vec<EpicChildren>,
    /// #975: milestone plan-roster — one entry per milestone/epic with
    /// ready / blocked / in-flight / done counts + attention signals,
    /// computed server-side by `coord.plans.aggregate_repo_plans`.  Empty on
    /// the local-SQLite-mode read path (no daemon to compute it) and on
    /// daemons older than #975 — the Plans panel renders "No plans yet" in
    /// that case.
    #[serde(default)]
    pub(crate) plan_roster: Vec<PlanRosterEntry>,
    /// #976: capability flag — true whenever the daemon computes
    /// `plan_roster` at all (see `serve_app.py`'s `board()` handler), even
    /// if the roster came back empty this tick due to a per-repo
    /// aggregation error. Absent (→ `false` via `#[serde(default)]`) on
    /// daemons older than #975, which never emit `plan_roster`/this flag at
    /// all. Lets the Plans panel tell "genuinely zero milestones" apart
    /// from "connected to a daemon too old to compute a roster" instead of
    /// rendering an identical, silent "0 plans" for both (the #976 review
    /// finding: a stale/pre-#975 daemon produced exactly this ambiguity).
    #[serde(default)]
    pub(crate) plan_roster_supported: bool,
    /// #978: GOAL.md pinned north-star header for the Plans panel — see
    /// `GoalHeader` doc comment. `#[serde(default)]` leaves `available:
    /// false` (the type's `Default`) on daemons that predate #978, which
    /// never emit this key at all.
    #[serde(default)]
    pub(crate) goal_header: GoalHeader,
    /// #1037/#1039: count of `audit_log` rows written in the last 15
    /// minutes — a single forward-compatible integer so the Audit
    /// ActivityBar panel can show an attention badge without fetching the
    /// full paginated `/audit` log.  `0` (the `Default`/`#[serde(default)]`
    /// value) on daemons older than #1037, which never emit this key.
    #[serde(default)]
    pub(crate) audit_recent_count: u64,
}

/// #584: serde deserializer for `Assignment::test_plan` on the remote
/// (`coord serve` /board) read path.  The wire shape is the decoded JSON
/// OBJECT `{"steps":[{kind,cmd,label,check},...], ...}` — NOT an array — so
/// we read it as an `Option<serde_json::Value>` and reuse the same
/// `.get("steps").as_array()` extraction as [`parse_test_plan_steps`].
/// Returns `None` on any shape mismatch (mirrors the SQLite path's tolerant
/// degradation to the "Preparing plan…" placeholder).
pub(crate) fn deserialize_test_plan<'de, D>(deserializer: D) -> Result<Option<Vec<TestPlanStep>>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    use serde::Deserialize;
    let val = Option::<serde_json::Value>::deserialize(deserializer)?;
    let Some(val) = val else {
        return Ok(None);
    };
    let Some(steps) = val.get("steps").and_then(|s| s.as_array()) else {
        return Ok(None);
    };
    let result: Vec<TestPlanStep> = steps
        .iter()
        .filter_map(|s| {
            let kind = s.get("kind")?.as_str()?.to_string();
            Some(TestPlanStep {
                kind,
                cmd: s.get("cmd").and_then(|v| v.as_str()).map(|s| s.to_string()),
                label: s
                    .get("label")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
                check: s
                    .get("check")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string()),
            })
        })
        .collect();
    Ok(Some(result))
}

/// #259: identifies what kind of sidebar row a context menu was opened
/// against.  Lets `dispatch_context_menu_action` route the action with
/// row-specific context (e.g. which issue number to copy, which repo
/// to scope a `gh issue edit` call against, etc.).
///
/// `repo_name` carries the **coord-local** name (matches `coordinator.yml`),
/// which is what `coord refine` / `coord ready` / etc. take as their
/// `<repo>` arg — the GH slug is looked up internally on the Python side.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum ContextMenuTarget {
    /// Right-click on a Board sidebar row.
    BoardRow {
        /// Issue under the cursor, or `None` when the click landed on
        /// a section header / empty space.
        issue_number: Option<u64>,
        /// Coord-local repo name for the row.
        repo_name: Option<String>,
        /// #260: lifecycle classification of the row at right-click
        /// time.  Drives which menu items appear (e.g. Refine only on
        /// Backlog rows).  See `Self::lifecycle_for_target` for how
        /// this is computed.
        lifecycle: BoardRowLifecycle,
    },
    /// Right-click on a Pipeline sidebar row.
    PipelineRow {
        issue_number: Option<u64>,
        /// Coord-local repo name for the row, drawn from
        /// `PipelineIssue::coord_repo`.  Used to gate live/zombie-session
        /// actions (Reattach vs Start) repo-precisely so that repo-a/#N and
        /// repo-b/#N don't cross-contaminate each other's menus (#983).
        /// `None` when no row is selected or the issue has no `coord_repo`.
        repo_name: Option<String>,
        /// #262: lifecycle classification of the row at right-click
        /// time.  Drives which menu items appear (e.g. Start with
        /// Plan / Skip Plan only on New rows).
        lifecycle: PipelineRowLifecycle,
    },
    /// Right-click on a Machines sidebar row.  Used for the routing-pause
    /// menu (`Pause` / `Resume`) — the user can steer agents away from a
    /// machine without editing coordinator.yml or shelling out.
    MachineRow { name: String, is_paused: bool },
    /// #771: right-click (or the keyboard shortcut) on the Milestone DAG
    /// view's milestone header. Carries what `coord milestone dispatch`
    /// needs — the coord-local repo name and the tracking-issue number —
    /// so the menu's "Dispatch milestone" item can spawn it directly.
    ///
    /// #1003: also reused for a right-click on a **Plans-panel** row (the
    /// Plans panel "elevates and subsumes" this view per its own doc
    /// comment) — `milestone_number` was added so the CRUD items (Edit
    /// milestone…, Add/Remove issue…) have what `coord milestone
    /// edit`/`assign`/`remove` need without a second GH round trip.
    MilestoneHeader {
        repo_name: String,
        tracking_issue: u64,
        milestone_title: String,
        milestone_number: i64,
    },
    /// #956: right-click on a Terminal-view tree TERMINAL row (not a
    /// machine row — those have no menu yet). Carries what "Kill terminal"
    /// needs to dispatch `coord terminal kill <machine>:<name>`.
    TerminalRow { machine: String, name: String },
}

/// #262: lifecycle bucket for a Pipeline sidebar row at right-click
/// time.  Mirrors the umbrella's three Pipeline sections (New /
/// In-progress / Done) plus a catch-all for the existing classifier's
/// "refining" / "new" rows (coord-labelled but not dispatch-ready),
/// where Start is not yet appropriate.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum PipelineRowLifecycle {
    /// `coord` + `status:ready`, no assignments — ready to dispatch.
    New,
    /// Has at least one assignment.
    InProgress,
    /// Closed issue with assignments.
    Done,
    /// Other state (e.g. coord-labelled but `status:refining`, or
    /// coord-labelled without a `status:*` label — still needs
    /// scoping before Start is meaningful).
    Other,
}

/// #260: lifecycle bucket for a Board sidebar row at right-click time.
/// Different from `IssueGroup::lifecycle_section()` because this also
/// distinguishes Pending into Backlog / Refining / Refined based on
/// labels — `#226` will eventually surface those as separate sections,
/// but right-click actions need the distinction now so they can offer
/// the right next-step verb.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum BoardRowLifecycle {
    /// Open issue, no `status:*` label, no assignments.
    Backlog,
    /// Open issue with `status:refining` label.
    Refining,
    /// Open issue with `status:ready` label, no `coord` label yet.
    Refined,
    /// Has at least one assignment, issue still open.
    InFlight,
    /// Closed issue with at least one assignment.
    Completed,
    /// Couldn't classify (no row focused / row not in any known state).
    Unknown,
}

/// Whether a Merge action against the currently-selected Pipeline row
/// will actually do something useful.  Drives both the dispatch path
/// (so silent no-ops become actionable toasts) and the toolbar button's
/// enabled state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum PipelineMergeState {
    /// View isn't the Pipeline panel, or no row is selected.
    NotApplicable,
    /// Selected issue has no merge_queue entry — the worker hasn't
    /// pushed a branch / opened a PR yet.
    NoQueue { issue: u64 },
    /// Selected issue's merge_queue entry is already merged.
    Merged { issue: u64 },
    /// Adversarial-review verdict isn't `approve` (`request-changes`,
    /// `pending`, or never run).  Server-side `coord merge` will
    /// refuse; surfacing it here gives the user actionable feedback.
    BlockedOnReview { issue: u64, verdict: String },
    /// CI checks are failed for the PR.  Falls through to the existing
    /// `pending_force_merge` confirm prompt so the user can opt in.
    BlockedOnCi { issue: u64, repo: String },
    /// Safe to dispatch `coord merge --repo <repo>`.
    /// `repo_slug` is the GitHub `owner/name` slug used to key
    /// `pipeline_inflight_merges`; `repo` is the coord-local name passed to
    /// `--repo`.
    Ready { issue: u64, repo: String, repo_slug: String },
}

/// #259: one item in an open context menu.  Lightweight engine-side
/// shape converted to `quadraui::ContextMenuItem` at render time.
#[derive(Clone, Debug)]
pub(crate) struct ContextMenuItem {
    /// Action identifier dispatched on click / Enter.
    /// `None` with `submenu.is_none()` ⇒ separator.
    /// `None` with `submenu.is_some()` ⇒ pull-right parent.
    pub(crate) action_id: Option<String>,
    pub(crate) label: String,
    /// Optional right-aligned shortcut hint (e.g. `"r"`).
    pub(crate) shortcut: Option<String>,
    pub(crate) disabled: bool,
    /// Pull-right submenu (#607).  When `Some`, activating the item opens
    /// the child menu instead of dispatching an action.
    pub(crate) submenu: Option<Vec<ContextMenuItem>>,
}

impl ContextMenuItem {
    pub(crate) fn action(id: &str, label: &str) -> Self {
        Self {
            action_id: Some(id.to_string()),
            label: label.to_string(),
            shortcut: None,
            disabled: false,
            submenu: None,
        }
    }
    pub(crate) fn separator() -> Self {
        Self {
            action_id: None,
            label: String::new(),
            shortcut: None,
            disabled: false,
            submenu: None,
        }
    }
    /// Create a pull-right parent item whose children appear in a submenu.
    pub(crate) fn parent(label: &str, children: Vec<ContextMenuItem>) -> Self {
        Self {
            action_id: None,
            label: label.to_string(),
            shortcut: None,
            disabled: false,
            submenu: Some(children),
        }
    }
    pub(crate) fn with_shortcut(mut self, s: &str) -> Self {
        self.shortcut = Some(s.to_string());
        self
    }
    /// True iff this item is a visual separator (neither action nor parent).
    pub(crate) fn is_separator(&self) -> bool {
        self.action_id.is_none() && self.submenu.is_none()
    }
    /// True iff this item can be selected (action or submenu parent, not disabled).
    pub(crate) fn is_selectable(&self) -> bool {
        (self.action_id.is_some() || self.submenu.is_some()) && !self.disabled
    }
}

#[derive(Clone, serde::Deserialize)]
#[allow(dead_code)] // pr_url stored for future display
pub(crate) struct MergeQueueEntry {
    pub(crate) assignment_id: String,
    #[serde(default)]
    pub(crate) issue_number: Option<u64>,
    pub(crate) state: String,
    #[serde(default)]
    pub(crate) pr_number: Option<i64>,
    #[serde(default)]
    pub(crate) pr_url: Option<String>,
    /// Repo slug (owner/name) — needed to call `gh pr checks --repo <slug>`.
    /// Joined from the `merge_queue.repo_github` column.
    pub(crate) repo_github: String,
    /// Target branch the PR merges into (e.g. "main").  `None` for entries
    /// written before this column was read by the TUI.
    #[serde(default)]
    pub(crate) target_branch: Option<String>,
    /// Last gate-eval error string from `coord merge`, if any.  Non-empty
    /// is the single most useful clue when a merge is stalled.
    #[serde(default)]
    pub(crate) error: Option<String>,
    /// Worker branch name — joined from `assignments.branch`.  Used by
    /// `compute_staging_local` for branch-level dedup (#778): a fix worker
    /// that shares a branch with an already-queued original work assignment
    /// must be excluded from staging even though it has a different assignment_id.
    #[serde(default)]
    pub(crate) branch: Option<String>,
    /// Milestone title — client-side join from `open_issues` keyed on
    /// `(repo_name, issue_number)`.  `None` when the issue carries no
    /// milestone or the issue row is absent from `open_issues`.
    #[serde(default)]
    pub(crate) milestone_title: Option<String>,
    /// Unix timestamp of the last merge attempt (`coord/merge_queue.py` sets
    /// this immediately before calling `gh pr merge`, and leaves it untouched
    /// on success) — for a `state == "merged"` entry this IS the merge time.
    /// #913: the Pipeline Done section's recency window + sort use this (via
    /// `issue_done_at`) instead of the work assignment's `finished_at`, so a
    /// freshly-merged item lands in Done immediately rather than being keyed
    /// off however long ago the work itself finished.
    #[serde(default)]
    pub(crate) last_attempt: Option<f64>,
}

/// One entry in the server-side merge plan from #776.
///
/// Deserialized from the `merge_plan` key of the `/board` payload, which is
/// computed by `coord.merge_queue.plan()` and injected by `serve_app.py`.
/// Unlike the raw `MergeQueueEntry` (DB row), this carries computed fields
/// that are always fresh: `rank` (true merge order), `status` (READY /
/// BLOCKED / MERGING / MERGED / NEEDS_ATTENTION), `reason` (live gate
/// explanation), `size` (diff lines), `enqueued_at`, and `last_attempt`.
///
/// The panel reads this structure; it does **not** re-derive ordering or gate
/// status in Rust.
#[derive(Clone, serde::Deserialize, Default)]
pub(crate) struct PlannedMergeEntry {
    pub(crate) assignment_id: String,
    pub(crate) repo_name: String,
    pub(crate) repo_github: String,
    #[allow(dead_code)]
    pub(crate) branch: String,
    pub(crate) target_branch: String,
    pub(crate) issue_number: u64,
    pub(crate) issue_title: String,
    /// 1-based position in the true merge sequence across all repos.
    pub(crate) rank: u32,
    /// Diff size in lines (populated at enqueue; `None` = unknown).
    #[serde(default)]
    pub(crate) size: Option<i64>,
    /// Computed status: "READY" | "BLOCKED" | "MERGING" | "MERGED" | "NEEDS_ATTENTION".
    pub(crate) status: String,
    /// Human-readable explanation of why the entry is BLOCKED (None when not blocked).
    #[serde(default)]
    pub(crate) reason: Option<String>,
    /// Unix timestamp when the entry was enqueued.
    #[serde(default)]
    pub(crate) enqueued_at: Option<f64>,
    /// Unix timestamp of the last merge attempt.
    #[serde(default)]
    pub(crate) last_attempt: Option<f64>,
    /// Issue milestone title (None when no milestone).  Stored for future
    /// display (e.g. milestone grouping in a later phase); not shown in the
    /// #777 panel which groups by repo→target_branch instead.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) milestone: Option<String>,
}

/// One entry in the server-side per-issue stage/gate projection (#550).
///
/// Deserialized from the `issue_stage_projection` key of the `/board`
/// payload, computed by `coord.stage_projection.compute_board_stage_projection`
/// and injected by `serve_app.py` — generalizes the #776/#778 pattern to
/// coord-tui's `pipeline.rs` stage-status functions (`stage_status_for`,
/// `merge_stage_status_for`, `test_stage_status_for`,
/// `issue_has_any_approved_review`).
///
/// `stages` maps a stage name (e.g. "work", "review", "test", "merge") to a
/// lowercase status string (`"pending"|"active"|"done"|"failed"|"stale"|"skipped"`)
/// — kept as a plain string on the wire (not `quadraui::StageStatus` directly)
/// so this crate isn't coupled to that enum's serde representation; see
/// [`parse_stage_status`] in `pipeline.rs` for the mapping.
///
/// Deliberately excludes TUI-session-local overlays (an optimistic
/// "merge just dispatched" flag, a locally-spawned test-build subprocess,
/// the TUI's own CI-check poll cache) — those are applied client-side on top
/// of this projection; see `pipeline.rs` for where each is layered back in.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct IssueStageProjection {
    pub(crate) repo_name: String,
    pub(crate) issue_number: u64,
    #[allow(dead_code)]
    pub(crate) issue_title: String,
    pub(crate) stages: std::collections::HashMap<String, String>,
    pub(crate) has_approved_review: bool,
}

/// One node in a milestone work order from the `/board` payload (#795 Phase 3b).
///
/// Deserialized from the `milestone_work_orders[*].nodes[*]` key of the
/// `/board` payload, computed by `coord/serve_app.py` from the tracking
/// issue's `## Work order` block via `coord.milestone_order`. The TUI uses
/// this to render work-order rank, next-up, and blocked-on badges on Pipeline
/// milestone cards without re-implementing the DAG logic in Rust.
///
/// `rank` is 0-indexed position in the declared work order. `ready` is `true`
/// when all `after:` dependencies are terminal. `next_up` refines `ready`
/// further: the issue is ready AND not already claimed by an active assignment.
/// `blocked_on` carries the still-unmet dependency issue numbers when `ready`
/// is `false`.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct MilestoneWorkOrderNode {
    pub(crate) issue_number: u64,
    /// 0-indexed position in the `## Work order` list (display as rank+1).
    pub(crate) rank: u32,
    pub(crate) ready: bool,
    /// `true` when `ready` AND not claimed — the dispatcher's next candidates.
    pub(crate) next_up: bool,
    #[serde(default)]
    pub(crate) blocked_on: Vec<u64>,
}

/// One child of an epic tracking issue, from the `/board` payload's
/// top-level `children[*].children[*]` (#1195 EP-1 seam).
///
/// `state` reflects the tracking issue's `## Sub-issues` checklist checkbox
/// (`"closed"` when checked, `"open"` otherwise) — an approximation of the
/// real GitHub issue state, computed server-side by
/// `coord.parentage.MarkdownParentage` (see its docstring). Good enough for
/// nesting/display; #1197 additionally cross-references
/// `data.open_issues`/`data.assignments` via `milestone_dag::build_dag_nodes`
/// for a live Done/InFlight/Ready badge rather than trusting this checkbox
/// state directly.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct EpicChild {
    pub(crate) number: u64,
    #[allow(dead_code)]
    pub(crate) state: String,
}

/// One epic's child-issue list from the `/board` payload (#1195 EP-1 seam,
/// consumed for Pipeline tree nesting by #1197).
///
/// Deserialized from `children[*]` — computed server-side by
/// `coord/serve_app.py` via `coord.parentage.MarkdownParentage` over the
/// tracking issue's own already-cached body (the `## Sub-issues` checklist,
/// #1008), so no extra `gh`/network round trip. Only epics with at least one
/// child are published. Empty on daemons older than #1195.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct EpicChildren {
    pub(crate) repo_name: String,
    pub(crate) tracking_issue: u64,
    #[serde(default)]
    pub(crate) children: Vec<EpicChild>,
}

/// One milestone's work-order data from the `/board` payload (#795 Phase 3b).
///
/// Deserialized from `milestone_work_orders[*]` in the `/board` payload. The
/// TUI looks up `(repo_name, issue_number)` across all `MilestoneWorkOrder`
/// entries to augment Pipeline milestone card rows with rank and frontier state.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct MilestoneWorkOrder {
    pub(crate) repo_name: String,
    /// Tracking issue number (the "epic"-labelled issue carrying the
    /// `## Work order` block). Not rendered directly in the #795 Phase 3b
    /// UI, but read by `milestone_tracking_issue_for` (#1060) to build the
    /// `coord acceptance author <repo> <tracking_issue> --issue N` argument
    /// list for a member issue's per-row context-menu action.
    pub(crate) tracking_issue: u64,
    /// Milestone title from the tracking issue's GitHub milestone field.
    /// Kept for future display; not shown in the #795 Phase 3b card badges.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) milestone_title: String,
    #[serde(default)]
    pub(crate) nodes: Vec<MilestoneWorkOrderNode>,
}

/// One row in the milestone plan-roster from the `/board` payload (#975).
///
/// Deserialized from the top-level `plan_roster[*]` list, computed
/// server-side by `coord/serve_app.py` via
/// `coord.plans.aggregate_repo_plans` — one entry per milestone/epic across
/// all configured repos.  The coord-tui "Plans" panel renders one row per
/// entry with ready / blocked / in-flight / done counts and a `needs_you`
/// attention list.
///
/// **Serde note (#632).** Every field here must match the JSON type emitted
/// by `PlanEntry.to_dict()` in `coord/plans.py`.  Counts are integers,
/// `has_work_order` is a bool (Python bool → JSON true/false, so no
/// `de_bool_from_int_or_bool` needed), `tracking_issue` can be null.  A
/// mistyped field would fail the whole `BoardPayload` parse and blank the
/// board on load.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct PlanRosterEntry {
    /// Coord-local repo name (matches `coordinator.yml`).
    pub(crate) repo: String,
    /// Milestone title, e.g. `"Substrate"`.
    pub(crate) title: String,
    /// GitHub milestone number.
    pub(crate) milestone_number: i64,
    /// Tracking-epic issue number, or `None` when the milestone has no
    /// `"epic"`-labelled issue.  When present, selecting the row opens this
    /// issue in the browser via `gh issue view --web`.
    #[serde(default)]
    pub(crate) tracking_issue: Option<u64>,
    /// True iff the tracking epic body carries a parseable `## Work order`
    /// block with ≥1 node.  False for milestones surfaced in the roster
    /// purely because member issues reference them (no epic yet) — those
    /// get `needs_you: ["no_work_order"]` and zero counts.
    #[serde(default)]
    pub(crate) has_work_order: bool,
    /// Work-order nodes on the ready frontier (dependencies met, unclaimed,
    /// not terminal) — the dispatcher's next candidates.
    #[serde(default)]
    pub(crate) ready_frontier: u32,
    /// Work-order nodes blocked by unmet dependencies or a conflict.
    #[serde(default)]
    pub(crate) blocked: u32,
    /// Work-order nodes currently claimed by an active board assignment or
    /// a remote `issue-N-*` branch.
    #[serde(default)]
    pub(crate) in_flight: u32,
    /// Work-order nodes that have reached a terminal (closed) state.
    #[serde(default)]
    pub(crate) done: u32,
    /// Total work-order nodes declared under this milestone.
    #[serde(default)]
    pub(crate) total: u32,
    /// Ordered attention signals — currently one of:
    ///   * `"no_work_order"` — open milestone with no parseable work order
    ///   * `"ready_waiting"` — ≥1 ready-frontier entry exists to dispatch
    ///   * `"stalled"` — open with a work order but nothing ready or in flight
    /// #976 will paint these as health chips on each row.
    #[serde(default)]
    pub(crate) needs_you: Vec<String>,
    /// #886 Phase 2: the latest Milestone Outcome Audit (`--audit-of`) run
    /// number for this milestone's epic. `None` when no audit has ever run
    /// against it — the Outcome chip is omitted entirely in that case rather
    /// than showing a fabricated 0/0.
    #[serde(default)]
    pub(crate) outcome_run_number: Option<u32>,
    /// Goals verdicted `"met"` in the latest run.
    #[serde(default)]
    pub(crate) outcome_met: Option<u32>,
    /// Goals verdicted `"partial"` in the latest run.
    #[serde(default)]
    pub(crate) outcome_partial: Option<u32>,
    /// Goals verdicted `"gap"` in the latest run — the count the done-gate
    /// cares about: milestone completion is judged by goals met, not issues
    /// closed.
    #[serde(default)]
    pub(crate) outcome_gap: Option<u32>,
    /// The latest run's one-line bottom-line verdict (e.g. "5/6 goals met").
    #[serde(default)]
    pub(crate) outcome_bottom_line: Option<String>,
    /// Pre-rendered delta vs the previous run (e.g. "v1→v2: closed: tests.rs
    /// split; still open: #550"), computed server-side by
    /// `coord.plans._latest_audit_outcome`. `None` on the first run (nothing
    /// to diff against) or when neither run moved any goal.
    #[serde(default)]
    pub(crate) outcome_diff_summary: Option<String>,
}

/// GOAL.md pinned north-star header for the Plans panel (#978).
///
/// Deserialized from the top-level `goal_header` object, computed
/// server-side by `coord.goal.read_goal_header()` and injected into the
/// `/board` payload by `coord/serve_app.py`'s `board()` handler. GOAL.md is a
/// repo-root doc that isn't shipped in the `coord` PyPI package, so this is
/// only ever populated when the daemon happens to be running from an actual
/// git checkout — everywhere else (older daemons that predate #978, packaged
/// installs, local-SQLite-mode with no daemon at all) `available` is `false`
/// (the `Default` impl, also what `#[serde(default)]` on `BoardData`'s field
/// produces when the key is absent entirely) and the Plans panel renders
/// exactly as it did before this field existed — no header strip, full rect
/// to the roster.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct GoalHeader {
    /// True iff the daemon located and parsed a GOAL.md this tick.
    #[serde(default)]
    pub(crate) available: bool,
    /// The north-star one-liner (bolded sentence under GOAL.md's "North
    /// star" heading, or its H1 title as a fallback), pre-truncated
    /// server-side.
    #[serde(default)]
    pub(crate) headline: String,
    /// ISO `YYYY-MM-DD` parsed from GOAL.md's `_Last updated: ..._` line, or
    /// `None` if that line is missing/malformed.
    #[serde(default)]
    pub(crate) last_updated: Option<String>,
    /// Server-computed `today - last_updated` in days (Python's `datetime`
    /// does the calendar math so the Rust side never needs a date crate).
    /// `None` whenever `last_updated` is `None`.
    #[serde(default)]
    pub(crate) days_since_update: Option<i64>,
}

/// #1039: one row from the `/audit` endpoint (#1037) — deserialized
/// verbatim from the `entries[*]` wire shape pinned by contract §6
/// (`tests/acceptance/ms-33/contract.md`).
///
/// `pub`, not `pub(crate)`: this is the parameter type of
/// `app::fixtures::make_app_with_audit_json` (test-support-feature-gated),
/// which must be at least as visible as that `pub fn` (E0446) to be
/// reachable from an external integration-test crate — same rationale as
/// `Assignment` above.
#[derive(Clone, Debug, serde::Deserialize)]
pub struct AuditEntry {
    pub id: i64,
    pub ts: f64,
    #[serde(default)]
    pub tier: Option<String>,
    pub category: String,
    pub event_type: String,
    pub actor: String,
    #[serde(default)]
    pub repo: Option<String>,
    #[serde(default)]
    pub issue: Option<u64>,
    #[serde(default)]
    pub assignment_id: Option<String>,
    #[serde(default)]
    pub machine: Option<String>,
    pub summary: String,
    /// JSON-decoded `details` object — `null` (→ `None`) when the source
    /// `details_json` column was `NULL`.
    #[serde(default)]
    pub details: Option<serde_json::Value>,
}

/// #1039: the `/audit` endpoint's paginated response envelope (contract
/// §6). Cached on `CoordApp` (see `audit_page`), populated by
/// `spawn_audit_fetch`/`data::spawn_audit_fetch` — deliberately kept off
/// `BoardData`/`BoardPayload` (the epic's instruction: "Do not add the log
/// to BoardPayload" — `/board` carries only the `audit_recent_count`
/// summary integer below).
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub struct AuditPage {
    #[serde(default)]
    pub entries: Vec<AuditEntry>,
    /// Keyset cursor for the next page (contract §6). Unread in this slice:
    /// `spawn_audit_fetch` only ever requests the first page — #1040 wires
    /// `since`/`category`/`type` filters onto that first-page request, but
    /// "load more" pagination is still a later issue; kept on the
    /// wire-shape type now so that doesn't need another deserializer change.
    #[serde(default)]
    #[allow(dead_code)]
    pub next_cursor: Option<String>,
    /// Whether more pages exist beyond `next_cursor`. Same rationale as
    /// `next_cursor` above.
    #[serde(default)]
    #[allow(dead_code)]
    pub has_more: bool,
}

/// #1040: time-range filter for the Audit panel, cycled forward by the `t`
/// key (contract §8, `tests/acceptance/ms-33/contract.md`). Maps to the
/// `/audit` endpoint's `since` query param — there is deliberately no
/// `until` side, matching contract §8's param table.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub(crate) enum AuditTimeRange {
    /// Last 60 minutes.
    LastHour,
    /// Since the start of the current UTC day. (No timezone crate is
    /// available in this workspace — see `since()` below.)
    Today,
    /// Last 7 days.
    D7,
    /// No lower bound — the default (contract §8: "Default: All").
    #[default]
    All,
}

impl AuditTimeRange {
    /// Cycle forward: `LastHour -> Today -> D7 -> All -> LastHour`.
    pub(crate) fn next(self) -> Self {
        match self {
            AuditTimeRange::LastHour => AuditTimeRange::Today,
            AuditTimeRange::Today => AuditTimeRange::D7,
            AuditTimeRange::D7 => AuditTimeRange::All,
            AuditTimeRange::All => AuditTimeRange::LastHour,
        }
    }

    /// Exact display string pinned by contract §8 — shown in the sidebar
    /// and the `t=time-range (…)` status-bar hint.
    pub(crate) fn label(self) -> &'static str {
        match self {
            AuditTimeRange::LastHour => "Last hour",
            AuditTimeRange::Today => "Today",
            AuditTimeRange::D7 => "7d",
            AuditTimeRange::All => "All",
        }
    }

    /// The `/audit` `since=` lower bound (epoch seconds) for this range, or
    /// `None` for "All" (no param sent). `now` is epoch seconds, passed in
    /// by the caller (rather than read here via `SystemTime::now()`) so
    /// this stays a pure function callers can unit-test deterministically.
    ///
    /// "Today" uses the UTC calendar day boundary (`now` truncated to the
    /// nearest 86400s), not the operator's local timezone — this workspace
    /// has no timezone crate, and UTC-day is a reasonable, unambiguous
    /// approximation of contract §8's "start_of_today_epoch".
    pub(crate) fn since(self, now: f64) -> Option<f64> {
        match self {
            AuditTimeRange::LastHour => Some(now - 3_600.0),
            AuditTimeRange::Today => Some(now - now.rem_euclid(86_400.0)),
            AuditTimeRange::D7 => Some(now - 604_800.0),
            AuditTimeRange::All => None,
        }
    }
}

/// #1040: category filter for the Audit panel, cycled forward by `Tab`
/// (contract §9). Maps to the `/audit` endpoint's `category` query param.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub(crate) enum AuditCategory {
    /// No category filter applied — the default (contract §9).
    #[default]
    All,
    Dispatch,
    Test,
    Review,
    Merge,
    Override,
    Plan,
    Error,
}

impl AuditCategory {
    /// Cycle forward through the exact enum-value order pinned by contract
    /// §9: `all -> dispatch -> test -> review -> merge -> override -> plan
    /// -> error -> all`.
    pub(crate) fn next(self) -> Self {
        match self {
            AuditCategory::All => AuditCategory::Dispatch,
            AuditCategory::Dispatch => AuditCategory::Test,
            AuditCategory::Test => AuditCategory::Review,
            AuditCategory::Review => AuditCategory::Merge,
            AuditCategory::Merge => AuditCategory::Override,
            AuditCategory::Override => AuditCategory::Plan,
            AuditCategory::Plan => AuditCategory::Error,
            AuditCategory::Error => AuditCategory::All,
        }
    }

    /// Exact lowercase string (contract §9) — both the display label (shown
    /// in the sidebar / status-bar hint) and the `/audit` `category=` query
    /// value for every variant except `All`.
    pub(crate) fn label(self) -> &'static str {
        match self {
            AuditCategory::All => "all",
            AuditCategory::Dispatch => "dispatch",
            AuditCategory::Test => "test",
            AuditCategory::Review => "review",
            AuditCategory::Merge => "merge",
            AuditCategory::Override => "override",
            AuditCategory::Plan => "plan",
            AuditCategory::Error => "error",
        }
    }

    /// The `/audit` `category=` query value, or `None` for `All` (contract
    /// §9: "no category filter applied").
    pub(crate) fn query_value(self) -> Option<&'static str> {
        match self {
            AuditCategory::All => None,
            other => Some(other.label()),
        }
    }
}

/// #1116: time-scope selector for the Usage panel, cycled forward by `t`
/// (mirrors `AuditTimeRange`'s role). `Today`/`Week`/`Month` are UTC
/// calendar-boundary presets (this workspace has no chrono/time crate — see
/// `app/usage.rs`'s civil-calendar helpers); `Custom` is entered via the
/// "Custom range…" two-step dialog (`c`), not part of the `t` cycle.
#[derive(Clone, Copy, PartialEq, Debug)]
pub(crate) enum UsageScope {
    Today,
    Week,
    Month,
    /// Explicit `[start, end)` UTC instants (Unix seconds), set by the
    /// custom-range dialog. Cycling `t` from here goes back to `Today`
    /// rather than continuing a `Today -> Week -> Month -> Custom -> ...`
    /// loop — `Custom` is a deliberate one-off pick, not a cycle stop.
    Custom { start: f64, end: f64 },
}

impl Default for UsageScope {
    fn default() -> Self {
        UsageScope::Today
    }
}

impl UsageScope {
    /// Cycle forward: `Today -> Week -> Month -> Today`; `Custom -> Today`.
    pub(crate) fn cycle_next(self) -> Self {
        match self {
            UsageScope::Today => UsageScope::Week,
            UsageScope::Week => UsageScope::Month,
            UsageScope::Month => UsageScope::Today,
            UsageScope::Custom { .. } => UsageScope::Today,
        }
    }
}

/// #1116: grouping dimension for the Usage grid, toggled by `g`.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub(crate) enum UsageGroupBy {
    #[default]
    Issue,
    Repo,
}

impl UsageGroupBy {
    pub(crate) fn next(self) -> Self {
        match self {
            UsageGroupBy::Issue => UsageGroupBy::Repo,
            UsageGroupBy::Repo => UsageGroupBy::Issue,
        }
    }

    pub(crate) fn label(self) -> &'static str {
        match self {
            UsageGroupBy::Issue => "Issue",
            UsageGroupBy::Repo => "Repo",
        }
    }
}

/// #1116: which metric the Usage grid is sorted by. `CostTotal` (captured +
/// estimated) is the default — matching the CLI's (#1115) desc-by-total-cost
/// default — and isn't tied to any single visible column (the grid shows
/// captured cost and estimated cost as separate columns), so no header
/// sort-arrow is shown until the operator clicks one. Clicking a header
/// switches to that column's key (toggling direction on a repeat click of
/// the same column) — see `usage::column_sort_key`/`usage_sort_by_column`.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub(crate) enum UsageSortKey {
    #[default]
    CostTotal,
    IssueNumber,
    Repo,
    Title,
    Legs,
    CostCaptured,
    CostEst,
    Tokens,
    Time,
}

/// CI check status for one PR, fetched in the background via `gh pr checks`.
///
/// Populated from `fetch_ci_checks_summary` and stored on `CoordApp` keyed by
/// `(repo_github, pr_number)`. Drives the "Checks: 2✓ 1✗" line under the
/// Merge stage in the Pipeline detail tab and the "Checks failed" status bar
/// hint when Merge is actionable.
#[derive(Clone, Debug)]
pub(crate) struct CiCheckSummary {
    pub(crate) passed: usize,
    pub(crate) failed: usize,
    pub(crate) running: usize,
    /// Names of failed checks (for the status-bar hint and detail row).
    pub(crate) failed_names: Vec<String>,
    /// URL of the first failed check — populated for future surfacing in the
    /// Merge Queue panel CI detail view (#738: previously shown in the retired
    /// per-issue Merge stage rows, now awaiting a Phase-3 panel detail row).
    #[allow(dead_code)]
    pub(crate) first_failed_url: Option<String>,
    /// When this summary was fetched. Used to TTL the cache.
    pub(crate) fetched_at: Instant,
}

impl CiCheckSummary {
    pub(crate) fn has_failures(&self) -> bool {
        self.failed > 0
    }

    /// One-line summary like `2✓ 1✗ 1⋯`. Empty string when no checks at all
    /// (caller can suppress the row in that case).
    pub(crate) fn terse(&self) -> String {
        let mut parts: Vec<String> = Vec::new();
        if self.passed > 0 {
            parts.push(format!("{}✓", self.passed));
        }
        if self.failed > 0 {
            parts.push(format!("{}✗", self.failed));
        }
        if self.running > 0 {
            parts.push(format!("{}⋯", self.running));
        }
        parts.join(" ")
    }
}

#[derive(Clone, serde::Deserialize)]
pub(crate) struct Proposal {
    pub(crate) id: i64,
    #[serde(rename = "machine_name")]
    pub(crate) machine: String,
    #[serde(rename = "repo_name")]
    pub(crate) repo: String,
    pub(crate) issue_number: u64,
    pub(crate) issue_title: String,
    pub(crate) rationale: String,
    #[serde(rename = "type")]
    pub(crate) proposal_type: String,
}

/// One GitHub issue tracked by the pipeline panel.
///
/// Sourced from a background `gh search issues label:<L> --state all` poll
/// and matched back to a coord-local repo name via `pipeline_repos` in
/// `board_meta`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PipelineIssue {
    /// Issue number within the GitHub repo.
    pub(crate) number: u64,
    /// Issue title (as returned by gh).
    pub(crate) title: String,
    /// Issue body text (as returned by gh). Empty string when absent.
    pub(crate) body: String,
    /// `owner/name` slug of the GitHub repo the issue lives in.
    pub(crate) repo_slug: String,
    /// Coord-local repo name (matched via `pipeline_repos` map). `None` when
    /// the issue is in a repo not declared in coordinator.yml — such issues
    /// are still listed but cannot be dispatched.
    pub(crate) coord_repo: Option<String>,
    /// Tracked labels that flagged this issue (subset of `all_labels`).
    pub(crate) matched_labels: Vec<String>,
    /// All GitHub labels on this issue (not filtered by tracked labels).
    /// Used to compute lifecycle sections (status:refining, status:ready, …).
    pub(crate) all_labels: Vec<String>,
    /// True when the issue is closed on GitHub (`state == "closed"`).
    pub(crate) is_closed: bool,
}

#[derive(Default)]
/// A single issue freshly fetched via `gh issue view` for the Board Issue tab
/// when no row exists in the local `issues` table. Mirrors [`OpenIssue`] but
/// produced on-demand rather than from a sync.
#[derive(Clone, Debug)]
pub(crate) struct FetchedIssue {
    pub(crate) number: u64,
    pub(crate) title: String,
    pub(crate) body: String,
    pub(crate) labels: Vec<String>,
    /// "open" | "closed".  Carried so the DB upsert mirrors what `coord sync`
    /// would have written.
    pub(crate) state: String,
    /// #406: GitHub milestone number, if any.
    pub(crate) milestone_number: Option<i64>,
    /// #406: GitHub milestone title, if any.
    pub(crate) milestone_title: Option<String>,
}

/// #271 part 2 follow-up: cached `gh pr view` snapshot for a Pipeline
/// PR.  Surfaces the worker's PR description (which typically explains
/// what they did, including any new sample apps / demo binaries / entry
/// points) plus the list of files touched and the latest review's
/// verdict + body, so the user testing the branch has in-TUI context
/// instead of having to ask Claude or click out to GitHub.
#[derive(Clone, Debug)]
pub(crate) struct FetchedPr {
    pub(crate) title: String,
    /// Markdown body of the PR.  May be empty when the worker / merge
    /// command didn't write one.
    pub(crate) body: String,
    /// File paths touched by the PR.  Sorted by gh's default ordering.
    pub(crate) files: Vec<String>,
    /// Reviews posted on this PR, in the order gh returns them
    /// (chronological).  The latest one is what the user typically
    /// cares about (was it an approve or request-changes?).  When the
    /// adversarial reviewer posted via `coord notify`, the body is the
    /// full review text the reviewer wrote.
    pub(crate) reviews: Vec<FetchedReview>,
}

/// One row from `gh pr view --json reviews`.  `state` is gh's verbatim
/// status string: `"APPROVED"`, `"CHANGES_REQUESTED"`, `"COMMENTED"`,
/// or `"PENDING"`.
#[derive(Clone, Debug)]
pub(crate) struct FetchedReview {
    /// gh status string (uppercase).
    pub(crate) state: String,
    /// Markdown review body.  Empty when the reviewer left only a
    /// status change with no comment.
    pub(crate) body: String,
}

/// #248: parsed `<!-- coord:review ... -->` header.  The coordinator
/// prepends this to every review body so the TUI can surface the
/// verdict + counts without ingesting the prose.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct CoordReviewHeader {
    pub(crate) verdict: Option<String>,
    pub(crate) blocking: Option<u32>,
    pub(crate) nonblocking: Option<u32>,
    pub(crate) nits: Option<u32>,
    pub(crate) reviewer: Option<String>,
    pub(crate) assignment: Option<String>,
}

/// #558: One session entry shown in the Pipeline Summary tab.
/// Represents a single work/review/fix/plan session from GitHub comments.
/// #876: Used only in unit tests (the live Summary tab now sources from the
/// board layer via `build_board_summary_list_view`); the struct and its
/// helpers are compiled exclusively in test mode.
#[cfg(test)]
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct SessionSummary {
    /// The coord assignment id (from the `assignment=` marker token), or empty.
    pub(crate) assignment_id: String,
    /// Session type: "work", "review", "fix", "plan", "re-review", etc.
    /// Derived from `assignment_type` in the local DB when available, otherwise
    /// inferred from the comment event (completion → "work", review → "review").
    pub(crate) session_type: String,
    /// Machine name from the comment marker (`machine=` or `reviewer=` token).
    pub(crate) machine: String,
    /// Terminal status: "done", "failed", "advisory".
    pub(crate) status: String,
    /// Verdict for review sessions: "approve" | "request-changes".  None for
    /// non-review sessions.
    pub(crate) verdict: Option<String>,
    /// One-line prose summary extracted from the `### Summary` block (completion
    /// comments) or the first non-empty line of the REVIEW_BODY block (review
    /// comments).  May be empty when the worker didn't emit a summary.
    pub(crate) summary_text: String,
    /// Unix timestamp of the comment creation, used for newest-first sorting.
    /// Zero when the comment carries no timestamp.
    pub(crate) created_at_ts: f64,
}

/// An open issue from the local `issues` table (synced from GitHub on coord plan).
#[derive(Clone, serde::Deserialize)]
pub(crate) struct OpenIssue {
    pub(crate) repo_name: String,
    pub(crate) number: u64,
    pub(crate) title: String,
    /// Issue body, synced from GitHub via `coord sync`.  Empty string when
    /// the issue has no description.
    #[serde(default)]
    pub(crate) body: String,
    /// GitHub labels on this issue. Used by the Board Issue tab to render the
    /// same context the Pipeline Issue tab shows.
    #[serde(default)]
    pub(crate) labels: Vec<String>,
    /// "open" | "closed".  We load both into `data.open_issues` so the Board
    /// Issue tab can display bodies for closed issues (e.g. in the Completed
    /// group), but only "open" entries get injected as Pending rows.
    pub(crate) state: String,
    /// #406: GitHub milestone number.  `None` for issues without a milestone.
    #[serde(default)]
    pub(crate) milestone_number: Option<i64>,
    /// #406: GitHub milestone title (e.g. "v0.5").  `None` when no milestone.
    #[serde(default)]
    pub(crate) milestone_title: Option<String>,
}

/// #803: models config snapshot read from `board_meta['pipeline_models']`.
/// Mirrors `coord.config.ModelsConfig` and `pipeline.escalate_fix_model`.
/// Used to compute the escalated model tier for the interactive `--fix-of`
/// path without requiring the TUI to parse `coordinator.yml` itself.
#[derive(Clone, Debug, serde::Deserialize)]
pub(crate) struct PipelineModels {
    /// Alias used when no model is specified (e.g. `"sonnet"`).
    #[serde(default = "pipeline_models_default_tier")]
    pub(crate) default: String,
    /// Ordered list of model aliases (low → high).  Mirrors
    /// `models.escalation` in coordinator.yml.
    #[serde(default = "pipeline_models_default_escalation")]
    pub(crate) escalation: Vec<String>,
    /// When `false`, no escalation happens and every fix iteration uses the
    /// default model.  Mirrors `pipeline.escalate_fix_model`.
    #[serde(default = "pipeline_models_default_escalate")]
    pub(crate) escalate_fix_model: bool,
}

pub(crate) fn pipeline_models_default_tier() -> String { "sonnet".to_string() }
pub(crate) fn pipeline_models_default_escalation() -> Vec<String> {
    vec!["haiku".to_string(), "sonnet".to_string(), "opus".to_string()]
}
pub(crate) fn pipeline_models_default_escalate() -> bool { true }

impl Default for PipelineModels {
    fn default() -> Self {
        Self {
            default: pipeline_models_default_tier(),
            escalation: pipeline_models_default_escalation(),
            escalate_fix_model: pipeline_models_default_escalate(),
        }
    }
}

/// #803: pure function — mirrors Python's `_fix_model_for_iteration`.
///
/// Returns the model alias to use for a fix on the given iteration number
/// (1-based: iteration 1 = first fix, iteration 2 = second fix, …).
///
/// - `None` when `escalate_fix_model` is false — escalation is disabled, so
///   no model hint is surfaced (the fix will use coord's normal default).
/// - Iteration 1 → `Some(models.default)` (first fix stays cheap/fast).
/// - Iteration 2+ → climb one rung up `models.escalation` per extra
///   iteration, capped at the ladder top.
pub(crate) fn fix_model_for_iteration(models: &PipelineModels, iteration: i64) -> Option<String> {
    if !models.escalate_fix_model {
        return None;
    }
    let mut model = models.default.clone();
    let extra = (iteration.max(1) - 1) as usize;
    for _ in 0..extra {
        let pos = models.escalation.iter().position(|m| m == &model);
        match pos {
            Some(idx) if idx + 1 < models.escalation.len() => {
                model = models.escalation[idx + 1].clone();
            }
            _ => break, // already at the top or not on the ladder → cap
        }
    }
    Some(model)
}

#[derive(Default)]
// #1042: `pub`, not `pub(crate)` — see the note on `Assignment` above; this
// is `make_test_app`'s parameter type. Fields stay `pub(crate)`; external
// callers only ever need `BoardData::default()` (derived, no field access
// required) to build a fixture.
pub struct BoardData {
    pub(crate) local_machine: String,
    pub(crate) assignments: Vec<Assignment>,
    /// Open issues from the local SQLite `issues` table — the full backlog.
    pub(crate) open_issues: Vec<OpenIssue>,
    pub(crate) machines: Vec<Machine>,
    pub(crate) merge_queue: Vec<MergeQueueEntry>,
    /// Server-side merge plan (#776) — ranked list from `/board`.  Non-empty
    /// when the daemon is v0.4.53+ and has planned merges.  The MergeQueue
    /// panel renders this in preference to `merge_queue` when non-empty.
    pub(crate) merge_plan: Vec<PlannedMergeEntry>,
    pub(crate) proposals: Vec<Proposal>,
    /// Pipeline gate names from `pipeline.default_gates` in coordinator.yml.
    /// Defaults to `["review", "merge"]` when the board_meta key is absent.
    pub(crate) pipeline_default_gates: Vec<String>,
    /// GitHub issue labels considered "in the pipeline". Defaults to
    /// `["coord"]` when the board_meta key is absent.
    pub(crate) pipeline_tracked_labels: Vec<String>,
    /// Coord-local repo name → GitHub `owner/repo` slug (and inverse).
    /// Empty when no config snapshot has been written yet.
    pub(crate) pipeline_repos: Vec<(String, String)>,
    /// #296: coord-local repo name → `run_cmd` shell command from
    /// `coordinator.yml`.  Only repos that define `run_cmd` are present.
    /// Surfaced in the Test stage detail panel as the "Run" row.
    pub(crate) pipeline_repo_run_cmds: std::collections::HashMap<String, String>,
    /// #349: coord-local repo name → absolute local checkout path on this
    /// machine.  Populated by `_save_config_snapshot` from the hostname-
    /// matched `repo_paths` in `coordinator.yml` and stored in
    /// `board_meta['pipeline_repo_paths']`.  Used by the TUI to read git
    /// branch HEAD SHAs for test-plan staleness detection.
    pub(crate) pipeline_repo_paths: std::collections::HashMap<String, String>,
    /// #1151: coord-local repo name → list of `acceptance.drivers.<repo>
    /// .routes[*].match` globs (#1125 in-repo path routing), for repos whose
    /// acceptance driver is *routed*. Absent (no key) for an unrouted repo,
    /// or when the daemon predates this field. Populated by
    /// `_save_config_snapshot` and stored in
    /// `board_meta['pipeline_acceptance_routes']`. Used by
    /// `dispatch_gate_a_mock_for_selected_pipeline_row` /
    /// `dispatch_acceptance_author_for_selected_pipeline_row` /
    /// `dispatch_acceptance_record_for_selected_pipeline_row` to decide
    /// whether a `--for-path` is needed before firing `coord acceptance
    /// mock/author/record` — those CLI commands 500 with "no route matched"
    /// when the repo is routed and `--for-path` is omitted, which the TUI
    /// used to do unconditionally, every time.
    pub(crate) pipeline_acceptance_routes: std::collections::HashMap<String, Vec<String>>,
    /// Mirror of `dispatch.require_plan` from coordinator.yml.  When true,
    /// the pipeline prepends a Plan stage before Work, and Work [Go]
    /// becomes "approve the plan and dispatch work" rather than fresh
    /// dispatch.  Defaults to `false`.
    pub(crate) pipeline_require_plan: bool,
    /// Cached structured plans keyed by plan-assignment-id.  Populated by
    /// `coord notify` parsing the worker log into the `plans` table; the
    /// TUI just loads the JSON blob and renders it in the Plan stage
    /// content panel.
    pub(crate) plans: std::collections::HashMap<String, PlanData>,
    /// #778: approved/done work not yet in the merge queue.  Computed
    /// locally in `load_data` or received from the `/board` JSON payload.
    pub(crate) merge_staging: Vec<StagingEntry>,
    /// #803: model config snapshot for the interactive `--fix-of` escalation.
    /// `None` when `board_meta['pipeline_models']` has not been written yet
    /// (pre-#803 coordinator versions).  Falls back to the struct's `Default`
    /// implementation (sonnet default, [haiku,sonnet,opus] ladder, escalation
    /// enabled) which matches the coordinator.yml defaults.
    pub(crate) pipeline_models: Option<PipelineModels>,
    /// #550: server-computed per-issue stage/gate projection from `/board`.
    /// Empty on the local-SQLite-mode read path (no daemon to compute it) and
    /// on daemons older than #550 — `pipeline.rs`'s stage functions fall back
    /// to local computation in both cases.
    pub(crate) issue_stage_projection: Vec<IssueStageProjection>,
    /// #795 Phase 3b: per-milestone work-order rank + ready frontier from
    /// `/board`.  Empty on the local-SQLite-mode read path and daemons older
    /// than #795.  The Pipeline view renders rank/next-up/blocked-on badges
    /// on milestone cards using this projection.
    pub(crate) milestone_work_orders: Vec<MilestoneWorkOrder>,
    /// #1195/#1197: per-epic child-issue lists from `/board`'s `children`
    /// field.  Empty on the local-SQLite-mode read path and daemons older
    /// than #1195.  The Pipeline view nests each epic's children beneath its
    /// row using this projection (`compute_epic_nesting`, `pipeline.rs`).
    pub(crate) epic_children: Vec<EpicChildren>,
    /// #975: milestone plan-roster — one entry per milestone/epic, sourced
    /// from `/board`'s `plan_roster` field.  Empty on the local-SQLite-mode
    /// read path and daemons older than #975.  The Plans panel renders one
    /// row per entry.
    pub(crate) plan_roster: Vec<PlanRosterEntry>,
    /// #976: mirrors `BoardPayload::plan_roster_supported` — `true` only
    /// when the connected daemon actually computes `plan_roster` (any
    /// #975+ daemon, whether or not it found milestones this tick). `false`
    /// on the local-SQLite-mode read path (no daemon at all) and on daemons
    /// older than #975 (no such field on the wire). The Plans panel uses
    /// this — not `plan_roster.is_empty()` — to decide whether an empty
    /// roster means "genuinely no plans" or "not receiving plan data."
    pub(crate) plan_roster_supported: bool,
    /// #978: mirrors `BoardPayload::goal_header` — the pinned GOAL.md
    /// north-star header for the Plans panel. `available: false` (the
    /// type's `Default`) on the local-SQLite-mode read path (no daemon to
    /// read GOAL.md from) and on daemons older than #978.
    pub(crate) goal_header: GoalHeader,
    /// #1037/#1039: mirrors `BoardPayload::audit_recent_count` — count of
    /// audit rows written in the last 15 minutes, driving the Audit panel's
    /// sidebar "N recent" badge. `0` on the local-SQLite-mode read path and
    /// on daemons older than #1037.
    pub(crate) audit_recent_count: u64,
}

/// Parsed plan data, mirroring `coord.plan_parser.WorkerPlan.to_dict()`.
/// Only the fields we render are pulled out; everything else stays in
/// the original JSON blob (we don't roundtrip it back to disk).
#[derive(Clone, Debug, Default)]
pub(crate) struct PlanData {
    pub(crate) plan: String,
    pub(crate) files_modify: Vec<String>,
    pub(crate) approach: String,
    pub(crate) risks: String,
    pub(crate) estimate: String,
    /// Tri-state: None = no SMOKE_TESTS block emitted (legacy / plan
    /// worker forgot); `Some(empty)` = "(none — change is internal)";
    /// `Some(non-empty)` = bullets.
    pub(crate) smoke_tests: Option<Vec<String>>,
}
