//! App data-model types extracted from `app/mod.rs` (#743).
//!
//! DTO/enum structs and their pure impls — no I/O, no quadraui rendering.
use std::time::{Instant, SystemTime, UNIX_EPOCH};
use quadraui::Color;
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
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub(crate) enum PipelineDetailTab {
    /// Horizontal stage view + repo/labels/gates meta.
    #[default]
    Pipeline,
    /// Full issue body text (scrollable with j/k).
    Issue,
    /// Per-stage detail: assignment id, machine, status, timing,
    /// exit code (or merge-queue state for the merge stage).
    Stages,
    /// Live worker log — same content as the watch overlay but inline
    /// so the pipeline stage boxes remain visible above.
    Log,
    /// #558: session-history summary for the selected issue.  Lists every
    /// work/review/fix session (newest-first) with type, machine,
    /// status/verdict, and summary text.  Data is fetched from GitHub
    /// issue comments on demand and cached per-issue.
    Summary,
    /// #264: refinement chat for the selected issue.  Empty placeholder
    /// when no `type="refinement"` assignment is active for the row; an
    /// embedded `ChatController` when one is — so the user can flip back
    /// to Issue / Stages / Log while the chat keeps streaming in the
    /// background.
    Refinement,
    /// #440: per-issue interactive shell for the detail view.  Each active
    /// issue gets its own `TerminalSession`; only the selected issue's
    /// terminal is visible while others keep running in the background.
    /// Focus arbitration mirrors the standalone Terminal pane: F12 toggles
    /// PTY focus (detail_terminal_focused); F12-released keys drive normal
    /// TUI navigation.  The #437 human-attended launcher will dock into
    /// this tab in a follow-up — this PR hosts a generic shell only.
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
        }
    }
}

#[derive(Clone, serde::Deserialize)]
pub(crate) struct Assignment {
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
    pub(crate) cache_creation_tokens: i64,
    #[serde(default)]
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
#[derive(Clone, Debug)]
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
    MilestoneHeader {
        repo_name: String,
        tracking_issue: u64,
        milestone_title: String,
    },
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

/// One milestone's work-order data from the `/board` payload (#795 Phase 3b).
///
/// Deserialized from `milestone_work_orders[*]` in the `/board` payload. The
/// TUI looks up `(repo_name, issue_number)` across all `MilestoneWorkOrder`
/// entries to augment Pipeline milestone card rows with rank and frontier state.
#[derive(Clone, Debug, Default, serde::Deserialize)]
pub(crate) struct MilestoneWorkOrder {
    pub(crate) repo_name: String,
    /// Tracking issue number (the "epic"-labelled issue carrying the
    /// `## Work order` block).  Kept for future surfacing (e.g. a
    /// "jump to tracking issue" action) and round-trip parity with the
    /// server payload; not rendered in the #795 Phase 3b UI.
    #[allow(dead_code)]
    pub(crate) tracking_issue: u64,
    /// Milestone title from the tracking issue's GitHub milestone field.
    /// Kept for future display; not shown in the #795 Phase 3b card badges.
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) milestone_title: String,
    #[serde(default)]
    pub(crate) nodes: Vec<MilestoneWorkOrderNode>,
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
pub(crate) struct BoardData {
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
